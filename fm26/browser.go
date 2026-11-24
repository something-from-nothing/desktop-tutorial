package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"sync"
	"time"
)

var (
	steamWinMu  sync.Mutex
	steamWinCmd *exec.Cmd
)

func findBrowser() string {
	if p := os.Getenv("FM26DASH_BROWSER"); p != "" {
		return p
	}
	if runtime.GOOS != "windows" {
		for _, c := range []string{"/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"} {
			if _, err := os.Stat(c); err == nil {
				return c
			}
		}
		return ""
	}
	// Chrome first: a fresh Edge profile pops up sync and sign in prompts.
	var cands []string
	for _, name := range []string{`Google\Chrome\Application\chrome.exe`, `Microsoft\Edge\Application\msedge.exe`} {
		for _, env := range []string{"ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"} {
			if base := os.Getenv(env); base != "" {
				cands = append(cands, filepath.Join(base, name))
			}
		}
	}
	for _, c := range cands {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	return ""
}

// layout puts the dashboard on the right of the screen.
func layout() (bootX, bootW, dashX, dashW, h int) {
	sw, sh := screenSize()
	if sw < 900 {
		sw, sh = 1366, 768
	}
	h = sh - 60
	dashW = sw * 36 / 100
	if dashW < 440 {
		dashW = 440
	}
	if dashW > 640 {
		dashW = 640
	}
	bootW = sw - dashW
	return 0, bootW, bootW, dashW, h
}

func openDefault(u string) {
	switch runtime.GOOS {
	case "windows":
		_ = exec.Command("rundll32", "url.dll,FileProtocolHandler", u).Start()
	case "darwin":
		_ = exec.Command("open", u).Start()
	default:
		_ = exec.Command("xdg-open", u).Start()
	}
}

// openDashboard shows the dashboard in its own app window. When you close it, the app exits.
func openDashboard(u string) {
	if os.Getenv("FM26DASH_NO_WINDOW") != "" {
		println(u)
		return
	}
	exe := findBrowser()
	if exe == "" {
		openDefault(u)
		return
	}
	_, _, dx, dw, h := layout()
	cmd := exec.Command(exe, "--app="+u, "--user-data-dir="+filepath.Join(appDir(), "dashboard-window"),
		"--window-position="+itoa(dx)+",0", "--window-size="+itoa(dw)+","+itoa(h),
		"--no-first-run", "--no-default-browser-check",
		"--disable-sync", "--disable-features=msImplicitSignin,msEdgeSyncConsent,EdgeSigninPrompts")
	if cmd.Start() != nil {
		openDefault(u)
		return
	}
	started := time.Now()
	_ = cmd.Wait()
	if time.Since(started) < 8*time.Second {
		// Handed to a dashboard window that was already open: keep running until that closes.
		return
	}
	os.Exit(0)
}

// openDefaultApp brings up the dashboard of an instance that is already running.
func openDefaultApp(u string) {
	exe := findBrowser()
	if exe == "" {
		openDefault(u)
		return
	}
	_ = exec.Command(exe, "--app="+u, "--user-data-dir="+filepath.Join(appDir(), "dashboard-window")).Start()
}

// openSteamWindow opens Steam's own sign in page in a separate app window with its own
// browser profile. You type your Steam password into Steam's page, never into this app.
func openSteamWindow(u string) {
	exe := findBrowser()
	if exe == "" {
		openDefault(u)
		return
	}
	closeSteamWindow()
	sw, sh := screenSize()
	w, h := 560, 780
	if sh-80 < h {
		h = sh - 80
	}
	cmd := exec.Command(exe, "--app="+u, "--user-data-dir="+filepath.Join(appDir(), "steam-signin-window"),
		"--window-position="+itoa((sw-w)/2)+",40", "--window-size="+itoa(w)+","+itoa(h),
		"--no-first-run", "--no-default-browser-check",
		"--disable-sync", "--disable-features=msImplicitSignin,msEdgeSyncConsent,EdgeSigninPrompts")
	if cmd.Start() != nil {
		openDefault(u)
		return
	}
	steamWinMu.Lock()
	steamWinCmd = cmd
	steamWinMu.Unlock()
	go func() { _ = cmd.Wait() }()
}

func closeSteamWindow() {
	steamWinMu.Lock()
	defer steamWinMu.Unlock()
	if steamWinCmd != nil && steamWinCmd.Process != nil {
		_ = steamWinCmd.Process.Kill()
	}
	steamWinCmd = nil
}

func itoa(i int) string { return strconv.Itoa(i) }
