# FM26 Dashboard

A live dashboard beside the Football Manager 26 Demo on Steam (app 3551360), in the same style as the Boot.dev Dashboard.

* `*.go` the Windows app (no outside dependencies)
* `web/dashboard.html` the dashboard page, shared by the Windows and Android apps
* `android/` the Android app: a second screen that pairs with the PC
* `testdata/` sample Steam files and made up sample screens used by the tests
* `LLM_Chat_image_link_prompt.md` the change LLM Chat needs before it can take screen pictures
* Built by `.github/workflows/build-fm26.yml`; downloads on the `fm26_dashboard` release

## Where the data comes from

| Panel | Source |
|---|---|
| Running, session time | Steam's `RunningAppID` in the registry (HKCU\Software\Valve\Steam), or the Web API's `gameid` |
| Total playtime, last played | Steam Web API `GetOwnedGames` (with `include_played_free_games`), else `localconfig.vdf` on this PC, else the app's own log |
| Installed | `appmanifest_3551360.acf` in any Steam library |
| Streak | the app's own session log (Steam doesn't track streaks) |
| Demo progress, My club | read from screen pictures by Claude (FM26 has no print to file) |
| Weak topics, quizzes | your questions, problems Claude spots on screen, and quiz answers |

## What it never does

* Read or change the game's memory or save files, or inject anything into the game.
* Ask for, see or store your Steam password (sign in is on Steam's own page).
* Send your desktop anywhere: only the Football Manager window is captured.
* Send the Steam Web API key or Claude key to the AI, or write them to logs.
* Open anything to the internet. The phone link answers home network addresses only.
