package main

import (
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
)

// Settings live in %APPDATA%\FC26Dashboard\settings.json. The Steam Web API key and the Claude
// key are encrypted with Windows DPAPI, so only your Windows account on this PC can read them.
// There is no Steam password anywhere: Steam sign in happens on Steam's own page.
type pairedDevice struct {
	Name      string `json:"name"`
	TokenHash string `json:"tokenHash"`
	Added     int64  `json:"added"`
	LastSeen  int64  `json:"lastSeen"`
}

type settings struct {
	SteamID     string         `json:"steamId"`
	SteamName   string         `json:"steamName"`
	SteamKeyEnc string         `json:"steamKeyEnc,omitempty"`
	BridgeKey   string         `json:"bridgeKey"`
	ClaudeEnc   string         `json:"claudeEnc,omitempty"`
	Model       string         `json:"model"`
	Auto        string         `json:"auto"`            // "off" (default) or "on"
	AutoGap     int            `json:"autoGap"`         // seconds between automatic explanations
	AutoPerHour int            `json:"autoPerHour"`     // most automatic explanations in an hour
	PhoneLink   string         `json:"phoneLinkSwitch"` // "on" (default): the phone link runs whenever the app does
	Trial       string         `json:"trial"`           // "on": show the EA Play trial countdown
	TrialHours  int            `json:"trialHours"`      // length of the trial (10 hours for FC 26)
	Devices     []pairedDevice `json:"devices,omitempty"`
}

var (
	setMu sync.Mutex
	cur   = defaultSettings()
)

func defaultSettings() settings {
	return settings{Model: "claude-sonnet-5-5", Auto: "off", AutoGap: 90, AutoPerHour: 12, PhoneLink: "on", Trial: "on", TrialHours: 10}
}

func settingsPath() string { return filepath.Join(appDir(), "settings.json") }

func loadSettings() {
	b, err := os.ReadFile(settingsPath())
	if err != nil {
		return
	}
	setMu.Lock()
	_ = json.Unmarshal(b, &cur)
	clampSettings(&cur)
	setMu.Unlock()
}

func clampSettings(s *settings) {
	if s.AutoGap < 30 {
		s.AutoGap = 30
	}
	if s.AutoGap > 1800 {
		s.AutoGap = 1800
	}
	if s.AutoPerHour < 1 {
		s.AutoPerHour = 1
	}
	if s.AutoPerHour > 60 {
		s.AutoPerHour = 60
	}
	if s.Auto != "on" {
		s.Auto = "off"
	}
	if s.Model == "" {
		s.Model = "claude-sonnet-5-5"
	}
	if s.PhoneLink != "off" {
		s.PhoneLink = "on"
	}
	if s.Trial != "off" {
		s.Trial = "on"
	}
	if s.TrialHours < 1 || s.TrialHours > 100 {
		s.TrialHours = 10
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

func writeSettings(next settings) error {
	clampSettings(&next)
	b, _ := json.MarshalIndent(next, "", "  ")
	tmp := settingsPath() + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, settingsPath()); err != nil {
		return err
	}
	cur = next
	return nil
}

// updateSettings changes settings under the lock.
func updateSettings(fn func(*settings) error) error {
	setMu.Lock()
	defer setMu.Unlock()
	next := cur
	next.Devices = append([]pairedDevice(nil), cur.Devices...)
	if err := fn(&next); err != nil {
		return err
	}
	return writeSettings(next)
}

// saveSettings takes what the Settings screen sends. fromPhone limits it to harmless choices.
func saveSettings(in map[string]string, fromPhone bool) error {
	return updateSettings(func(next *settings) error {
		if v := in["model"]; v != "" {
			next.Model = v
		}
		if v := in["auto"]; v != "" {
			next.Auto = v
		}
		if v, err := strconv.Atoi(in["autoGap"]); err == nil {
			next.AutoGap = v
		}
		if v, err := strconv.Atoi(in["autoPerHour"]); err == nil {
			next.AutoPerHour = v
		}
		if v := in["trial"]; v != "" {
			next.Trial = v
		}
		if v, err := strconv.Atoi(in["trialHours"]); err == nil {
			next.TrialHours = v
		}
		if fromPhone {
			return nil
		}
		if v := in["phoneLink"]; v == "on" || v == "off" {
			next.PhoneLink = v
		}
		if v, ok := in["bridgeKey"]; ok {
			next.BridgeKey = strings.TrimSpace(v)
		}
		if strings.HasPrefix(next.BridgeKey, "sk-ant-") {
			// A Claude API key pasted into the LLM Chat link box: keep it encrypted as the Claude key.
			if in["claudeKey"] == "" {
				in["claudeKey"] = next.BridgeKey
			}
			next.BridgeKey = ""
		}
		if v := strings.TrimSpace(in["claudeKey"]); v != "" {
			s, err := seal(v)
			if err != nil {
				return err
			}
			next.ClaudeEnc = s
		}
		if v := strings.TrimSpace(in["steamKey"]); v != "" {
			s, err := seal(v)
			if err != nil {
				return err
			}
			next.SteamKeyEnc = s
		}
		if in["steamSignOut"] == "1" {
			next.SteamID, next.SteamName = "", ""
		}
		if in["forgetSteamKey"] == "1" {
			next.SteamKeyEnc = ""
		}
		return nil
	})
}

func publicSettings() map[string]any {
	setMu.Lock()
	defer setMu.Unlock()
	devs := []map[string]any{}
	for _, d := range cur.Devices {
		devs = append(devs, map[string]any{"name": d.Name, "added": d.Added, "lastSeen": d.LastSeen})
	}
	return map[string]any{
		"steamId": cur.SteamID, "steamName": cur.SteamName, "hasSteamKey": cur.SteamKeyEnc != "",
		"bridgeKey": cur.BridgeKey, "hasClaudeKey": cur.ClaudeEnc != "", "model": cur.Model,
		"auto": cur.Auto, "autoGap": cur.AutoGap, "autoPerHour": cur.AutoPerHour, "trial": cur.Trial, "trialHours": cur.TrialHours,
		"phoneLink": cur.PhoneLink, "devices": devs,
	}
}

func snapshotSettings() settings {
	setMu.Lock()
	defer setMu.Unlock()
	s := cur
	s.Devices = append([]pairedDevice(nil), cur.Devices...)
	return s
}

// ---- A small key/value store for the dashboard (weak topics, club, explanations, chats) ----

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
	stWrite.Lock()
	defer stWrite.Unlock()
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

var stWrite sync.Mutex

func storeJSON(k string, v any) {
	b, err := json.Marshal(v)
	if err == nil {
		storeSet(k, string(b))
	}
}

func storeLoad(k string, v any) bool {
	s := storeGet(k)
	return s != "" && json.Unmarshal([]byte(s), v) == nil
}
