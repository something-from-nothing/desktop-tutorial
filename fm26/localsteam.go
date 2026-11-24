package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Steam's own files on this PC. These work without the Web API and without the internet.
// Read only: nothing here writes to Steam's folders.

const (
	demoAppID     = 3551360 // Football Manager 26 Demo (checked on SteamDB, 24 Nov 2026)
	fullAppID     = 3551340 // Football Manager 26
	steamID64Base = 76561197960265728
)

var demoAppStr = strconv.Itoa(demoAppID)

type localSteam struct {
	SteamPath    string `json:"steamPath,omitempty"`
	Found        bool   `json:"found"`        // Steam itself was found
	Running      bool   `json:"running"`      // the demo is running right now (registry)
	RunningKnown bool   `json:"runningKnown"` // the registry could be read
	Installed    bool   `json:"installed"`    // appmanifest says fully installed
	InstallDir   string `json:"installDir,omitempty"`
	Name         string `json:"name,omitempty"`
	LastPlayed   int64  `json:"lastPlayed,omitempty"`  // unix seconds, from localconfig.vdf
	PlaytimeMin  int    `json:"playtimeMin,omitempty"` // minutes, from localconfig.vdf
	Playtime2wk  int    `json:"playtime2wk,omitempty"` // minutes, last two weeks
	AccountID    uint32 `json:"accountId,omitempty"`
	Error        string `json:"error,omitempty"`
}

// libraryPaths lists every Steam library folder from libraryfolders.vdf, plus Steam's own folder.
func libraryPaths(steamPath string) []string {
	out := []string{steamPath}
	b, err := os.ReadFile(filepath.Join(steamPath, "steamapps", "libraryfolders.vdf"))
	if err != nil {
		return out
	}
	root, err := parseVDF(string(b))
	if err != nil {
		return out
	}
	lf := root.Get("libraryfolders")
	if lf == nil {
		return out
	}
	seen := map[string]bool{strings.ToLower(filepath.Clean(steamPath)): true}
	for _, c := range lf.Children {
		p := c.Node.Str("path")
		if p == "" && c.Node.Value != "" {
			p = c.Node.Value // very old format: "1" "D:\\Games"
		}
		if p == "" {
			continue
		}
		k := strings.ToLower(filepath.Clean(p))
		if !seen[k] {
			seen[k] = true
			out = append(out, p)
		}
	}
	return out
}

// readManifest finds appmanifest_<appid>.acf in any library.
func readManifest(steamPath string, appid string) (lib string, m *vdfNode) {
	for _, l := range libraryPaths(steamPath) {
		b, err := os.ReadFile(filepath.Join(l, "steamapps", "appmanifest_"+appid+".acf"))
		if err != nil {
			continue
		}
		root, err := parseVDF(string(b))
		if err != nil {
			continue
		}
		if st := root.Get("AppState"); st != nil {
			return l, st
		}
	}
	return "", nil
}

// gameDir is the demo's install folder, used to recognise its window.
func gameDir(steamPath string) string {
	lib, m := readManifest(steamPath, demoAppStr)
	if m == nil || m.Str("installdir") == "" {
		return ""
	}
	return filepath.Join(lib, "steamapps", "common", m.Str("installdir"))
}

// userConfigs returns localconfig.vdf paths: the given account first, then any others.
func userConfigs(steamPath string, account uint32) []string {
	var out []string
	if account != 0 {
		out = append(out, filepath.Join(steamPath, "userdata", strconv.FormatUint(uint64(account), 10), "config", "localconfig.vdf"))
	}
	ents, _ := os.ReadDir(filepath.Join(steamPath, "userdata"))
	for _, e := range ents {
		if !e.IsDir() || (account != 0 && e.Name() == strconv.FormatUint(uint64(account), 10)) {
			continue
		}
		out = append(out, filepath.Join(steamPath, "userdata", e.Name(), "config", "localconfig.vdf"))
	}
	return out
}

func readLocalSteam(steamPath string, account uint32) localSteam {
	ls := localSteam{SteamPath: steamPath, AccountID: account}
	if steamPath == "" {
		ls.Error = "Steam wasn't found on this PC."
		return ls
	}
	if _, err := os.Stat(steamPath); err != nil {
		ls.Error = "Steam's folder isn't where Windows says it is."
		return ls
	}
	ls.Found = true
	if _, m := readManifest(steamPath, demoAppStr); m != nil {
		ls.Name = m.Str("name")
		ls.InstallDir = m.Str("installdir")
		flags, _ := strconv.Atoi(m.Str("StateFlags"))
		ls.Installed = flags&4 != 0
		if t, _ := strconv.ParseInt(m.Str("LastPlayed"), 10, 64); t > 0 {
			ls.LastPlayed = t
		}
	}
	for _, p := range userConfigs(steamPath, account) {
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		root, err := parseVDF(string(b))
		if err != nil {
			continue
		}
		app := root.Path("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", demoAppStr)
		if app == nil {
			app = root.Path("UserLocalConfigStore", "Software", "Valve", "Steam", "Apps", demoAppStr)
		}
		if app == nil {
			continue
		}
		if t, _ := strconv.ParseInt(app.Str("LastPlayed"), 10, 64); t > ls.LastPlayed {
			ls.LastPlayed = t
		}
		ls.PlaytimeMin, _ = strconv.Atoi(app.Str("Playtime"))
		ls.Playtime2wk, _ = strconv.Atoi(app.Str("Playtime2wks"))
		if ls.AccountID == 0 {
			if id, err := strconv.ParseUint(filepath.Base(filepath.Dir(filepath.Dir(p))), 10, 32); err == nil {
				ls.AccountID = uint32(id)
			}
		}
		break
	}
	return ls
}

func accountFromSteamID(id string) uint32 {
	n, err := strconv.ParseUint(id, 10, 64)
	if err != nil || n < steamID64Base {
		return 0
	}
	return uint32(n - steamID64Base)
}

func unixOrZero(t int64) time.Time {
	if t <= 0 {
		return time.Time{}
	}
	return time.Unix(t, 0)
}
