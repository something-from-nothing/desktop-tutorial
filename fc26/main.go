// FC26 Dashboard for Windows.
//
// A live dashboard beside EA SPORTS FC 26. Two feeds: Steam from the outside (is it running,
// playtime, last played, achievements; from Steam's own files on this PC and the Steam Web
// API) and the game screen itself (capture of the FC 26 window only, explained by Claude
// through LLM Chat). It never reads or changes the game's memory or save files, never injects
// anything into the game, and never touches EA's anti cheat.
package main

import (
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

//go:embed web/dashboard.html
var dashboardHTML []byte

const version = "1.0.0"

var token string

func appDir() string {
	if d := os.Getenv("FC26DASH_DIR"); d != "" {
		_ = os.MkdirAll(d, 0o700)
		return d
	}
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = "."
	}
	d := filepath.Join(dir, "FC26Dashboard")
	_ = os.MkdirAll(d, 0o700)
	return d
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

var stateSeq atomic.Int64

func bumpSeq() { stateSeq.Add(1) }

// ---- local Steam (this PC) ----

var (
	localMu   sync.Mutex
	localLast localSteam
)

func localSnapshot() localSteam {
	localMu.Lock()
	defer localMu.Unlock()
	return localLast
}

// watchLoop checks every 5 seconds whether FC 26 is running, and re-reads Steam's files
// every minute (or when the game stops, so the new playtime shows up).
func watchLoop() {
	var lastRead time.Time
	wasRunning := false
	for {
		s := snapshotSettings()
		regRunning, known := gameRunningRegistry()
		src := "registry"
		isRunning := regRunning
		if !known {
			src = "steam api"
			isRunning = steamSnapshot().Playing
		}
		observe(time.Now(), isRunning, src)
		if time.Since(lastRead) > time.Minute || isRunning != wasRunning {
			acct := activeSteamUser()
			if acct == 0 {
				acct = accountFromSteamID(s.SteamID)
			}
			ls := readLocalSteam(steamPath(), acct)
			ls.Running, ls.RunningKnown = regRunning, known
			localMu.Lock()
			localLast = ls
			localMu.Unlock()
			lastRead = time.Now()
			if isRunning != wasRunning {
				refreshSteamNow()
			}
		}
		if isRunning != wasRunning {
			bumpSeq()
		}
		wasRunning = isRunning
		time.Sleep(5 * time.Second)
	}
}

// ---- the dashboard's view of everything ----

func stateFor(local bool) map[string]any {
	now := time.Now()
	s := snapshotSettings()
	clubMu.Lock()
	cs := clubLoad()
	clubMu.Unlock()
	exMu.Lock()
	ex := map[string]any{"busy": exBusy, "autoNote": autoNote, "autoLastHour": autoLimit.countLastHour(now)}
	if exLatest != nil {
		ex["latest"] = exLatest
	}
	hist := exHistory
	if len(hist) > 6 {
		hist = hist[:6]
	}
	ex["history"] = hist
	exMu.Unlock()
	st := map[string]any{
		"version": version, "seq": stateSeq.Load(), "now": now.Unix(), "pc": hostName(),
		"steam": steamSnapshot(), "local": localSnapshot(), "session": sessionSnapshot(now),
		"club": cs, "explain": ex,
		"trial": trialOf(s, steamSnapshot(), localSnapshot(), sessionSnapshot(now), cs, now),
		"weak":  weakRanked(), "ai": aiStatus(), "platform": platformName(), "hotkey": hotkeyStatus(),
		"steamSignedIn": s.SteamID != "", "steamName": s.SteamName, "hasSteamKey": s.SteamKeyEnc != "",
	}
	if local {
		st["phone"] = pairingInfo()
	}
	return st
}

// api adds the routes both the dashboard window and the phone use.
func api(mux *http.ServeMux, auth func(*http.Request) bool, fromPhone bool) {
	h := func(path string, fn http.HandlerFunc) {
		mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
			if !auth(r) {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			fn(w, r)
		})
	}
	h("/api/state", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, stateFor(!fromPhone)) })
	h("/api/settings", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var in map[string]string
			if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&in); err != nil {
				writeJSON(w, map[string]any{"ok": false, "error": "bad request"})
				return
			}
			if err := saveSettings(in, fromPhone); err != nil {
				writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			if in["steamKey"] != "" || in["steamSignOut"] == "1" {
				refreshSteamNow()
			}
			bumpSeq()
			writeJSON(w, map[string]any{"ok": true})
			return
		}
		ps := publicSettings()
		if fromPhone {
			delete(ps, "bridgeKey")
			delete(ps, "devices")
		}
		writeJSON(w, ps)
	})
	h("/api/store", func(w http.ResponseWriter, r *http.Request) {
		k := r.URL.Query().Get("k")
		if !strings.HasPrefix(k, "ui:") {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		if r.Method == http.MethodPost {
			var in struct {
				V string `json:"v"`
			}
			_ = json.NewDecoder(io.LimitReader(r.Body, 2<<20)).Decode(&in)
			storeSet(k, in.V)
			writeJSON(w, map[string]bool{"ok": true})
			return
		}
		writeJSON(w, map[string]string{"v": storeGet(k)})
	})
	h("/api/ai", func(w http.ResponseWriter, r *http.Request) {
		var req aiRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&req); err != nil {
			writeJSON(w, aiResult{OK: false, Error: "bad request"})
			return
		}
		if hasImage(req) {
			// Only the app itself sends pictures, and only of the game window.
			writeJSON(w, aiResult{OK: false, Error: "pictures can't be sent this way"})
			return
		}
		writeJSON(w, askAI(req))
	})
	h("/api/explain", func(w http.ResponseWriter, r *http.Request) {
		rec, err := explainNow(false)
		if err == errBusy {
			writeJSON(w, map[string]any{"ok": false, "error": "Already explaining. One moment."})
			return
		}
		writeJSON(w, map[string]any{"ok": err == nil, "record": rec})
	})
	h("/api/ask", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Q string `json:"q"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&in)
		a, err := askAboutScreen(in.Q)
		if err != nil {
			writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, map[string]any{"ok": true, "text": a})
	})
	h("/api/weak", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var in struct {
				Topic string `json:"topic"`
				Kind  string `json:"kind"`
				Note  string `json:"note"`
				Reset bool   `json:"reset"`
			}
			_ = json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&in)
			if in.Reset && !fromPhone {
				weakMu.Lock()
				storeSet("weak", "")
				weakMu.Unlock()
			} else {
				weakBump(in.Topic, in.Kind, in.Note)
			}
			bumpSeq()
		}
		writeJSON(w, weakRanked())
	})
	h("/api/steam/refresh", func(w http.ResponseWriter, r *http.Request) {
		refreshSteamNow()
		writeJSON(w, map[string]bool{"ok": true})
	})
	h("/api/status", func(w http.ResponseWriter, r *http.Request) {
		st := map[string]any{"version": version}
		for k, v := range aiStatus() {
			st[k] = v
		}
		writeJSON(w, st)
	})
}

func localRoutes(mux *http.ServeMux, base string) {
	authed := func(r *http.Request) bool {
		return token != "" && (r.Header.Get("X-Token") == token || r.URL.Query().Get("t") == token)
	}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" || !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(dashboardHTML)
	})
	api(mux, authed, false)

	// Steam sign in: open Steam's own page; Steam sends the browser back here.
	mux.HandleFunc("/api/steam/signin", func(w http.ResponseWriter, r *http.Request) {
		if !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		n := make([]byte, 16)
		_, _ = rand.Read(n)
		nonce := hex.EncodeToString(n)
		ret := base + "/steam/return?n=" + nonce
		signinMu.Lock()
		signinNonces[nonce] = ret
		signinMu.Unlock()
		openSteamWindow(openIDStart(ret, base+"/"))
		writeJSON(w, map[string]bool{"ok": true})
	})
	mux.HandleFunc("/steam/return", func(w http.ResponseWriter, r *http.Request) {
		nonce := r.URL.Query().Get("n")
		signinMu.Lock()
		want, ok := signinNonces[nonce]
		delete(signinNonces, nonce)
		signinMu.Unlock()
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if !ok {
			fmt.Fprint(w, signinPage("This sign in link has already been used. Go back to the dashboard and press Sign in with Steam again.", false))
			return
		}
		id, err := openIDVerify(r.URL.Query(), want)
		if err != nil {
			fmt.Fprint(w, signinPage("Steam sign in didn't work: "+err.Error()+". Close this window and try again.", false))
			return
		}
		_ = updateSettings(func(s *settings) error { s.SteamID = id; return nil })
		go func() {
			// Fill in the Steam name once the key is there.
			time.Sleep(2 * time.Second)
			refreshSteamNow()
			time.Sleep(15 * time.Second)
			if st := steamSnapshot(); st.Persona != "" {
				_ = updateSettings(func(s *settings) error { s.SteamName = st.Persona; return nil })
			}
		}()
		bumpSeq()
		fmt.Fprint(w, signinPage("Signed in with Steam. You can close this window.", true))
		go func() { time.Sleep(2500 * time.Millisecond); closeSteamWindow() }()
	})
	mux.HandleFunc("/api/phone", func(w http.ResponseWriter, r *http.Request) {
		if !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var in struct {
			Do string `json:"do"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&in)
		switch in.Do {
		case "pair":
			if err := startLAN(lanHandler()); err != nil {
				writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			newPairCode()
		case "forget":
			forgetPhones()
		}
		bumpSeq()
		writeJSON(w, map[string]any{"ok": true, "phone": pairingInfo()})
	})
	mux.HandleFunc("/api/open", func(w http.ResponseWriter, r *http.Request) {
		if !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		var in struct {
			What string `json:"what"`
		}
		_ = json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&in)
		switch in.What {
		case "apikey":
			openDefault("https://steamcommunity.com/dev/apikey")
		case "privacy":
			openDefault("https://steamcommunity.com/my/edit/settings")
		case "store":
			openDefault("https://store.steampowered.com/app/" + strconv.Itoa(mainAppID))
		case "folder":
			showFolder(appDir())
		}
		writeJSON(w, map[string]bool{"ok": true})
	})
}

