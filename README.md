# Desk Agent

A Windows app that lets Claude do jobs on your laptop. It can see the screen, move the mouse, type, search the web, write and run code, and use any MCP server you plug in. You type what you want done, it gets on with it.

The whole app is one file, `deskagent.py`, which builds into one file, `DeskAgent.exe`.

The thinking happens on Anthropic's servers, so you need an internet connection and an API key. Nothing big runs on the laptop itself, which is why it's fine on an Acer Aspire 5.

## Installing it

Download **DeskAgent-Setup.exe** and double click it. It installs Desk Agent for your user account (no admin password needed), puts an icon on your desktop and in the Start menu, and adds an uninstaller under Settings, Apps. After that you just double click the icon like any other app.

The full source code is installed with it, in the `source` folder inside the install folder.

**Where to get the installer:** every push to `main` builds it on GitHub. Open the repo on GitHub, go to **Actions**, click the latest **Build Windows EXE** run and download **DeskAgent-Setup** at the bottom. Unzip it to get `DeskAgent-Setup.exe`.

For a permanent link, go to **Releases**, **Draft a new release**, type a tag such as `v1.0.0` and publish. The installer is attached automatically and is always at the latest release.

Windows SmartScreen will probably warn you the first time because the installer isn't signed. Click **More info** then **Run anyway**.

**Building a portable EXE yourself instead:** install Python 3.12 from python.org (tick "Add python.exe to PATH"), put `deskagent.py`, `requirements.txt`, `icon.ico` and `build.bat` in one folder, then double click `build.bat`. You get a single `DeskAgent.exe` in the `dist` folder that runs without installing.

## First run

1. Get an API key from console.anthropic.com (you pay per use, add some credit first).
2. Open Desk Agent from your desktop. The Settings window opens. Paste the key and press Save.
3. Type a job, for example "Open Notepad and write a shopping list with milk, eggs and bread".

While it works the window minimises so Claude can see your desktop. It comes back when the job is finished.

**Emergency stop:** shove the mouse hard into any corner of the screen. You can also press Stop, which halts after the current step.

By default it asks before every click, keypress and MCP call. Once you trust a task, pick "Allow all for this session" or untick the option in Settings.

## Switching models

The box at the top left picks the model.

| Choice | Good for | Rough cost |
|---|---|---|
| Auto | Lets a quick, cheap model read each job and pick one of the four below. It tells you what it picked and why. | Adds a fraction of a penny per job |
| Fable 5.1 | The hardest jobs, big programs | Most expensive |
| Opus 5.5 | Long screen tasks, real programs, careful thinking | Middle |
| Sonnet 5.5 | Everyday jobs, small programs | Cheaper |
| Haiku 5.5 | Quick questions and simple jobs | Very cheap |

You can switch at any point, even halfway through a chat.

## Making programs

Ask it to build something, for example "make me a program that renames my holiday photos by the date they were taken". It writes the code into `Documents\DeskAgent Projects`, runs it, fixes what breaks and tells you how to start it. It can only touch files inside that folder. You can change the folder in Settings.

## How it learns

After every chat it saves the conversation in `%APPDATA%\DeskAgent\history` and updates a memory file with anything worth remembering about you: your work, the apps you use, how you like things done, what worked and what didn't. That memory goes into every new chat, and Auto uses it when choosing a model, so it gets better suited to you the more you use it.

Press **Memory** to read or edit what it knows. Untick "Learn from my conversations" in Settings to turn it off.

This is memory, not training. The model itself doesn't change.

The bar under the model box shows how many jobs you've run, since when, and roughly what you've spent.

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
| `model` | `auto`, or a model name such as `claude-sonnet-5-5`. Easier to set with the box at the top. |
| `effort` | How hard it thinks when you pick the model yourself. Auto sets its own. |
| `coding_tools` | Let it write and run code in the projects folder. |
| `workspace` | The projects folder. |
| `learn` | Keep the memory file up to date after each chat. |
| `computer_use` | Turn screen control on or off. Off means it only uses web search and MCP tools. |
| `confirm_actions` | Ask before each action. |
| `max_steps` | How many rounds it gets before pausing to check in with you. |
| `screenshot_max_width` | Screenshots are shrunk to this width before sending. Smaller is cheaper and faster. |

## Things to know

* **Cost.** Every step sends a screenshot, so a long job can cost tens of pence or more. Watch your usage on the Anthropic console for the first few days.
* **Your API key is stored in plain text** in the config file. Don't share that file.
* **It sees everything on screen.** Close anything private before giving it a job.
* **It runs commands for real.** Keep confirmations on until you trust it, and only let it run code you'd be happy running yourself.
* **It can get things wrong.** Keep confirmations on for anything that touches money, email or files you care about.
* If Claude's safety checks decline a request, the app retries on another model automatically.

## What's in deskagent.py

It's split into labelled sections, in this order: models, settings and saved data, screen control, coding tools, MCP servers, the agent, and the window.
