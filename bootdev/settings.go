package main

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// Settings live in %APPDATA%\BootDevDashboard\settings.json. The boot.dev password and the
// Claude key are encrypted with Windows DPAPI, so only your Windows account on this PC can read them.
type settings struct {
	Email       string `json:"email"`
	PasswordEnc string `json:"passwordEnc,omitempty"`
	BridgeKey   string `json:"bridgeKey"`
	ClaudeEnc   string `json:"claudeEnc,omitempty"`
	Model       string `json:"model"`
	Level       string `json:"level"`
	Auto        string `json:"auto"`
}

var (
	setMu sync.Mutex
	cur   = settings{Model: "claude-sonnet-5-5", Level: "hints", Auto: "on"}
)

func settingsPath() string { return filepath.Join(appDir(), "settings.json") }

func loadSettings() {
	b, err := os.ReadFile(settingsPath())
	if err != nil {
		return
	}
	setMu.Lock()
	_ = json.Unmarshal(b, &cur)
	bk := strings.TrimSpace(cur.BridgeKey)
	setMu.Unlock()
	if strings.HasPrefix(bk, "sk-ant-") {
		// A Claude API key pasted into the LLM Chat link box: store it encrypted as the Claude key.
		_ = saveSettings(map[string]string{"bridgeKey": bk})
	}
}

func seal(plain string) (string, error) {
	if plain == "" {
		return "", nil
	}
	b, err := protect([]byte(plain))
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(b), nil
}

func unseal(enc string) string {
	if enc == "" {
		return ""
	}
	b, err := base64.StdEncoding.DecodeString(enc)
	if err != nil {
		return ""
	}
	p, err := unprotect(b)
	if err != nil {
		return ""
	}
	return string(p)
}

func saveSettings(in map[string]string) error {
	setMu.Lock()
	defer setMu.Unlock()
	next := cur
	if v, ok := in["email"]; ok {
		next.Email = v
	}
	if v, ok := in["bridgeKey"]; ok {
		next.BridgeKey = strings.TrimSpace(v)
	}
	if strings.HasPrefix(next.BridgeKey, "sk-ant-") {
		// That's a Claude API key, not an LLM Chat link key: keep it encrypted as the Claude key.
		if in["claudeKey"] == "" {
			in["claudeKey"] = next.BridgeKey
		}
		next.BridgeKey = ""
	}
	if v := in["model"]; v != "" {
		next.Model = v
	}
	if v := in["level"]; v != "" {
		next.Level = v
	}
	if v := in["auto"]; v != "" {
		next.Auto = v
	}
	if v := in["password"]; v != "" {
		s, err := seal(v)
		if err != nil {
			return err
		}
		next.PasswordEnc = s
	}
	if v := in["claudeKey"]; v != "" {
		s, err := seal(v)
		if err != nil {
			return err
		}
		next.ClaudeEnc = s
	}
	b, _ := json.MarshalIndent(next, "", "  ")
	if err := os.WriteFile(settingsPath(), b, 0o600); err != nil {
		return err
	}
	cur = next
	return nil
}

func publicSettings() map[string]any {
	setMu.Lock()
	defer setMu.Unlock()
	return map[string]any{
		"email": cur.Email, "hasPassword": cur.PasswordEnc != "", "bridgeKey": cur.BridgeKey,
		"hasClaudeKey": cur.ClaudeEnc != "", "model": cur.Model, "level": cur.Level, "auto": cur.Auto,
	}
}

func savedLogin() (string, string) {
	setMu.Lock()
	defer setMu.Unlock()
	return cur.Email, unseal(cur.PasswordEnc)
}

func snapshotSettings() settings {
	setMu.Lock()
	defer setMu.Unlock()
	return cur
}

// ---- A small key/value store for the dashboard (stats, weak topics, chats) ----

var (
	stMu  sync.Mutex
	store = map[string]string{}
)

func storePath() string { return filepath.Join(appDir(), "store.json") }

func loadStore() {
	b, err := os.ReadFile(storePath())
	if err != nil {
		return
	}
	stMu.Lock()
	_ = json.Unmarshal(b, &store)
	stMu.Unlock()
}

func storeGet(k string) string {
	stMu.Lock()
	defer stMu.Unlock()
	return store[k]
}

func storeSet(k, v string) {
	if k == "" {
		return
	}
	stMu.Lock()
	if v == "" {
		delete(store, k)
	} else {
		store[k] = v
	}
	b, _ := json.Marshal(store)
	stMu.Unlock()
	tmp := storePath() + ".tmp"
	if os.WriteFile(tmp, b, 0o600) == nil {
		_ = os.Rename(tmp, storePath())
	}
}
