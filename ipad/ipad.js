// Desk Agent for iPad. The app page (the same index.html as the phone) talks to an AI
// engine at http://127.0.0.1:PORT, the way it does on Desk OS. On iPad there is no
// engine program, so this answers those requests itself, running the model inside
// Safari with WebLLM (WebGPU). It loads before the app's own script.
(() => {
  const PORT = "1";
  const MODELS = {f16: "Qwen2.5-1.5B-Instruct-q4f16_1-MLC", f32: "Qwen2.5-1.5B-Instruct-q4f32_1-MLC",
                  small: "Qwen2.5-0.5B-Instruct-q4f32_1-MLC"};
  const CONTEXT_CHARS = 9000;  // the model holds 4096 tokens; leave room for the answer
  // Tools that work in a browser. Web search can't (sites block other pages reading them).
  const IPAD_TOOLS = ["ask_user", "set_reminder", "create_artifact"];
  let engine = null, loading = null, failed = null;

  const status = t => { try { window.setStatus && setStatus(t); } catch (e) {} };

  async function pickModel() {
    if (!navigator.gpu) throw new Error("This iPad's Safari doesn't have WebGPU switched on. Update iPadOS, then try again.");
    const adapter = await navigator.gpu.requestAdapter();
    if (!adapter) throw new Error("Safari couldn't start the graphics chip for the AI.");
    return adapter.features.has("shader-f16") ? MODELS.f16 : MODELS.f32;
  }
  function load() {
    if (engine || loading) return loading;
    loading = (async () => {
      // Its own copy first; the public CDN if that copy can't be loaded.
      const webllm = await import("./webllm.js").catch(() => import("https://cdn.jsdelivr.net/npm/@mlc-ai/web-llm@0.2.85/lib/index.js"));
      const progress = p => status(p.progress < 1 ? `Getting the AI ready: ${Math.round(p.progress * 100)}%. The first time downloads about 1 GB.` : "");
      let model = await pickModel();
      try {
        engine = await webllm.CreateMLCEngine(model, {initProgressCallback: progress});
      } catch (e) {
        // Not enough memory for the 1.5B model: fall back to the small one.
        status("Trying a smaller AI that fits this iPad...");
        model = MODELS.small;
        engine = await webllm.CreateMLCEngine(model, {initProgressCallback: progress});
      }
      status("");
    })().catch(e => {
      failed = e;
      status("The AI couldn't start on this iPad: " + (e.message || e));
      throw e;
    });
    return loading;
  }

  // ---- turning the app's tool requests into Qwen's own tool format, and back

  function toolsPrompt(tools) {
    return "\n\n# Tools\n\nYou may call one or more functions to assist with the user query.\n\n" +
      "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n" +
      tools.map(t => JSON.stringify(t)).join("\n") +
      "\n</tools>\n\nFor each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n" +
      '<tool_call>\n{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>';
  }
  function toEngineMessages(messages, tools) {
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
    // Keep the newest messages that fit the model's memory (the system message stays).
    const sys = out[0]?.role === "system" ? out.shift() : null;
    let total = sys ? sys.content.length : 0, keep = [];
    for (let i = out.length - 1; i >= 0; i--) {
      total += out[i].content.length;
      if (total > CONTEXT_CHARS && keep.length) break;
      keep.unshift(out[i]);
    }
    while (keep.length && keep[0].role !== "user") keep.shift();
    return sys ? [sys, ...keep] : keep;
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

  // ---- the stand in for the engine's web API

  const sse = obj => `data: ${JSON.stringify(obj)}\n\n`;
  function chatResponse(body) {
    const tools = (body.tools || []).filter(t => IPAD_TOOLS.includes(t.function?.name));
    const messages = toEngineMessages(body.messages || [], tools);
    const enc = new TextEncoder();
    const stream = new ReadableStream({
      async start(ctrl) {
        const send = obj => ctrl.enqueue(enc.encode(sse(obj)));
        try {
          await load();
          const chunks = await engine.chat.completions.create({messages, stream: true, max_tokens: Math.min(body.max_tokens || 1500, 1500), temperature: 0.6});
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
          const calls = parseToolCalls(text);
          if (calls.length) {
            send({choices: [{index: 0, delta: {tool_calls: calls.map((c, i) => ({index: i, id: "call_" + Math.random().toString(36).slice(2, 10), type: "function", function: c}))}}]});
            finish = "tool_calls";
          } else if (shown < text.length) send({choices: [{index: 0, delta: {content: text.slice(shown)}}]});
          send({choices: [{index: 0, delta: {}, finish_reason: finish}]});
          ctrl.enqueue(enc.encode("data: [DONE]\n\n"));
        } catch (e) {
          ctrl.error(e);
        }
        ctrl.close();
      },
    });
    return new Response(stream, {status: 200, headers: {"Content-Type": "text/event-stream"}});
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
