package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Steam sign in uses Steam's own OpenID page. This app never sees, asks for or stores your
// Steam password: you type it into Steam's page, and Steam tells this app only your Steam ID.
// The Web API key is yours, stored encrypted, and never written to logs or sent to the AI.

var (
	steamOpenID = "https://steamcommunity.com/openid/login"
	steamAPI    = "https://api.steampowered.com"
	steamHTTP   = &http.Client{Timeout: 15 * time.Second}
)

var claimedRe = regexp.MustCompile(`^https://steamcommunity\.com/openid/id/(\d{17})$`)

// openIDStart builds the address of Steam's sign in page.
func openIDStart(returnTo, realm string) string {
	q := neturl.Values{}
	q.Set("openid.ns", "http://specs.openid.net/auth/2.0")
	q.Set("openid.mode", "checkid_setup")
	q.Set("openid.return_to", returnTo)
	q.Set("openid.realm", realm)
	q.Set("openid.identity", "http://specs.openid.net/auth/2.0/identifier_select")
	q.Set("openid.claimed_id", "http://specs.openid.net/auth/2.0/identifier_select")
	return steamOpenID + "?" + q.Encode()
}

// openIDVerify checks Steam's answer with Steam itself (check_authentication) and returns the
// 64 bit Steam ID. Nothing is trusted from the browser alone.
func openIDVerify(q neturl.Values, expectReturn string) (string, error) {
	if q.Get("openid.mode") != "id_res" {
		if q.Get("openid.mode") == "cancel" {
			return "", errors.New("sign in was cancelled")
		}
		return "", errors.New("Steam didn't finish signing in")
	}
	if rt := q.Get("openid.return_to"); rt != expectReturn {
		return "", errors.New("Steam's answer was for a different sign in")
	}
	if ep := q.Get("openid.op_endpoint"); ep != "https://steamcommunity.com/openid/login" {
		return "", errors.New("the answer didn't come from Steam")
	}
	m := claimedRe.FindStringSubmatch(q.Get("openid.claimed_id"))
	if m == nil {
		return "", errors.New("Steam didn't send a Steam ID")
	}
	check := neturl.Values{}
	for k, v := range q {
		if strings.HasPrefix(k, "openid.") {
			check[k] = v
		}
	}
	check.Set("openid.mode", "check_authentication")
	resp, err := steamHTTP.PostForm(steamOpenID, check)
	if err != nil {
		return "", errors.New("couldn't reach Steam to confirm the sign in")
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if !regexp.MustCompile(`(?m)^is_valid\s*:\s*true\s*$`).Match(b) {
		return "", errors.New("Steam said the sign in wasn't valid")
	}
	return m[1], nil
}

// ---- Web API ----

type steamErr struct {
	Status int
	Msg    string
}

func (e *steamErr) Error() string { return e.Msg }

// redact removes the key from anything that might be shown or logged (Go puts the full
// address, key included, into network error messages).
func redact(s, key string) string {
	if key != "" {
		s = strings.ReplaceAll(s, key, "<key>")
		s = strings.ReplaceAll(s, neturl.QueryEscape(key), "<key>")
	}
	return regexp.MustCompile(`([?&]key=)[^&\s"]+`).ReplaceAllString(s, "${1}<key>")
}

func steamGet(ctx context.Context, key, path string, q neturl.Values, out any) error {
	q.Set("key", key)
	q.Set("format", "json")
	req, _ := http.NewRequestWithContext(ctx, "GET", steamAPI+path+"?"+q.Encode(), nil)
	resp, err := steamHTTP.Do(req)
	if err != nil {
		return &steamErr{0, "couldn't reach Steam: " + redact(err.Error(), key)}
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode >= 400 {
		// Some errors carry a reason in the body, e.g. {"playerstats":{"error":"Profile is not public"}}.
		_ = json.Unmarshal(b, out)
		if bytes.Contains(b, []byte("not public")) {
			return &steamErr{resp.StatusCode, "your Steam profile is private"}
		}
	}
	switch {
	case resp.StatusCode == 401 || resp.StatusCode == 403:
		return &steamErr{resp.StatusCode, "Steam rejected the Web API key. Check it on steamcommunity.com/dev/apikey."}
	case resp.StatusCode == 429:
		return &steamErr{429, "Steam says too many requests. It will try again shortly."}
	case resp.StatusCode >= 400:
		return &steamErr{resp.StatusCode, fmt.Sprintf("Steam returned HTTP %d", resp.StatusCode)}
	}
	if err := json.Unmarshal(b, out); err != nil {
		return &steamErr{resp.StatusCode, "Steam sent something unexpected"}
	}
	return nil
}

type playerSummary struct {
	SteamID       string `json:"steamid"`
	PersonaName   string `json:"personaname"`
	Avatar        string `json:"avatarmedium"`
	Visibility    int    `json:"communityvisibilitystate"` // 3 = public
	PersonaState  int    `json:"personastate"`
	GameID        string `json:"gameid"`
	GameExtraInfo string `json:"gameextrainfo"`
	LastLogoff    int64  `json:"lastlogoff"`
	ProfileState  int    `json:"profilestate"`
}

type ownedGame struct {
	AppID       int    `json:"appid"`
	Name        string `json:"name"`
	Playtime    int    `json:"playtime_forever"`
	Playtime2wk int    `json:"playtime_2weeks"`
	LastPlayed  int64  `json:"rtime_last_played"`
}

type achievement struct {
	APIName     string `json:"apiname"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Achieved    int    `json:"achieved"`
	UnlockTime  int64  `json:"unlocktime"`
}

// steamStats is everything the Web API says about you and the demo.
type steamStats struct {
	At           int64             `json:"at"`
	Persona      string            `json:"persona,omitempty"`
	Avatar       string            `json:"avatar,omitempty"`
	Public       bool              `json:"public"`
	DetailsOK    bool              `json:"detailsOk"` // game details visible (owned games list came back)
	Playing      bool              `json:"playing"`   // gameid == the demo
	PlayingOther string            `json:"playingOther,omitempty"`
	Owned        *ownedGame        `json:"owned,omitempty"`  // the demo in GetOwnedGames
	Recent       *ownedGame        `json:"recent,omitempty"` // the demo in GetRecentlyPlayedGames
	Full         *ownedGame        `json:"full,omitempty"`   // the full game, if you own it
	Achievements []achievement     `json:"achievements,omitempty"`
	AchNote      string            `json:"achNote,omitempty"`
	Privacy      string            `json:"privacy,omitempty"` // what to change, if anything
	Error        string            `json:"error,omitempty"`
	SelfTest     map[string]string `json:"selfTest,omitempty"`
}

const privacyHelp = "Your Steam game details are private, so Steam won't share your playtime. To fix it: open Steam, click your name at the top, choose Profile, then Edit Profile, then Privacy Settings. Set My profile and Game details to Public, and untick \"Always keep my total playtime private\". It can take a few minutes to update."

func fetchSteamStats(ctx context.Context, key, steamID string) steamStats {
	st := steamStats{At: time.Now().Unix(), SelfTest: map[string]string{}}
	if key == "" || steamID == "" {
		st.Error = "Sign in with Steam and add your Web API key in Settings."
		return st
	}
	var sum struct {
		Response struct {
			Players []playerSummary `json:"players"`
		} `json:"response"`
	}
	if err := steamGet(ctx, key, "/ISteamUser/GetPlayerSummaries/v2/", neturl.Values{"steamids": {steamID}}, &sum); err != nil {
		st.Error = err.Error()
		st.SelfTest["GetPlayerSummaries"] = "failed: " + err.Error()
		return st
	}
	if len(sum.Response.Players) == 0 {
		st.Error = "Steam didn't return your profile."
		return st
	}
	p := sum.Response.Players[0]
	st.Persona, st.Avatar, st.Public = p.PersonaName, p.Avatar, p.Visibility == 3
	switch {
	case p.GameID == demoAppStr:
		st.Playing = true
		st.SelfTest["GetPlayerSummaries"] = "shows the demo running (gameid " + p.GameID + ")"
	case p.GameID != "":
		st.PlayingOther = p.GameExtraInfo
		st.SelfTest["GetPlayerSummaries"] = "shows a different game running: " + p.GameExtraInfo + " (" + p.GameID + ")"
	default:
		st.SelfTest["GetPlayerSummaries"] = "works; no game running right now, so it can't prove the demo shows up yet"
	}

	var owned struct {
		Response struct {
			GameCount *int        `json:"game_count"`
			Games     []ownedGame `json:"games"`
		} `json:"response"`
	}
	oq := neturl.Values{"steamid": {steamID}, "include_appinfo": {"1"}, "include_played_free_games": {"1"}, "skip_unvetted_apps": {"0"}}
	if err := steamGet(ctx, key, "/IPlayerService/GetOwnedGames/v1/", oq, &owned); err != nil {
		st.SelfTest["GetOwnedGames"] = "failed: " + err.Error()
	} else if owned.Response.GameCount == nil {
		// An empty response means the game details are private.
		st.SelfTest["GetOwnedGames"] = "empty: game details are private"
	} else {
		st.DetailsOK = true
		for i, g := range owned.Response.Games {
			if g.AppID == demoAppID {
				st.Owned = &owned.Response.Games[i]
			}
			if g.AppID == fullAppID {
				st.Full = &owned.Response.Games[i]
			}
		}
		if st.Owned != nil {
			st.SelfTest["GetOwnedGames"] = fmt.Sprintf("lists the demo (%d minutes played)", st.Owned.Playtime)
		} else {
			st.SelfTest["GetOwnedGames"] = fmt.Sprintf("works (%d games) but doesn't list the demo", len(owned.Response.Games))
		}
	}

	var recent struct {
		Response struct {
			TotalCount *int        `json:"total_count"`
			Games      []ownedGame `json:"games"`
		} `json:"response"`
	}
	if err := steamGet(ctx, key, "/IPlayerService/GetRecentlyPlayedGames/v1/", neturl.Values{"steamid": {steamID}, "count": {"0"}}, &recent); err != nil {
		st.SelfTest["GetRecentlyPlayedGames"] = "failed: " + err.Error()
	} else {
		for i, g := range recent.Response.Games {
			if g.AppID == demoAppID {
				st.Recent = &recent.Response.Games[i]
			}
		}
		switch {
		case st.Recent != nil:
			st.SelfTest["GetRecentlyPlayedGames"] = fmt.Sprintf("lists the demo (%d minutes in two weeks)", st.Recent.Playtime2wk)
		case recent.Response.TotalCount == nil:
			st.SelfTest["GetRecentlyPlayedGames"] = "empty: game details are private"
		default:
			st.SelfTest["GetRecentlyPlayedGames"] = "works but doesn't list the demo (not played in the last two weeks, or demos aren't included)"
		}
	}

	var ach struct {
		PlayerStats struct {
			Success      bool          `json:"success"`
			Error        string        `json:"error"`
			Achievements []achievement `json:"achievements"`
		} `json:"playerstats"`
	}
	err := steamGet(ctx, key, "/ISteamUserStats/GetPlayerAchievements/v1/", neturl.Values{"steamid": {steamID}, "appid": {demoAppStr}, "l": {"english"}}, &ach)
	var se *steamErr
	switch {
	case err == nil && ach.PlayerStats.Success && len(ach.PlayerStats.Achievements) > 0:
		st.Achievements = ach.PlayerStats.Achievements
		st.SelfTest["GetPlayerAchievements"] = fmt.Sprintf("the demo has %d achievements", len(st.Achievements))
	case err == nil || (errors.As(err, &se) && se.Status == 400):
		st.AchNote = "The demo has no Steam achievements of its own."
		if ach.PlayerStats.Error != "" {
			st.SelfTest["GetPlayerAchievements"] = "none: " + ach.PlayerStats.Error
		} else {
			st.SelfTest["GetPlayerAchievements"] = "none for the demo"
		}
	default:
		st.SelfTest["GetPlayerAchievements"] = "failed: " + err.Error()
	}

	if !st.Public || !st.DetailsOK {
		st.Privacy = privacyHelp
	}
	return st
}

// ---- Cache shared by the dashboard and the phone ----

var (
	steamMu   sync.Mutex
	steamLast steamStats
	steamKick = make(chan struct{}, 1)
)

func steamSnapshot() steamStats {
	steamMu.Lock()
	defer steamMu.Unlock()
	return steamLast
}

func refreshSteamNow() {
	select {
	case steamKick <- struct{}{}:
	default:
	}
}

// steamLoop asks the Web API every two minutes (every 45 seconds while the demo runs).
// Steam allows 100,000 calls a day per key; this uses about 3,000 at most.
func steamLoop() {
	for {
		s := snapshotSettings()
		key := unseal(s.SteamKeyEnc)
		if key != "" && s.SteamID != "" {
			ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
			st := fetchSteamStats(ctx, key, s.SteamID)
			cancel()
			steamMu.Lock()
			steamLast = st
			steamMu.Unlock()
			if b, err := json.Marshal(st); err == nil {
				storeSet("cache:steam", string(b))
			}
		}
		wait := 2 * time.Minute
		if gameRunning() {
			wait = 45 * time.Second
		}
		select {
		case <-steamKick:
		case <-time.After(wait):
		}
	}
}

func loadSteamCache() {
	if v := storeGet("cache:steam"); v != "" {
		var st steamStats
		if json.Unmarshal([]byte(v), &st) == nil {
			steamMu.Lock()
			steamLast = st
			steamMu.Unlock()
		}
	}
}

func itoa64(n int64) string { return strconv.FormatInt(n, 10) }
