// Boot.dev Dashboard for Windows.
//
// Opens boot.dev in its own window (its own browser profile, so you sign in once) and a
// dashboard window beside it. A reader script inside the boot.dev window passes what it sees
// (lesson, code, level, XP, streak, chapters, submissions) to this app through the browser's
// local debugging port. The dashboard asks LLM Chat (or Claude directly, as a backup) to explain.
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
	"time"
)

//go:embed web/dashboard.html
var dashboardHTML []byte

//go:embed web/reader.js
var readerJS string

var startURL = envOr("BOOTDASH_START", "https://www.boot.dev/dashboard")

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

var token string

func appDir() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = "."
	}
	d := filepath.Join(dir, "BootDevDashboard")
	_ = os.MkdirAll(d, 0o700)
	return d
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

func authed(r *http.Request) bool {
	return token != "" && (r.Header.Get("X-Token") == token || r.URL.Query().Get("t") == token)
}

func handle(path string, fn http.HandlerFunc) {
	http.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
		if !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		fn(w, r)
	})
}

// ---- Events from the reader ----

type event struct {
	Seq int64           `json:"-"`
	Raw json.RawMessage `json:"-"`
}

var (
	evMu       sync.Mutex
	evSeq      int64
	evRing     []event
	latestPage json.RawMessage
	lessonData = map[string]json.RawMessage{}
	lessonKeys []string
)

func pushEvent(raw []byte, ownWindow bool) {
	var head struct {
		Type    string `json:"type"`
		ID      string `json:"id"`
		Login   bool   `json:"login"`
		URL     string `json:"url"`
		Visible *bool  `json:"visible"`
		Level   *int   `json:"level"`
	}
	if json.Unmarshal(raw, &head) != nil || head.Type == "" {
		return
	}
	// Only the boot.dev tab you are looking at counts as "where you are".
	if head.Type == "page" && ((head.Visible != nil && !*head.Visible) || !isBootDevURL(head.URL)) {
		return
	}
	if !ownWindow && head.Type == "page" && extActive(time.Minute) {
		go switchToOwnChrome()
	}
	cp := json.RawMessage(append([]byte(nil), raw...))
	evMu.Lock()
	evSeq++
	evRing = append(evRing, event{Seq: evSeq, Raw: cp})
	if len(evRing) > 600 {
		evRing = evRing[len(evRing)-500:]
	}
	switch head.Type {
	case "page":
		latestPage = cp
	case "lesson_data":
		if _, ok := lessonData[head.ID]; !ok {
			lessonKeys = append(lessonKeys, head.ID)
		}
		lessonData[head.ID] = cp
		if len(lessonKeys) > 30 {
			delete(lessonData, lessonKeys[0])
			lessonKeys = lessonKeys[1:]
		}
	}
	evMu.Unlock()
	// Signed out in the app's own boot.dev window? Sign in with the saved email and password.
	if ownWindow && head.Type == "page" && head.Level == nil {
		go func() {
			time.Sleep(3 * time.Second) // let the page finish loading first
			signIn(false)
		}()
	}
}

func routes() {
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" || !authed(r) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write(dashboardHTML)
	})
	handle("/api/events", func(w http.ResponseWriter, r *http.Request) {
		since, _ := strconv.ParseInt(r.URL.Query().Get("since"), 10, 64)
		evMu.Lock()
		out := []json.RawMessage{}
		seq := evSeq
		if since <= 0 {
			// A fresh dashboard: start from what is known now.
			for _, k := range lessonKeys {
				out = append(out, lessonData[k])
			}
			if latestPage != nil {
				out = append(out, latestPage)
			}
		} else {
			seq = since
			for _, e := range evRing {
				if e.Seq <= since {
					continue
				}
				if len(out) >= 300 {
					break
				}
				out = append(out, e.Raw)
				seq = e.Seq
			}
		}
		evMu.Unlock()
		writeJSON(w, map[string]any{"events": out, "seq": seq})
	})
	handle("/api/settings", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var in map[string]string
			if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&in); err != nil {
				writeJSON(w, map[string]any{"ok": false, "error": "bad request"})
				return
			}
			if err := saveSettings(in); err != nil {
				writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
				return
			}
			writeJSON(w, map[string]any{"ok": true})
			return
		}
		writeJSON(w, publicSettings())
	})
	handle("/api/store", func(w http.ResponseWriter, r *http.Request) {
		k := r.URL.Query().Get("k")
		if r.Method == http.MethodPost {
			var in struct {
				V string `json:"v"`
			}
			_ = json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&in)
			storeSet(k, in.V)
			writeJSON(w, map[string]bool{"ok": true})
			return
		}
		writeJSON(w, map[string]string{"v": storeGet(k)})
	})
	handle("/api/ai", func(w http.ResponseWriter, r *http.Request) {
		var req aiRequest
		if err := json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(&req); err != nil {
			writeJSON(w, aiResult{OK: false, Error: "bad request"})
			return
		}
		writeJSON(w, askAI(req))
	})
	handle("/api/status", func(w http.ResponseWriter, r *http.Request) {
		st := map[string]any{"version": version, "ext": extActive(3 * time.Minute), "ownChrome": usesOwnChrome(),
			"window": len(liveConns()) > 0, "extPath": extensionDir()}
		for k, v := range aiStatus() {
			st[k] = v
		}
		writeJSON(w, st)
	})
	handle("/api/signin", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, signIn(true))
	})
	handle("/api/chrome", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Do string `json:"do"`
		}
		_ = json.NewDecoder(r.Body).Decode(&in)
		switch in.Do {
		case "folder":
			showFolder(extensionDir())
		case "extensions":
			openChromePage("chrome://extensions")
		}
		writeJSON(w, map[string]bool{"ok": true})
	})
	handle("/api/open", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			URL string `json:"url"`
		}
		_ = json.NewDecoder(r.Body).Decode(&in)
		if in.URL == "" {
			in.URL = startURL
		}
		openBootDev(in.URL)
		writeJSON(w, map[string]bool{"ok": true})
	})
}

func main() {
	tb := make([]byte, 16)
	_, _ = rand.Read(tb)
	token = hex.EncodeToString(tb)

	// Already running? Show that dashboard instead of starting a second copy.
	// An older version is closed (with its windows) and replaced by this one.
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
	closeOldCopies()
	writeExtension()
	go startExtServer()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		fatal("Could not start: " + err.Error())
	}
	_ = os.WriteFile(inst, []byte(fmt.Sprintf("http://%s/?t=%s", ln.Addr().String(), token)), 0o600)
	loadSettings()
	loadStore()
	routes()
	go ensureShortcut()
	if !usesOwnChrome() || os.Getenv("BOOTDASH_START") != "" {
		go startBootDevWindow(startURL)
	}
	go cdpLoop()
	go openDashboard(fmt.Sprintf("http://%s/?t=%s", ln.Addr().String(), token))
	_ = http.Serve(ln, nil)
}

func isBootDevURL(u string) bool {
	return strings.HasPrefix(u, "https://www.boot.dev/") || strings.HasPrefix(u, "https://boot.dev/") || sameHost(u, startURL)
}

var switchOnce sync.Once

// switchToOwnChrome closes the app's own boot.dev window the first time your Chrome connects,
// so there is only one boot.dev to look at.
func switchToOwnChrome() {
	switchOnce.Do(func() {
		if os.Getenv("BOOTDASH_START") != "" {
			return
		}
		for _, c := range liveConns() {
			_, _ = c.call("Browser.close", map[string]any{}, 2*time.Second)
			return
		}
	})
}
