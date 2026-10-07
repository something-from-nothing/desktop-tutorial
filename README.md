# Prompt mods

My personal prompt mods for Claude, split into pieces I can switch on where I need them.

## The mods

| Mod | What it does |
|---|---|
| advisor | Challenges first, uncomfortable truth first, no warm up, structured disagreement |
| confidence | Tags claims as [Certain], [Likely] or [Guessing] |
| humanise | No hyphens or dashes in prose, UK English, strips AI tells |
| holdground | Only changes position on genuinely new information |
| promptmods | All four at once |

## Where each piece goes

**Every chat on claude.ai (always on)**
Settings, Profile, "What personal preferences should Claude consider". Paste the contents of `project-instructions/full-stack.txt`.

**A specific Project (always on for that project)**
Open the project, Project instructions, paste `project-instructions/full-stack.txt`. Do this once per project.

**Switchable in any chat or project**
Settings, Capabilities, Skills, Upload skill. Upload each zip from `dist/`. Then say "advisor mode", "humanise this" or "full mods" and Claude picks the right one.

**Claude Code in this repo**
Already wired. `CLAUDE.md` loads the full stack automatically and the skills in `.claude/skills` work as /advisor, /humanise and so on.

**Claude Code everywhere else**
Copy the folders in `.claude/skills` into `~/.claude/skills` on your machine, and copy the text of `full-stack.txt` into `~/.claude/CLAUDE.md`.

## Editing a mod

1. Edit the `SKILL.md` file in `.claude/skills/<mod>/`
2. Run `./scripts/build.sh` to rebuild the zips
3. Upload the new zip on claude.ai (it replaces the old one)

The `---` lines at the top of each `SKILL.md` are required by Claude to read the file. They are not prose and never show up in replies.
