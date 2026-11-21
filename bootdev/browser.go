package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	bootMu   sync.Mutex
	bootCmd  *exec.Cmd
	starting bool
)

func findBrowser() string {
	if p := os.Getenv("BOOTDASH_BROWSER"); p != "" {
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

// layout splits the screen: boot.dev on the left, the dashboard on the right.
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

// readDevPort reads the port the browser chose for its debugging connection.
func readDevPort(profile string) int {
	b, err := os.ReadFile(filepath.Join(profile, "DevToolsActivePort"))
	if err != nil {
		return 0
	}
	p, _ := strconv.Atoi(strings.TrimSpace(strings.SplitN(string(b), "\n", 2)[0]))
	return p
}

func startBootDevWindow(url string) {
	bootMu.Lock()
	if starting {
		bootMu.Unlock()
		return
	}
	starting = true
	bootMu.Unlock()
	defer func() { bootMu.Lock(); starting = false; bootMu.Unlock() }()

	profile := filepath.Join(appDir(), "bootdev-profile")
	_ = os.MkdirAll(profile, 0o700)

	// Already open from an earlier start? Reuse it.
	if p := readDevPort(profile); p != 0 {
		if _, err := listTargets(p); err == nil {
			setDevPort(p)
			if len(liveConns()) > 0 {
				openBootDev(url)
			}
			return
		}
	}
	_ = os.Remove(filepath.Join(profile, "DevToolsActivePort"))

	exe := findBrowser()
	if exe == "" {
		openDefault(url)
		return
	}
	bx, bw, _, _, h := layout()
	args := []string{
		"--app=" + url,
		"--user-data-dir=" + profile,
		"--remote-debugging-port=0",
		"--remote-allow-origins=http://127.0.0.1",
		"--no-first-run", "--no-default-browser-check",
		"--disable-sync", "--disable-features=msImplicitSignin,msEdgeSyncConsent,EdgeSigninPrompts",
		"--window-position=" + itoa(bx) + ",0",
		"--window-size=" + itoa(bw) + "," + itoa(h),
	}
	if os.Getenv("BOOTDASH_HEADLESS") != "" {
		args = append(args, "--headless=new")
	}
	cmd := exec.Command(exe, args...)
	if err := cmd.Start(); err != nil {
		openDefault(url)
		return
	}
	bootMu.Lock()
	bootCmd = cmd
	bootMu.Unlock()
	go func() { _ = cmd.Wait() }()
	for i := 0; i < 80; i++ {
		if p := readDevPort(profile); p != 0 {
			setDevPort(p)
			return
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func closeBootDevWindow() {
	for _, c := range liveConns() {
		_, _ = c.call("Browser.close", map[string]any{}, 2*time.Second)
	}
	bootMu.Lock()
	if bootCmd != nil && bootCmd.Process != nil {
		_ = bootCmd.Process.Kill()
	}
	bootMu.Unlock()
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

// openDashboard shows the dashboard in its own app window. When you close it, the app
// closes the boot.dev window too and exits.
func openDashboard(u string) {
	if os.Getenv("BOOTDASH_NO_WINDOW") != "" {
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
	closeBootDevWindow()
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

// chromeExe finds Google Chrome only (the extension is for Chrome).
func chromeExe() string {
	for _, env := range []string{"ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"} {
		if base := os.Getenv(env); base != "" {
			c := filepath.Join(base, `Google\Chrome\Application\chrome.exe`)
			if _, err := os.Stat(c); err == nil {
				return c
			}
		}
	}
	return ""
}

func openChromePage(u string) {
	if exe := chromeExe(); exe != "" {
		_ = exec.Command(exe, u).Start()
	}
}

// closeOldCopies closes an older version of this app and the windows it opened.
func closeOldCopies() {
	profile := filepath.Join(appDir(), "bootdev-profile")
	if p := readDevPort(profile); p != 0 {
		if ts, err := listTargets(p); err == nil {
			for _, t := range ts {
				if t.Type == "page" && t.WSURL != "" {
					if ws, err := wsDial(t.WSURL, 3*time.Second); err == nil {
						_ = ws.WriteJSON(map[string]any{"id": 1, "method": "Browser.close", "params": map[string]any{}})
						time.Sleep(300 * time.Millisecond)
						ws.Close()
					}
					break
				}
			}
		}
	}
	killOldCopies(appDir())
}
