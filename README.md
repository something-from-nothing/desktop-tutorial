# Desk Agent

Your own AI that runs on your laptop. No account, no API key, no monthly bill, and nothing you type leaves the machine. Once a model is downloaded it works with the internet switched off.

It can chat, write and run programs, open apps, files and websites, type into windows, search the web, and use any MCP server you plug in. You can switch between models or leave it on Auto. It keeps a memory of what it learns about you and gets more useful the more you use it.

The whole app is one file, `deskagent.py`. The AI itself runs through [llama.cpp](https://github.com/ggml-org/llama.cpp), which comes bundled in the installer.

## Installing it

Download **DeskAgent-Setup.exe** and double click it. It installs for your user account (no admin password needed), puts an icon on your desktop and in the Start menu, and adds an uninstaller under Settings, Apps.

**Where to get the installer:** every push to `main` builds it on GitHub. Open the repo on GitHub, go to **Actions**, click the latest **Build Windows EXE** run and download **DeskAgent-Setup** at the bottom. Unzip it to get `DeskAgent-Setup.exe`.

For a permanent link, go to **Releases**, **Draft a new release**, type a tag such as `v2.0.0` and publish. The installer is attached automatically.

Windows SmartScreen will probably warn you the first time because the installer isn't signed. Click **More info** then **Run anyway**.

## First run

1. Open Desk Agent from your desktop. The Models window opens.
2. Press **Download** next to **General**. It's about 2.5 GB, so give it a while.
3. Close the Models window and type a job, for example "Open Notepad and write a shopping list with milk, eggs and bread".

The first reply after starting takes a few seconds longer while the model loads into memory.

## Models

| Model | Size | Good for |
|---|---|---|
| Quick (Qwen 2.5, 1.5B) | 1 GB | Fast chat and simple questions |
| General (Qwen 3, 4B) | 2.5 GB | Everyday jobs and using tools. Start here. |
| Coder (Qwen 2.5 Coder, 7B) | 4.7 GB | Writing programs. Slower, wants 8 GB of RAM or more |

**Auto** picks one for each job: Coder for anything that looks like programming, Quick for short questions at the start of a chat, General for everything else. It only picks from models you've downloaded.

You can also add any other model in GGUF format with **Models**, **Add my own .gguf file**. Hugging Face has thousands.

Models are stored in `%LOCALAPPDATA%\DeskAgent\models`.

## Be realistic about what it can do

A model that fits on a laptop is much smaller than ChatGPT or Claude. Expect it to:

* be good at chat, writing, explaining things and small to medium programs
* make more mistakes on long multi step jobs, so check its work
* type and use shortcuts, but **not see the screen**. It finds windows by their titles and types into them blind
* run at roughly reading speed on a normal laptop. Bigger models are slower

## Making programs

Ask it to build something, for example "make me a program that renames my holiday photos by the date they were taken". It writes the code into `Documents\DeskAgent Projects`, runs it, and tells you how to start it. It can only touch files inside that folder. To run Python programs it makes, install Python from python.org.

## How it learns

After every chat it saves the conversation in `%APPDATA%\DeskAgent\history` and asks the model to update a memory file with anything worth remembering about you: your work, the apps you use, how you like things done. That memory goes into every new chat, so it fits you better the more you use it.

Press **Memory** to read or edit what it knows. Untick "Learn from my conversations" in Settings to turn it off.

This is memory, not retraining. Retraining the model itself would need far more computing power than a laptop has.

## Adding MCP servers

Press **Settings**, then **Open config file**. MCP servers go in the `mcpServers` block, in the same format Claude Desktop uses. Press Save in Settings afterwards to reconnect.

```json
"mcpServers": {
  "files": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\YOU\\Documents"]
  }
}
```

Servers started with `npx` need Node.js installed.

## Settings

All settings live in `%APPDATA%\DeskAgent\config.json`.

| Setting | What it does |
|---|---|
| `model` | `auto`, or a model name such as `general` |
| `context` | How much of the chat the model can hold at once. 8192 is a good balance; bigger uses more RAM |
| `threads` | CPU threads to use. 0 lets llama.cpp decide |
| `coding_tools` | Let it write and run code in the projects folder |
| `desktop_tools` | Let it open apps and type into windows |
| `web_tools` | Let it search the web and read pages |
| `confirm_actions` | Ask before it runs commands, types, opens things or changes files |
| `learn` | Keep the memory file up to date after each chat |
| `max_steps` | How many tool steps it gets before checking in with you |
| `workspace` | The projects folder |

**Emergency stop:** shove the mouse into any corner of the screen while it's typing, or press Stop.

## Building it yourself

Install Python 3.12 from python.org (tick "Add python.exe to PATH"), put `deskagent.py`, `requirements.txt`, `icon.ico` and `build.bat` in one folder, and double click `build.bat`. It builds the app and downloads the llama.cpp engine into `dist\DeskAgent`. Keep that whole folder together and run `DeskAgent.exe` from it.

## What's in deskagent.py

It's split into labelled sections, in this order: models, settings and saved data, downloading models, the local AI engine, tools, MCP servers, the agent, and the window.
