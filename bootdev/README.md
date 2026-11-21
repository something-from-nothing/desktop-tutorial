# Boot.dev Dashboard

boot.dev in its own window, with a live dashboard beside it: level, level progress, streak,
course progress, the current lesson explained by AI, weak topics and quick quizzes.

* `web/` the dashboard page and the boot.dev reader, shared by both apps
* `*.go` the Windows app (no outside dependencies)
* `android/` the Android app
* Built by `.github/workflows/build-bootdev.yml`; downloads on the `bootdev-dashboard` release

AI answers come from LLM Chat over its local link (Windows: `127.0.0.1:47821/bridge/v1`,
Android: content provider `app.llmchat.bridge`), or from Claude directly with a backup key.
The reader never sends boot.dev's own solution files anywhere.
