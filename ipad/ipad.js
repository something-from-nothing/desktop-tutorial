// Desk Agent for iPad. The app page (the same index.html as the phone) talks to an AI
// engine at http://127.0.0.1:PORT, the way it does on Desk OS. On iPad there is no
// engine program, so this answers those requests itself, running the model inside
// Safari. It loads before the app's own script.
//
// Two ways to run the model, tried in order:
//  1. WebLLM on the graphics chip (fast). Older iPads (A12X/A12Z) allow less GPU
//     working memory than WebLLM asks for; build.sh patches it to ask for no more
//     than the iPad allows, and if the model still won't run there we move on.
//  2. wllama (llama.cpp) on the processor. Slower, but works on any iPad, and it
//     handles tool calls itself.
(() => {
  const PORT = "1";
  const GPU_MODELS = {f16: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC", f32: "Qwen2.5-1.5B-Instruct-q4f32_1-MLC"};
  const CPU_MODEL = "https://huggingface.co/bartowski/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf";
  const CONTEXT_CHARS = 9000;  // the model holds 4096 tokens; leave room for the answer
  // Tools that work in a browser. Web search can't (sites block other pages reading them).
  const IPAD_TOOLS = ["ask_user", "set_reminder", "create_artifact"];
  let engine = null, mode = null, loading = null, failed = null, gpuWorked = false;

  const status = t => { try { window.setStatus && setStatus(t); } catch (e) {} };
  const percent = (done, total) => total ? Math.round(100 * done / total) : 0;

  async function loadGpu() {
    if (!navigator.gpu) throw new Error("no WebGPU");
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) throw new Error("no WebGPU adapter");
    const webllm = await import("./webllm.js");
    const model = adapter.features.has("shader-f16") ? GPU_MODELS.f16 : GPU_MODELS.f32;
    engine = await webllm.CreateMLCEngine(model, {initProgressCallback: p =>
      status(p.progress < 1 ? `Getting the AI ready: ${Math.round(p.progress * 100)}%. The first time downloads about 1 GB.` : "")});
    mode = "gpu";
  }
  async function loadCpu() {
    const {Wllama} = await import("./wllama.js");
    const w = new Wllama({default: new URL("wllama.wasm", location.href).href});
    await w.loadModelFromUrl(CPU_MODEL, {
      n_ctx: 4096, n_gpu_layers: 0,
      progressCallback: ({loaded, total}) => status(loaded < total
        ? `Getting the AI ready: ${percent(loaded, total)}%. The first time downloads about 1 GB.` : "Starting the AI..."),
    });
    engine = w;
    mode = "cpu";
  }
  function load() {
    if (engine) return Promise.resolve();
    if (loading) return loading;
    loading = (async () => {
      try { await loadGpu(); }
      catch (e) {
        console.warn("Graphics chip AI unavailable, using the processor:", e);
        engine = null;
        status("This iPad's graphics chip can't run the AI, so it's running on the processor. Answers will be slower.");
        await loadCpu();
      }
      status("");
    })().catch(e => {
      failed = e;
      status("The AI couldn't start on this iPad: " + (e.message || e));
      throw e;
    }).finally(() => { loading = null; });
    return loading;
  }

  // Keep the newest messages that fit the model's memory (the system message stays).
  function trim(messages, size) {
    const sys = messages[0]?.role === "system" ? messages[0] : null;
    const rest = sys ? messages.slice(1) : messages.slice();
    let total = sys ? size(sys) : 0;
    const keep = [];
    for (let i = rest.length - 1; i >= 0; i--) {
      total += size(rest[i]);
      if (total > CONTEXT_CHARS && keep.length) break;
      keep.unshift(rest[i]);
    }
    while (keep.length && keep[0].role !== "user") keep.shift();
    return sys ? [sys, ...keep] : keep;
  }
  const msgSize = m => (m.content || "").length + JSON.stringify(m.tool_calls || "").length;

  // ---- WebLLM has no tool calls for Qwen, so they go in Qwen's own text format

  function toolsPrompt(tools) {
    return "\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n" +
      "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n" +
      tools.map(t => JSON.stringify(t)).join("\n") +
      "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n" +
      '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>';
  }
  function toGpuMessages(messages, tools) {
    const out = [];
    const push = (role, content) => {
      const last = out[out.length - 1];
      if (last && last.role === role && role !== "system") last.content += "\n\n" + content;
      else out.push({role, content});
    };
    for (const m of messages) {
      if (m.role === "system") push("system", m.content + (tools.length ? toolsPrompt(tools) : ""));
      else if (m.role === "assistant") {
        const calls = (m.tool_calls || []).map(c => `<tool_call>\n{"name": "${c.function.name}", "arguments": ${c.function.arguments || "{}"}}\n</tool_call>`);
        push("assistant", [m.content || "", ...calls].filter(Boolean).join("\n") || "...");
      } else if (m.role === "tool") push("user", `<tool_response>\n${m.content}\n</tool_response>`);
      else push("user", String(m.content || ""));
    }
    return trim(out, msgSize);
  }
  function parseToolCalls(text) {
    const calls = [];
    for (const m of text.matchAll(/<tool_call>\s*([\s\S]*?)\s*(?:<\/tool_call>|$)/g)) {
      try {
        const j = JSON.parse(m[1]);
        if (j.name) calls.push({name: j.name, arguments: JSON.stringify(j.arguments || {})});
      } catch (e) {}
    }
    return calls;
  }
  async function gpuChat(body, tools, send) {
    const chunks = await engine.chat.completions.create({messages: toGpuMessages(body.messages || [], tools), stream: true,
      max_tokens: Math.min(body.max_tokens || 1500, 1500), temperature: 0.6});
    let text = "", shown = 0, finish = "stop";
    for await (const ch of chunks) {
      const d = ch.choices?.[0]?.delta?.content || "";
      if (ch.choices?.[0]?.finish_reason === "length") finish = "length";
      if (!d) continue;
      text += d;
      // Stream the words, but hold back anything that is (or may become) a tool call.
      const cut = text.indexOf("<tool_call>");
      let safe = cut >= 0 ? cut : text.length;
      const partial = text.lastIndexOf("<");
      if (cut < 0 && partial >= 0 && "<tool_call>".startsWith(text.slice(partial))) safe = partial;
      if (safe > shown) { send({choices: [{index: 0, delta: {content: text.slice(shown, safe)}}]}); shown = safe; }
    }
    gpuWorked = true;
    const calls = parseToolCalls(text);
    if (calls.length) {
      send({choices: [{index: 0, delta: {tool_calls: calls.map((c, i) => ({index: i, id: "call_" + Math.random().toString(36).slice(2, 10), type: "function", function: c}))}}]});
      finish = "tool_calls";
    } else if (shown < text.length) send({choices: [{index: 0, delta: {content: text.slice(shown)}}]});
    send({choices: [{index: 0, delta: {}, finish_reason: finish}]});
  }

  // ---- wllama speaks the same chat API as the engine, tool calls included

  async function cpuChat(body, tools, send) {
    const messages = trim((body.messages || []).map(m => ({...m, content: m.content || ""})), msgSize);
    const stream = await engine.createChatCompletion({messages, ...(tools.length ? {tools} : {}), stream: true,
      max_tokens: Math.min(body.max_tokens || 1500, 1500), temperature: 0.6});
    for await (const ch of stream) send(ch);
  }

  // ---- the stand in for the engine's web API

  function chatResponse(body) {
    const tools = (body.tools || []).filter(t => IPAD_TOOLS.includes(t.function?.name));
    const enc = new TextEncoder();
    return new Response(new ReadableStream({
      async start(ctrl) {
        const send = obj => ctrl.enqueue(enc.encode(`data: ${JSON.stringify(obj)}\n\n`));
        try {
          await load();
          if (mode === "gpu") {
            try { await gpuChat(body, tools, send); }
            catch (e) {
              if (gpuWorked) throw e;
              // The graphics chip loaded the model but can't run it: switch for good.
              console.warn("Graphics chip AI failed on first use, switching to the processor:", e);
              engine = null; mode = null;
              status("Switching the AI to the processor. Answers will be slower.");
              await loadCpu();
              status("");
              await cpuChat(body, tools, send);
            }
          } else await cpuChat(body, tools, send);
          ctrl.enqueue(enc.encode("data: [DONE]\n\n"));
          ctrl.close();
        } catch (e) {
          ctrl.error(e);
        }
      },
    }), {status: 200, headers: {"Content-Type": "text/event-stream"}});
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    if (!url.startsWith(`http://127.0.0.1:${PORT}/`)) return realFetch(input, init);
    const path = url.slice(`http://127.0.0.1:${PORT}`.length);
    if (path === "/health") {
      if (!failed) load().catch(() => {});
      return new Response(engine ? '{"status":"ok"}' : '{"status":"loading"}', {status: engine ? 200 : 503});
    }
    if (path === "/v1/chat/completions") {
      if (!engine) await load();
      return chatResponse(JSON.parse(init.body || "{}"));
    }
    return new Response("Not found", {status: 404});
  };
})();
