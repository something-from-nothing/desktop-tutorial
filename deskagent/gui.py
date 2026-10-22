"""Tkinter chat window. The agent runs on a worker thread; all widget updates go
through a queue that the Tk main loop drains, because Tk is not thread safe."""

import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from . import config
from .agent import Agent

log = logging.getLogger(__name__)

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


class App:
    def __init__(self, errlog):
        self.errlog = errlog
        self.cfg = config.load()
        self.agent: Agent | None = None
        self.busy = False
        self.events: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("Desk Agent")
        self.root.geometry("760x620")
        self.root.minsize(480, 360)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self._build()
        self.root.after(50, self._drain)

        if not self.cfg["api_key"] and not os.environ.get("ANTHROPIC_API_KEY"):
            self.root.after(200, self.open_settings)
        else:
            self._start_agent()

    # layout

    def _build(self):
        top = ttk.Frame(self.root, padding=(8, 6))
        top.pack(fill="x")
        self.status = ttk.Label(top, text="Starting...")
        self.status.pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="New chat", command=self.new_chat).pack(side="right", padx=4)

        self.log = scrolledtext.ScrolledText(self.root, wrap="word", state="disabled", font=("Segoe UI", 10), padx=8, pady=8)
        self.log.pack(fill="both", expand=True, padx=8)
        self.log.tag_config("you", foreground="#1a4fa0", font=("Segoe UI", 10, "bold"))
        self.log.tag_config("claude", foreground="#202020")
        self.log.tag_config("progress", foreground="#707070", font=("Segoe UI", 9, "italic"))
        self.log.tag_config("tool", foreground="#8a6d00", font=("Consolas", 9))
        self.log.tag_config("error", foreground="#b00020")

        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill="x")
        self.entry = tk.Text(bottom, height=3, wrap="word", font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", self._on_enter)
        buttons = ttk.Frame(bottom)
        buttons.pack(side="right", padx=(6, 0))
        self.send_btn = ttk.Button(buttons, text="Send", command=self.send)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(fill="x", pady=(4, 0))

        self._write("Type a job below, for example: \"Open Notepad and write a shopping list\".\n"
                    "Emergency stop: shove the mouse into any corner of the screen.\n\n", "progress")

    def _write(self, text, tag):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # agent lifecycle

    def _start_agent(self):
        if self.agent:
            old, self.agent = self.agent, None
            threading.Thread(target=old.close, daemon=True).start()
        self.status.config(text="Connecting to MCP servers...")
        self.agent = Agent(self.cfg, self, self.errlog)

        def work():
            tools, failed = self.agent.start()
            self.events.put(("ready", (len(tools), failed)))

        threading.Thread(target=work, daemon=True).start()

    def _on_enter(self, event):
        if not event.state & 0x1:  # Shift+Enter makes a new line
            self.send()
            return "break"

    def send(self):
        text = self.entry.get("1.0", "end").strip()
        if not text or self.busy or not self.agent:
            return
        self.entry.delete("1.0", "end")
        self._write(f"You: {text}\n\n", "you")
        self._set_busy(True)
        if self.cfg["computer_use"]:
            # Get out of the way so Claude sees the desktop, not this window.
            self.root.iconify()

        def work():
            try:
                self.agent.run(text)
            except Exception as e:
                log.exception("agent crashed")
                self.events.put(("say", (f"Something went wrong: {e}", "error")))
            self.events.put(("done", None))

        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        if self.agent:
            self.agent.stop_event.set()
            self.status.config(text="Stopping after the current step...")

    def new_chat(self):
        if self.busy or not self.agent:
            return
        self.agent.reset()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy):
        self.busy = busy
        self.send_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.status.config(text="Working..." if busy else self._ready_text)

    _ready_text = "Ready"

    # callbacks from the worker thread

    def say(self, text, kind="claude"):
        self.events.put(("say", (text.strip() + "\n\n", kind)))

    def progress(self, text):
        self.events.put(("say", (text.strip() + "\n", "progress")))

    def tool(self, text):
        self.events.put(("say", (f"> {text}\n", "tool")))

    def confirm(self, description) -> bool:
        answer = {}
        done = threading.Event()
        self.events.put(("confirm", (description, answer, done)))
        done.wait()
        return answer.get("ok", False)

    def _drain(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "say":
                    self._write(*data)
                elif kind == "ready":
                    count, failed = data
                    self._ready_text = f"Ready  |  {count} MCP tools"
                    if failed:
                        self._ready_text += f"  |  failed: {', '.join(failed)}"
                        self._write("Some MCP servers did not start:\n" +
                                    "".join(f"  {k}: {v}\n" for k, v in failed.items()) +
                                    f"Details in {config.app_dir() / 'deskagent.log'}\n\n", "error")
                    if not self.busy:
                        self.status.config(text=self._ready_text)
                elif kind == "confirm":
                    description, answer, done = data
                    answer["ok"] = self._ask(description)
                    done.set()
                elif kind == "done":
                    self._set_busy(False)
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        self.root.after(50, self._drain)

    def _ask(self, description) -> bool:
        win = tk.Toplevel(self.root)
        win.title("Allow this action?")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        result = {"ok": False}
        ttk.Label(win, text="Claude wants to:", padding=(12, 12, 12, 4)).pack(anchor="w")
        ttk.Label(win, text=description, wraplength=420, padding=(12, 0, 12, 12), font=("Consolas", 9)).pack(anchor="w")
        row = ttk.Frame(win, padding=12)
        row.pack(fill="x")

        def close(ok):
            result["ok"] = ok
            win.destroy()

        def allow_all():
            self.cfg["confirm_actions"] = False
            close(True)

        ttk.Button(row, text="Allow", command=lambda: close(True)).pack(side="left")
        ttk.Button(row, text="Allow all for this session", command=allow_all).pack(side="left", padx=6)
        ttk.Button(row, text="Deny", command=lambda: close(False)).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", lambda: close(False))
        win.update_idletasks()
        x = (win.winfo_screenwidth() - win.winfo_width()) // 2
        y = (win.winfo_screenheight() - win.winfo_height()) // 3
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_force()
        self.root.wait_window(win)
        return result["ok"]

    # settings

    def open_settings(self):
        if self.busy:
            return
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)

        key = tk.StringVar(value=self.cfg["api_key"])
        model = tk.StringVar(value=self.cfg["model"])
        effort = tk.StringVar(value=self.cfg["effort"])
        computer = tk.BooleanVar(value=self.cfg["computer_use"])
        web = tk.BooleanVar(value=self.cfg["web_search"])
        confirm = tk.BooleanVar(value=self.cfg["confirm_actions"])

        ttk.Label(frm, text="Anthropic API key (console.anthropic.com)").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Entry(frm, textvariable=key, show="*", width=52).grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))
        ttk.Label(frm, text="Model").grid(row=2, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=model, values=MODELS, width=24).grid(row=2, column=1, sticky="w")
        ttk.Label(frm, text="Effort").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(frm, textvariable=effort, values=EFFORTS, state="readonly", width=24).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(frm, text="Let Claude control the mouse and keyboard", variable=computer).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(frm, text="Ask me before each click, keypress or MCP call", variable=confirm).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(frm, text="Allow web search", variable=web).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(frm, text="MCP servers are set in the config file (same format as Claude Desktop).",
                  wraplength=380).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12, 2))
        ttk.Button(frm, text="Open config file", command=self._open_config).grid(row=8, column=0, sticky="w")

        def save():
            self.cfg = config.load()  # pick up any hand edits to mcpServers
            self.cfg.update(api_key=key.get().strip(), model=model.get().strip(), effort=effort.get(),
                            computer_use=computer.get(), web_search=web.get(), confirm_actions=confirm.get())
            config.save(self.cfg)
            win.destroy()
            self._start_agent()

        ttk.Button(frm, text="Save", command=save).grid(row=9, column=1, sticky="e", pady=(14, 0))
        win.grab_set()

    def _open_config(self):
        path = config.config_path()
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", str(path)])
        messagebox.showinfo("Desk Agent", "After editing, press Save in Settings to reconnect the MCP servers.", parent=self.root)

    def quit(self):
        if self.agent:
            self.agent.stop_event.set()
            threading.Thread(target=self.agent.close, daemon=True).start()
        self.root.after(300, self.root.destroy)

    def run(self):
        self.root.mainloop()
