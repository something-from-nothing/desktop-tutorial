package main

import (
	"embed"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Your own Chrome: the "Boot.dev Dashboard Link" extension reads your normal boot.dev tab
// and sends updates here, on a fixed port that only this PC can reach.

const version = "5"
const extPort = "47822"

//go:embed extension/manifest.json extension/relay.js extension/background.js extension/icon128.png
var extFiles embed.FS

var (
	extMu   sync.Mutex
	extLast time.Time
)

func extensionDir() string { return filepath.Join(appDir(), "chrome-extension") }

// writeExtension keeps the unpacked extension folder up to date for Chrome's "Load unpacked".
func writeExtension() {
	dir := extensionDir()
	_ = os.MkdirAll(dir, 0o755)
	ents, _ := extFiles.ReadDir("extension")
	for _, e := range ents {
		b, err := extFiles.ReadFile("extension/" + e.Name())
		if err == nil {
			_ = os.WriteFile(filepath.Join(dir, e.Name()), b, 0o644)
		}
	}
	_ = os.WriteFile(filepath.Join(dir, "reader.js"), []byte(readerJS), 0o644)
}

func extActive(within time.Duration) bool {
	extMu.Lock()
	defer extMu.Unlock()
	return !extLast.IsZero() && time.Since(extLast) < within
}

// usesOwnChrome is true once the extension has sent anything in the last two weeks.
func usesOwnChrome() bool {
	if extActive(14 * 24 * time.Hour) {
		return true
	}
	t, err := time.Parse(time.RFC3339, storeGet("extSeen"))
	return err == nil && time.Since(t) < 14*24*time.Hour
}

func allowOrigin(o string) bool {
	return o == "https://www.boot.dev" || o == "https://boot.dev" || strings.HasPrefix(o, "chrome-extension://")
}

func cors(w http.ResponseWriter, r *http.Request) {
	if o := r.Header.Get("Origin"); allowOrigin(o) {
		w.Header().Set("Access-Control-Allow-Origin", o)
		w.Header().Set("Vary", "Origin")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "content-type")
		w.Header().Set("Access-Control-Allow-Private-Network", "true")
	}
}

func startExtServer() {
	mux := http.NewServeMux()
	mux.HandleFunc("/ext/ping", func(w http.ResponseWriter, r *http.Request) {
		cors(w, r)
		writeJSON(w, map[string]any{"ok": true, "app": "Boot.dev Dashboard", "version": version})
	})
	mux.HandleFunc("/ext/push", func(w http.ResponseWriter, r *http.Request) {
		cors(w, r)
		if r.Method == http.MethodOptions {
			w.WriteHeader(204)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "post only", 405)
			return
		}
		if o := r.Header.Get("Origin"); o != "" && !allowOrigin(o) {
			http.Error(w, "forbidden", 403)
			return
		}
		b, err := io.ReadAll(io.LimitReader(r.Body, 8<<20))
		if err != nil || !json.Valid(b) {
			http.Error(w, "bad json", 400)
			return
		}
		extMu.Lock()
		first := extLast.IsZero() || time.Since(extLast) > time.Hour
		extLast = time.Now()
		extMu.Unlock()
		if first {
			storeSet("extSeen", time.Now().UTC().Format(time.RFC3339))
		}
		pushEvent(b, false)
		w.WriteHeader(204)
	})
	ln, err := net.Listen("tcp", "127.0.0.1:"+extPort)
	if err != nil {
		return // another copy has it; the dashboard shows the Chrome link as not connected
	}
	_ = http.Serve(ln, mux)
}