var (
	signinMu     sync.Mutex
	signinNonces = map[string]string{}
)

func signinPage(msg string, ok bool) string {
	col := "#ff6b6b"
	if ok {
		col = "#4fd18b"
	}
	return `<!DOCTYPE html><html><head><meta charset="utf-8"><title>FC26 Dashboard</title></head><body style="background:#0f1117;color:#e8e9ee;font:16px Segoe UI,system-ui,sans-serif;display:grid;place-items:center;height:90vh"><div style="max-width:440px;text-align:center"><div style="font-size:40px;color:` + col + `">` + map[bool]string{true: "✓", false: "!"}[ok] + `</div><p>` + htmlEsc(msg) + `</p></div></body></html>`
}

func htmlEsc(s string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;").Replace(s)
}

// lanHandler serves the phone: the same API, with the phone's token instead of the window's.
func lanHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/pair", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || !privateIP(remoteIP(r.RemoteAddr)) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		handlePair(w, r)
	})
	api(mux, phoneAuthed, true)
	return mux
}

func main() {
	if maybeInstall() {
		return // the installed copy has been started
	}
	tb := make([]byte, 16)
	_, _ = rand.Read(tb)
	token = hex.EncodeToString(tb)

	// Already running? Show that dashboard instead of starting a second copy.
	inst := filepath.Join(appDir(), "instance.txt")
	if b, err := os.ReadFile(inst); err == nil {
		u := strings.TrimSpace(string(b))
		if resp, err := quick.Get(strings.Replace(u, "/?t=", "/api/status?t=", 1)); err == nil {
			var st struct {
				Version string `json:"version"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&st)
			resp.Body.Close()
			if resp.StatusCode == 200 && st.Version == version {
				openDefaultApp(u)
				return
			}
		}
	}
	killOldCopies(appDir())

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fatal("Could not start: " + err.Error())
	}
	base := "http://" + ln.Addr().String()
	url := base + "/?t=" + token
	_ = os.WriteFile(inst, []byte(url), 0o600)
	loadSettings()
	loadStore()
	loadSessions()
	loadExplains()
	loadSteamCache()
	mux := http.NewServeMux()
	localRoutes(mux, base)
	if snapshotSettings().PhoneLink == "on" && len(snapshotSettings().Devices) > 0 {
		_ = startLAN(lanHandler())
	}
	go ensureShortcut()
	go watchLoop()
	go steamLoop()
	go autoLoop()
	go startHotkey(hotkeyPressed)
	go openDashboard(url)
	_ = http.Serve(ln, mux)
}
