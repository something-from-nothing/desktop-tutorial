# Desk Agent

A Windows app that lets Claude do jobs on your laptop. It can see the screen, move the mouse, type, search the web and use any MCP server you plug in. You type what you want done, it gets on with it.

The thinking happens on Anthropic's servers, so you need an internet connection and an API key. Nothing big runs on the laptop itself, which is why it's fine on an Acer Aspire 5.

## Getting the EXE

**Easiest:** every push to `main` builds `DeskAgent.exe` on GitHub. Open the repo on GitHub, go to **Actions**, click the latest **Build Windows EXE** run and download the **DeskAgent** file at the bottom. Unzip it and you have the EXE.

To get a proper download link, push a tag such as `v1.0.0` and the EXE gets attached to a GitHub Release.

**Building it yourself on the laptop:** install Python 3.12 from python.org (tick "Add python.exe to PATH"), download this repo, then double click `build.bat`. The EXE ends up in the `dist` folder.

Windows SmartScreen will probably warn you the first time because the EXE isn't signed. Click **More info** then **Run anyway**.

## First run

1. Get an API key from console.anthropic.com (you pay per use, add some credit first).
2. Open DeskAgent.exe. The Settings window opens. Paste the key and press Save.
3. Type a job, for example "Open Notepad and write a shopping list with milk, eggs and bread".

While it works the window minimises so Claude can see your desktop. It comes back when the job is finished.

**Emergency stop:** shove the mouse hard into any corner of the screen. You can also press Stop, which halts after the current step.

By default it asks before every click, keypress and MCP call. Once you trust a task, pick "Allow all for this session" or untick the option in Settings.

## Adding MCP servers

Press Settings, then **Open config file**. MCP servers go in the `mcpServers` block, in the same format Claude Desktop uses, so you can copy entries straight across. Press Save in Settings afterwards to reconnect.

```json
"mcpServers": {
  "files": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\YOU\\Documents"]
  },
  "my-remote-server": {
    "url": "https://example.com/mcp"
  }
}
```

Servers started with `npx` need Node.js installed. If a server fails to start, the app tells you, and the details are in `%APPDATA%\DeskAgent\mcp_servers.log`.

## Settings

All settings live in `%APPDATA%\DeskAgent\config.json`.

| Setting | What it does |
|---|---|
| `model` | Which Claude model to use. `claude-opus-5-5` is the default. `claude-sonnet-5-5` is cheaper, `claude-haiku-5-5` is much cheaper but less capable at driving the screen. |
| `effort` | How hard it thinks. `medium` is fine for most jobs, raise it for fiddly ones. |
| `computer_use` | Turn screen control on or off. Off means it only uses web search and MCP tools. |
| `confirm_actions` | Ask before each action. |
| `max_steps` | How many rounds it gets before pausing to check in with you. |
| `screenshot_max_width` | Screenshots are shrunk to this width before sending. Smaller is cheaper and faster. |

## Things to know

* **Cost.** Every step sends a screenshot, so a long job can cost tens of pence or more. Watch your usage on the Anthropic console for the first few days.
* **Your API key is stored in plain text** in the config file. Don't share that file.
* **It sees everything on screen.** Close anything private before giving it a job.
* **It can get things wrong.** Keep confirmations on for anything that touches money, email or files you care about.
* If Claude's safety checks decline a request, the app retries on another model automatically.

## Code layout

| File | Job |
|---|---|
| `main.py` | Starts the app and sets up logging |
| `deskagent/gui.py` | The chat window, settings and confirm dialogs |
| `deskagent/agent.py` | The loop that talks to Claude and runs the tools it asks for |
| `deskagent/computer.py` | Screenshots, mouse and keyboard |
| `deskagent/mcp_bridge.py` | Connects to MCP servers and passes their tools to Claude |
| `deskagent/config.py` | Loads and saves settings |
