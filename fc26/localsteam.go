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

// EA SPORTS FC 26 has more than one Steam listing, so the game is recognised by these IDs or by
// its name. 3405690 is the main one (SteamDB, checked 24 Nov 2026); 3629260 is the free
// Showcase (only sold in a few countries). Anything else named "EA SPORTS FC 26" counts too.
const (
	mainAppID     = 3405690
	showcaseAppID = 3629260
	steamID64Base = 76561197960265728
)

var knownApps = map[int]bool{mainAppID: true, 4328110: true, showcaseAppID: true}

var mainAppStr = strconv.Itoa(mainAppID)

// isGameName matches "EA SPORTS FC™ 26", "EA SPORTS FC 26 SHOWCASE" and so on, but not FC 25.
func isGameName(name string) bool {
	n := strings.ToUpper(strings.NewReplacer("™", "", "®", "").Replace(name))
	n = strings.Join(strings.Fields(n), " ")
	return strings.Contains(n, "EA SPORTS FC 26") || n == "FC 26"
}

func isGameApp(id int, name string) bool { return knownApps[id] || isGameName(name) }

type localSteam struct {
	SteamPath    string `json:"steamPath,omitempty"`
	Found        bool   `json:"found"`           // Steam itself was found
	AppID        int    `json:"appId,omitempty"` // which FC 26 listing is installed
	Running      bool   `json:"running"`         // the game is running right now (registry)
	RunningKnown bool   `json:"runningKnown"`    // the registry could be read
	Installed    bool   `json:"installed"`       // appmanifest says fully installed
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

// findGame looks through every library's appmanifest files for FC 26. A fully installed copy
// wins over one that is still downloading.
func findGame(steamPath string) (lib string, m *vdfNode, appid int) {
	for _, l := range libraryPaths(steamPath) {
		files, _ := filepath.Glob(filepath.Join(l, "steamapps", "appmanifest_*.acf"))
		for _, f := range files {
			b, err := os.ReadFile(f)
			if err != nil {
				continue
			}
			root, err := parseVDF(string(b))
			if err != nil {
				continue
			}
			st := root.Get("AppState")
			if st == nil {
				continue
			}
			id, _ := strconv.Atoi(st.Str("appid"))
			if !isGameApp(id, st.Str("name")) {
				continue
			}
			flags, _ := strconv.Atoi(st.Str("StateFlags"))
			if m == nil || (flags&4 != 0 && id != showcaseAppID) {
				lib, m, appid = l, st, id
			}
		}
	}
	return
}

// gameDir is the game's install folder, used to recognise its window.
func gameDir(steamPath string) string {
	lib, m, _ := findGame(steamPath)
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
	if _, m, id := findGame(steamPath); m != nil {
		ls.AppID = id
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
		apps := root.Path("UserLocalConfigStore", "Software", "Valve", "Steam", "apps")
		var app *vdfNode
		if apps != nil {
			for _, c := range apps.Children {
				id, _ := strconv.Atoi(c.Key)
				if (ls.AppID != 0 && id == ls.AppID) || (ls.AppID == 0 && knownApps[id]) {
					app = c.Node
					if ls.AppID == 0 {
						ls.AppID = id
					}
					break
				}
			}
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
