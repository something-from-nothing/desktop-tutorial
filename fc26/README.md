# FC26 Dashboard

A live dashboard beside EA SPORTS FC 26 on Steam (main app 3405690; any listing named EA SPORTS FC 26 counts), in the same style as the Boot.dev Dashboard. It replaces the earlier FM26 Dashboard.

Opening the EXE installs it to %LOCALAPPDATA%\Programs\FC26 Dashboard with desktop and Start menu shortcuts, and adds it to Settings, Apps, Installed apps (uninstall from there). Settings stay in %APPDATA%\FC26Dashboard.

* `*.go` the Windows app (no outside dependencies)
* `web/dashboard.html` the dashboard page, shared by the Windows and Android apps
* `android/` the Android app: a second screen that pairs with the PC
* `testdata/` sample Steam files and made up sample screens used by the tests
* `LLM_Chat_image_link_prompt.md` the change LLM Chat needs before it can take screen pictures
* Built by `.github/workflows/build-fc26.yml`; downloads on the `fc26_dashboard` release

## Where the data comes from

| Panel | Source |
|---|---|
| Running, session time | Steam's `RunningAppID` in the registry (HKCU\Software\Valve\Steam), or the Web API's `gameid` |
| Total playtime, last played | Steam Web API `GetOwnedGames` (with `include_played_free_games`), else `localconfig.vdf` on this PC, else the app's own log |
| Installed | the FC 26 `appmanifest_*.acf` in any Steam library |
| Streak | the app's own session log (Steam doesn't track streaks) |
| Achievements | Steam Web API `GetPlayerAchievements` |
| My club (Ultimate Team and Career Mode) | read from screen pictures by Claude |
| Weak topics, quizzes | your questions, problems Claude spots on screen, and quiz answers |

## What it never does

* Read or change the game's memory or save files, or inject anything into the game.
* Ask for, see or store your Steam password (sign in is on Steam's own page).
* Send your desktop anywhere: only the FC 26 window is captured.
* Touch EA Javelin Anticheat or the EA app.
* Send the Steam Web API key or Claude key to the AI, or write them to logs.
* Open anything to the internet. The phone link answers home network addresses only.
