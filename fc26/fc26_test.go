package main

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"image"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	neturl "net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// ---- helpers ----

func fresh(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("FC26DASH_DIR", dir)
	setMu.Lock()
	cur = defaultSettings()
	setMu.Unlock()
	stMu.Lock()
	store = map[string]string{}
	stMu.Unlock()
	sesMu.Lock()
	sessions, openStart, lastBeat, running = nil, 0, 0, false
	sesMu.Unlock()
	exMu.Lock()
	exLatest, exHistory, exLastFP, exBusy, autoNote = nil, nil, nil, false, ""
	exMu.Unlock()
	steamMu.Lock()
	steamLast = steamStats{}
	steamMu.Unlock()
	brMu.Lock()
	brCache, brKey = bridgeState{}, ""
	brMu.Unlock()
	return dir
}

// copySteam copies testdata Steam folders to a temp dir, filling in the library paths.
func copySteam(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	err := filepath.Walk("testdata", func(p string, info os.FileInfo, err error) error {
		if err != nil || strings.Contains(p, "screens") {
			return err
		}
		rel, _ := filepath.Rel("testdata", p)
		dst := filepath.Join(root, rel)
		if info.IsDir() {
			return os.MkdirAll(dst, 0o755)
		}
		b, _ := os.ReadFile(p)
		esc := func(s string) string { return strings.ReplaceAll(s, `\`, `\\`) }
		b = bytes.ReplaceAll(b, []byte("STEAMROOT"), []byte(esc(filepath.Join(root, "steam"))))
		b = bytes.ReplaceAll(b, []byte("LIB2ROOT"), []byte(esc(filepath.Join(root, "lib2"))))
		return os.WriteFile(dst, b, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(root, "steam")
}

func loadPNG(t *testing.T, name string) image.Image {
	t.Helper()
	f, err := os.Open(filepath.Join("testdata", "screens", name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	img, _, err := image.Decode(f)
	if err != nil {
		t.Fatal(err)
	}
	return img
}

// ---- Steam's local files ----

func TestVDFParsesSampleLocalConfig(t *testing.T) {
	b, _ := os.ReadFile("testdata/steam/userdata/123456789/config/localconfig.vdf")
	root, err := parseVDF(string(b))
	if err != nil {
		t.Fatal(err)
	}
	app := root.Path("UserLocalConfigStore", "software", "valve", "steam", "apps", "3405690")
	if app == nil || app.Str("Playtime") != "845" || app.Str("LastPlayed") != "1763900000" {
		t.Fatalf("demo entry not read: %+v", app)
	}
	if v := root.Path("UserLocalConfigStore", "WebStorage").Str(`key with "quotes"`); v != `value with \ backslash` {
		t.Fatalf("escapes not handled: %q", v)
	}
	if _, err := parseVDF(`"a" { "b" "c" `); err == nil {
		t.Fatal("unterminated block should fail")
	}
	if _, err := parseVDF("\xef\xbb\xbf\"a\" { \"b\" \"c\" }"); err != nil {
		t.Fatal("byte order mark should be skipped:", err)
	}
}

func TestReadLocalSteamFindsGameInSecondLibrary(t *testing.T) {
	sp := copySteam(t)
	ls := readLocalSteam(sp, 123456789)
	if !ls.Found || !ls.Installed || ls.Name != "EA SPORTS FC™ 26" || ls.AppID != 3405690 {
		t.Fatalf("install not found: %+v", ls)
	}
	if ls.PlaytimeMin != 845 || ls.Playtime2wk != 312 || ls.LastPlayed != 1763900000 {
		t.Fatalf("playtime wrong: %+v", ls)
	}
	want := filepath.Join(filepath.Dir(sp), "lib2", "steamapps", "common", "FC 26")
	if g := gameDir(sp); g != want {
		t.Fatalf("game dir %q, want %q", g, want)
	}
	// Unknown account: it still finds the config that mentions the demo.
	ls2 := readLocalSteam(sp, 0)
	if ls2.PlaytimeMin != 845 || ls2.AccountID != 123456789 {
		t.Fatalf("fallback account search failed: %+v", ls2)
	}
	if r := readLocalSteam("", 0); r.Found || r.Error == "" {
		t.Fatal("missing Steam should say so")
	}
	if accountFromSteamID("76561198083722517") != 123456789 {
		t.Fatal("steam id to account id wrong")
	}
}

// ---- Steam Web API (fake server) ----

type fakeSteam struct {
	public, details, playing, achievements, badKey bool
	calls                                          atomic.Int32
	sawKeys                                        []string
}

func (f *fakeSteam) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		f.sawKeys = append(f.sawKeys, r.URL.Query().Get("key"))
		if f.badKey {
			w.WriteHeader(403)
			fmt.Fprint(w, "<html>Forbidden</html>")
			return
		}
		vis := 1
		if f.public {
			vis = 3
		}
		switch {
		case strings.Contains(r.URL.Path, "GetPlayerSummaries"):
			game := ""
			if f.playing {
				game = `,"gameid":"3405690","gameextrainfo":"EA SPORTS FC™ 26"`
			}
			fmt.Fprintf(w, `{"response":{"players":[{"steamid":"76561198083722517","personaname":"Pete","communityvisibilitystate":%d%s}]}}`, vis, game)
		case strings.Contains(r.URL.Path, "GetOwnedGames"):
			if r.URL.Query().Get("include_played_free_games") != "1" {
				t := "include_played_free_games missing"
				http.Error(w, t, 400)
				return
			}
			if !f.details {
				fmt.Fprint(w, `{"response":{}}`)
				return
			}
			fmt.Fprint(w, `{"response":{"game_count":2,"games":[{"appid":3405690,"name":"EA SPORTS FC™ 26","playtime_forever":845,"playtime_2weeks":312,"rtime_last_played":1763900000},{"appid":228980,"playtime_forever":1}]}}`)
		case strings.Contains(r.URL.Path, "GetRecentlyPlayedGames"):
			if !f.details {
				fmt.Fprint(w, `{"response":{}}`)
				return
			}
			fmt.Fprint(w, `{"response":{"total_count":1,"games":[{"appid":3405690,"name":"EA SPORTS FC™ 26","playtime_2weeks":312,"playtime_forever":845}]}}`)
		case strings.Contains(r.URL.Path, "GetPlayerAchievements"):
			if !f.public {
				w.WriteHeader(403)
				fmt.Fprint(w, `{"playerstats":{"error":"Profile is not public","success":false}}`)
				return
			}
			if !f.achievements {
				w.WriteHeader(400)
				fmt.Fprint(w, `{"playerstats":{"error":"Requested app has no stats","success":false}}`)
				return
			}
			fmt.Fprint(w, `{"playerstats":{"success":true,"achievements":[{"apiname":"A1","name":"First Victory","achieved":1,"unlocktime":1763800000}]}}`)
		default:
			http.NotFound(w, r)
		}
	})
}

func withSteam(t *testing.T, f *fakeSteam) {
	srv := httptest.NewServer(f.handler())
	t.Cleanup(srv.Close)
	old := steamAPI
	steamAPI = srv.URL
	t.Cleanup(func() { steamAPI = old })
}

func TestSteamStatsPublicProfilePlayingFC26(t *testing.T) {
	fresh(t)
	f := &fakeSteam{public: true, details: true, playing: true}
	withSteam(t, f)
	st := fetchSteamStats(t.Context(), "KEY123SECRET", "76561198083722517")
	if st.Error != "" || !st.Playing || st.Owned == nil || st.Owned.Playtime != 845 || st.Recent == nil {
		t.Fatalf("bad stats: %+v", st)
	}
	if st.Privacy != "" {
		t.Fatal("public profile shouldn't show the privacy message")
	}
	if st.AchNote == "" || len(st.Achievements) != 0 {
		t.Fatalf("no stats returned should say so: %+v", st)
	}
	if !strings.Contains(st.SelfTest["GetPlayerSummaries"], "FC 26 running") || !strings.Contains(st.SelfTest["GetOwnedGames"], "lists FC 26 as app 3405690") {
		t.Fatalf("self test: %v", st.SelfTest)
	}
}

func TestSteamStatsPrivateProfileExplainsHowToFix(t *testing.T) {
	fresh(t)
	withSteam(t, &fakeSteam{public: false, details: false})
	st := fetchSteamStats(t.Context(), "KEY123SECRET", "76561198083722517")
	if st.Privacy == "" || !strings.Contains(st.Privacy, "Game details to Public") {
		t.Fatalf("expected privacy help, got %+v", st)
	}
	if st.Owned != nil || st.DetailsOK {
		t.Fatal("private profile can't have playtime")
	}
	if !strings.Contains(st.SelfTest["GetOwnedGames"], "private") {
		t.Fatalf("self test should name the cause: %v", st.SelfTest)
	}
}

func TestSteamAchievementsWhenPresent(t *testing.T) {
	fresh(t)
	withSteam(t, &fakeSteam{public: true, details: true, achievements: true})
	st := fetchSteamStats(t.Context(), "K", "76561198083722517")
	if len(st.Achievements) != 1 || st.Achievements[0].Name != "First Victory" {
		t.Fatalf("achievements: %+v", st)
	}
}

func TestSteamBadKeyNeverLeaksKey(t *testing.T) {
	fresh(t)
	withSteam(t, &fakeSteam{badKey: true})
	st := fetchSteamStats(t.Context(), "SUPERSECRETKEY", "76561198083722517")
	if !strings.Contains(st.Error, "rejected the Web API key") {
		t.Fatalf("error: %q", st.Error)
	}
	b, _ := json.Marshal(st)
	if strings.Contains(string(b), "SUPERSECRETKEY") {
		t.Fatal("key leaked into the stats")
	}
	// Network errors include the URL: make sure it's redacted.
	old := steamAPI
	steamAPI = "http://127.0.0.1:1"
	defer func() { steamAPI = old }()
	st = fetchSteamStats(t.Context(), "SUPERSECRETKEY", "76561198083722517")
	if strings.Contains(st.Error, "SUPERSECRETKEY") || st.Error == "" {
		t.Fatalf("network error leaked or missing: %q", st.Error)
	}
}

// ---- Steam OpenID ----

func TestOpenIDVerify(t *testing.T) {
	var gotMode string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		gotMode = r.PostForm.Get("openid.mode")
		if r.PostForm.Get("openid.sig") == "good" {
			fmt.Fprint(w, "ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")
		} else {
			fmt.Fprint(w, "ns:http://specs.openid.net/auth/2.0\nis_valid:false\n")
		}
	}))
	defer srv.Close()
	old := steamOpenID
	steamOpenID = srv.URL
	defer func() { steamOpenID = old }()

	ret := "http://127.0.0.1:5000/steam/return?n=abc"
	q := neturl.Values{
		"openid.mode": {"id_res"}, "openid.return_to": {ret}, "openid.op_endpoint": {"https://steamcommunity.com/openid/login"},
		"openid.claimed_id": {"https://steamcommunity.com/openid/id/76561198083722517"}, "openid.sig": {"good"},
	}
	id, err := openIDVerify(q, ret)
	if err != nil || id != "76561198083722517" || gotMode != "check_authentication" {
		t.Fatalf("verify: %v %q %q", err, id, gotMode)
	}
	q.Set("openid.sig", "forged")
	if _, err := openIDVerify(q, ret); err == nil {
		t.Fatal("forged signature accepted")
	}
	q.Set("openid.sig", "good")
	if _, err := openIDVerify(q, "http://127.0.0.1:5000/steam/return?n=other"); err == nil {
		t.Fatal("wrong return_to accepted")
	}
	q.Set("openid.claimed_id", "https://evil.example/openid/id/76561198083722517")
	if _, err := openIDVerify(q, ret); err == nil {
		t.Fatal("foreign claimed_id accepted")
	}
	u := openIDStart(ret, "http://127.0.0.1:5000/")
	if !strings.Contains(u, "openid.mode=checkid_setup") || !strings.Contains(u, neturl.QueryEscape(ret)) {
		t.Fatalf("start url: %s", u)
	}
}

// ---- Sessions and streak ----

func TestStreakFromSessionLog(t *testing.T) {
	loc := time.FixedZone("UK", 0)
	day := func(d, h int) int64 { return time.Date(2026, 11, d, h, 0, 0, 0, loc).Unix() }
	all := []session{
		{day(18, 19), day(18, 20)},      // 18th
		{day(20, 19), day(20, 21)},      // 20th
		{day(21, 23), day(22, 1)},       // crosses midnight: 21st and 22nd
		{day(23, 10), day(23, 10) + 30}, // 30 seconds: doesn't count
	}
	now := time.Date(2026, 11, 23, 15, 0, 0, 0, loc)
	cur, best, today, days := streakOf(all, now, loc)
	if cur != 3 || today || best != 3 {
		t.Fatalf("cur=%d best=%d today=%v", cur, best, today)
	}
	if len(days) != 28 || days[27] != "0" || days[26] != "1" {
		t.Fatalf("days: %v", days)
	}
	all = append(all, session{day(23, 16), day(23, 17)})
	now = time.Date(2026, 11, 23, 18, 0, 0, 0, loc)
	if cur, _, today, _ = streakOf(all, now, loc); cur != 4 || !today {
		t.Fatalf("after playing today: cur=%d today=%v", cur, today)
	}
	now = time.Date(2026, 11, 25, 9, 0, 0, 0, loc)
	if cur, _, _, _ = streakOf(all, now, loc); cur != 0 {
		t.Fatalf("missed a day, streak should reset, got %d", cur)
	}
}

func TestObserveWritesSessions(t *testing.T) {
	fresh(t)
	t0 := time.Date(2026, 11, 24, 18, 0, 0, 0, time.Local)
	for i := 0; i <= 120; i += 5 {
		observe(t0.Add(time.Duration(i)*time.Second), true, "registry")
	}
	si := sessionSnapshot(t0.Add(120 * time.Second))
	if !si.Running || si.SessionSec != 120 {
		t.Fatalf("live session: %+v", si)
	}
	observe(t0.Add(125*time.Second), false, "registry")
	si = sessionSnapshot(t0.Add(130 * time.Second))
	if si.Running || si.Count != 1 || si.LoggedSec != 125 {
		t.Fatalf("closed session: %+v", si)
	}
	// Survives a restart.
	sesMu.Lock()
	sessions = nil
	sesMu.Unlock()
	loadSessions()
	if s := sessionSnapshot(t0.Add(130 * time.Second)); s.Count != 1 {
		t.Fatal("session log not saved")
	}
	// A long gap splits sessions.
	observe(t0.Add(time.Hour), true, "registry")
	observe(t0.Add(time.Hour+5*time.Minute), true, "registry")
	observe(t0.Add(time.Hour+5*time.Minute+5*time.Second), false, "registry")
	if s := sessionSnapshot(t0.Add(2 * time.Hour)); s.Count != 3 {
		t.Fatalf("gap should split sessions: %+v", s)
	}
}

// ---- Explanations: parsing, club facts, demo progress, weak topics ----

const sampleUTReply = "Here you go:\n```json\n" + `{
 "screen": "Ultimate Team Squad",
 "mode": "Ultimate Team",
 "explanation": "This is your **Ultimate Team** squad. Rating 85, chemistry 29 of 33.",
 "next": ["Fix the left back's chemistry", "Play Division Rivals"],
 "topics": ["ultimate team", "chemistry", "Made Up Topic"],
 "mistakes": [{"topic": "chemistry", "note": "Iqbal at LB has no chemistry links"}],
 "game_date": null,
 "club": null,
 "ultimate_team": {"coins": "48,250", "fc_points": "250", "squad_rating": 85, "chemistry": "29/33", "division": "Division 6", "objectives": null}
}` + "\n```"

const sampleCareerReply = `{"screen":"Career Mode Central","mode":"Career Mode","explanation":"Your Career Mode hub.","next":["Scout a left back"],"topics":["Career Mode"],"mistakes":[],"game_date":"2025-08-16",
 "club":{"name":"Northbridge Athletic","league":"League One","position":null,"next_fixture":{"opponent":"Kingsmere Rovers","date":"2025-08-23","venue":"H","competition":"League One"},"squad":null,
 "finances":{"balance":"","transfer_budget":"€12.5M","wage_budget":"€180K / wk","wage_spend":""},
 "table":[{"pos":1,"team":"Kingsmere Rovers","played":3,"gd":5,"points":9},{"pos":2,"team":"Port Allerton","played":3,"gd":4,"points":8},{"pos":3,"team":"Northbridge Athletic","played":3,"gd":3,"points":7},{"pos":4,"team":"Ashcombe Town","played":3,"gd":2,"points":6}]},
 "ultimate_team": null}`

func TestGameRecognisedByIDOrName(t *testing.T) {
	for _, n := range []string{"EA SPORTS FC™ 26", "EA SPORTS FC 26 SHOWCASE", "ea sports fc  26"} {
		if !isGameName(n) {
			t.Errorf("%q should count as FC 26", n)
		}
	}
	for _, n := range []string{"EA SPORTS FC™ 25", "Football Manager 26", "EA SPORTS FC™ 26 - FC Points"} {
		if isGameName(n) && !strings.Contains(n, "Points") {
			t.Errorf("%q shouldn't count", n)
		}
	}
	if !isGameApp(3405690, "") || !isGameApp(9999999, "EA SPORTS FC™ 26") || isGameApp(2669320, "EA SPORTS FC™ 25") {
		t.Fatal("app matching wrong")
	}
}

func TestParseAndMergeClub(t *testing.T) {
	out, err := parseExplain(sampleUTReply)
	if err != nil {
		t.Fatal(err)
	}
	if out.Topics[0] != "Ultimate Team" || out.Topics[1] != "Chemistry" || out.Topics[2] != "Made Up Topic" {
		t.Fatalf("topics not normalised: %v", out.Topics)
	}
	var cs clubState
	mergeClub(&cs, out, 100)
	if cs.UT == nil || cs.UT.V.Coins != "48,250" || cs.UT.V.SquadRating != 85 || cs.UT.V.Division != "Division 6" || cs.Mode.V != "Ultimate Team" {
		t.Fatalf("UT: %+v", cs.UT)
	}
	out2, err := parseExplain(sampleCareerReply)
	if err != nil {
		t.Fatal(err)
	}
	mergeClub(&cs, out2, 200)
	if cs.Name.V != "Northbridge Athletic" || cs.NextFixture.V.Opponent != "Kingsmere Rovers" || cs.GameDate.V != "2025-08-16" {
		t.Fatalf("career: %+v", cs)
	}
	if cs.Position == nil || cs.Position.V != 3 {
		t.Fatalf("position from table: %+v", cs.Position)
	}
	if cs.Finances.V.TransferBudget != "€12.5M" || cs.Mode.V != "Career Mode" {
		t.Fatalf("finances/mode: %+v", cs)
	}
	if cs.UT.At != 100 || cs.UT.V.Coins != "48,250" {
		t.Fatal("UT facts from an earlier screen should stay")
	}
	// A later UT screen that only shows coins keeps the other UT figures.
	c := "51,000"
	mergeClub(&cs, explainOut{Screen: "Transfer Market", UT: &utRead{Coins: &c}}, 300)
	if cs.UT.V.Coins != "51,000" || cs.UT.V.SquadRating != 85 {
		t.Fatalf("partial UT update: %+v", cs.UT.V)
	}
	if _, err := parseExplain("Sorry, I can't see the image."); err == nil {
		t.Fatal("non JSON reply should be an error")
	}
	bad := explainOut{GameDate: strPtr("16/08/2025")}
	mergeClub(&cs, bad, 300)
	if cs.GameDate.V != "2025-08-16" {
		t.Fatal("badly formatted date should be ignored")
	}
}

func strPtr(s string) *string { return &s }

func TestWeakTopicsRanking(t *testing.T) {
	fresh(t)
	weakBump("defending", "ask", "")
	weakBump("Chemistry", "mistake", "Left back has no links")
	weakBump("Chemistry", "quizWrong", "")
	weakBump("Defending", "quizRight", "")
	weakBump("Defending", "quizRight", "")
	weakBump("", "ask", "")
	weakBump("Finishing", "bogus", "")
	r := weakRanked()
	if len(r) != 2 || r[0].Topic != "Chemistry" || r[0].Score != 4 || r[0].Notes[0] != "Left back has no links" {
		t.Fatalf("ranking: %+v", r)
	}
	if r[1].Topic != "Defending" || r[1].Score != 0 || r[1].Asks != 1 {
		t.Fatalf("defending should be floored at zero but kept: %+v", r[1])
	}
}

// ---- Pictures and auto mode ----

func TestChangeDetectionOnSampleScreens(t *testing.T) {
	sq, sq2, tbl, blank := loadPNG(t, "ultimate_team.png"), loadPNG(t, "ultimate_team_coins_changed.png"), loadPNG(t, "career_mode.png"), loadPNG(t, "blank_fullscreen.png")
	a, b, c := fingerprintOf(sq), fingerprintOf(sq2), fingerprintOf(tbl)
	if d := a.diff(b); d >= screenSettled {
		t.Fatalf("a small number changing shouldn't count as a new screen (diff %.3f)", d)
	}
	if d := a.diff(c); d < screenChanged {
		t.Fatalf("UT squad vs Career Mode should count as a new screen (diff %.3f)", d)
	}
	if !isBlank(fingerprintOf(blank)) || isBlank(a) {
		t.Fatal("blank detection wrong")
	}
	jpg, small := prepImage(sq, 1568)
	if small.Bounds().Dx() != 1568 || small.Bounds().Dy() != 882 || len(jpg) > 600_000 {
		t.Fatalf("image prep: %v, %d bytes", small.Bounds(), len(jpg))
	}
	if th := thumbJPEG(small, 360); !strings.HasPrefix(th, "data:image/jpeg;base64,") {
		t.Fatal("thumbnail")
	}
}

func TestAutoLimiter(t *testing.T) {
	var l limiter
	now := time.Now()
	for i := 0; i < 3; i++ {
		ok, why := l.allow(now, 90*time.Second, 3)
		if !ok {
			t.Fatalf("try %d refused: %s", i, why)
		}
		l.record(now)
		if ok, _ := l.allow(now.Add(30*time.Second), 90*time.Second, 3); ok {
			t.Fatal("gap not enforced")
		}
		now = now.Add(2 * time.Minute)
	}
	if ok, why := l.allow(now, 90*time.Second, 3); ok || !strings.Contains(why, "hourly") {
		t.Fatalf("hourly cap not enforced: %v %s", ok, why)
	}
	if ok, _ := l.allow(now.Add(time.Hour), 90*time.Second, 3); !ok {
		t.Fatal("should allow again after an hour")
	}
}

// ---- The whole explain pipeline, with a fake Claude ----

type fakeClaude struct {
	reply  string
	bodies [][]byte
	status int
}

func (f *fakeClaude) server(t *testing.T) string {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		f.bodies = append(f.bodies, b)
		if r.Header.Get("x-api-key") != "sk-ant-test" {
			w.WriteHeader(401)
			fmt.Fprint(w, `{"error":{"message":"bad key"}}`)
			return
		}
		if f.status != 0 {
			w.WriteHeader(f.status)
			fmt.Fprint(w, `{"error":{"message":"overloaded"}}`)
			return
		}
		out, _ := json.Marshal(map[string]any{"model": "claude-sonnet-5-5", "content": []map[string]string{{"type": "text", "text": f.reply}}})
		w.Write(out)
	}))
	t.Cleanup(srv.Close)
	old := claudeURL
	claudeURL = srv.URL
	t.Cleanup(func() { claudeURL = old })
	return srv.URL
}

func TestExplainPipelineWithSampleScreenshot(t *testing.T) {
	fresh(t)
	fc := &fakeClaude{reply: sampleUTReply}
	fc.server(t)
	if err := saveSettings(map[string]string{"claudeKey": "sk-ant-test", "steamKey": "STEAMKEY-DO-NOT-SEND"}, false); err != nil {
		t.Fatal(err)
	}
	t.Setenv("FC26_FAKE_CAPTURE", "testdata/screens/ultimate_team.png")
	rec, err := explainNow(false)
	if err != nil {
		t.Fatal(err)
	}
	if rec.Screen != "Ultimate Team Squad" || rec.Via != "claude" || len(rec.Next) != 2 || rec.Thumb == "" {
		t.Fatalf("record: %+v", rec)
	}
	if len(fc.bodies) != 1 {
		t.Fatalf("expected one call, got %d", len(fc.bodies))
	}
	body := fc.bodies[0]
	if bytes.Count(body, []byte(`"type":"image"`)) != 1 || !bytes.Contains(body, []byte(`"media_type":"image/jpeg"`)) {
		t.Fatal("exactly one game picture should be sent")
	}
	if bytes.Contains(body, []byte("STEAMKEY-DO-NOT-SEND")) || bytes.Contains(body, []byte("sk-ant-test")) {
		t.Fatal("a key leaked into the AI request body")
	}
	st := stateFor(true)
	cs := st["club"].(clubState)
	if cs.UT == nil || cs.UT.V.Coins != "48,250" {
		t.Fatalf("club not stored: %+v", cs)
	}
	w := weakRanked()
	if len(w) == 0 || w[0].Topic != "Chemistry" || w[0].Score != 3 {
		t.Fatalf("weak topics after a manual explain with a mistake: %+v", w)
	}
	// Follow up question: text only, no picture.
	fc.reply = "TOPIC: Chemistry\nMove Iqbal or swap in a left back from the same league."
	ans, err := askAboutScreen("How do I fix my chemistry?")
	if err != nil || !strings.HasPrefix(ans, "Move Iqbal") {
		t.Fatalf("ask: %q %v", ans, err)
	}
	if bytes.Contains(fc.bodies[1], []byte(`"image"`)) {
		t.Fatal("follow ups shouldn't resend the picture")
	}
	// A blank picture (exclusive full screen) gives clear advice instead of a wasted AI call.
	t.Setenv("FC26_FAKE_CAPTURE", "testdata/screens/blank_fullscreen.png")
	if _, err := explainNow(false); err == nil || !strings.Contains(err.Error(), "Borderless") {
		t.Fatalf("blank capture: %v", err)
	}
	if len(fc.bodies) != 2 {
		t.Fatal("blank capture shouldn't call the AI")
	}
	// An AI error is shown, not swallowed.
	t.Setenv("FC26_FAKE_CAPTURE", "testdata/screens/half_time_stats.png")
	fc.status = 529
	if rec, err := explainNow(false); err == nil || !strings.Contains(rec.Error, "529") {
		t.Fatalf("AI error: %+v %v", rec, err)
	}
}

func TestBridgeGetsPicturesOnlyWhenItSupportsThem(t *testing.T) {
	fresh(t)
	var images atomic.Bool
	var bridgeCalls atomic.Int32
	br := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/ping") {
			if images.Load() {
				fmt.Fprint(w, `{"ok":true,"features":["images"]}`)
			} else {
				fmt.Fprint(w, `{"ok":true}`)
			}
			return
		}
		bridgeCalls.Add(1)
		var in map[string]any
		_ = json.NewDecoder(r.Body).Decode(&in)
		if in["source"] != "fc26-dashboard" || in["key"] != "linkkey" {
			w.WriteHeader(401)
			fmt.Fprint(w, `{"ok":false,"error":"bad key"}`)
			return
		}
		fmt.Fprint(w, `{"ok":true,"text":"`+strings.ReplaceAll(strings.ReplaceAll(sampleCareerReply, `"`, `\"`), "\n", " ")+`"}`)
	}))
	defer br.Close()
	old := bridgeBase
	bridgeBase = br.URL
	defer func() { bridgeBase = old }()
	fc := &fakeClaude{reply: sampleCareerReply}
	fc.server(t)
	t.Setenv("FC26_FAKE_CAPTURE", "testdata/screens/career_mode.png")

	// LLM Chat without the image update and no backup key: a clear message.
	_ = saveSettings(map[string]string{"bridgeKey": "linkkey"}, false)
	rec, err := explainNow(false)
	if err == nil || !strings.Contains(rec.Error, "image update") || bridgeCalls.Load() != 0 {
		t.Fatalf("expected image update message, got %+v %v", rec, err)
	}
	// With a backup key, pictures go to Claude directly.
	_ = saveSettings(map[string]string{"claudeKey": "sk-ant-test"}, false)
	brMu.Lock()
	brCache = bridgeState{}
	brMu.Unlock()
	if rec, err := explainNow(false); err != nil || rec.Via != "claude" {
		t.Fatalf("fallback: %+v %v", rec, err)
	}
	// Once LLM Chat says it takes images, it gets them.
	images.Store(true)
	brMu.Lock()
	brCache = bridgeState{}
	brMu.Unlock()
	t.Setenv("FC26_FAKE_CAPTURE", "testdata/screens/ultimate_team.png")
	if rec, err := explainNow(false); err != nil || rec.Via != "llmchat" || bridgeCalls.Load() != 1 {
		t.Fatalf("bridge with images: %+v %v calls=%d", rec, err, bridgeCalls.Load())
	}
	// Text questions use LLM Chat even without the image update.
	images.Store(false)
	brMu.Lock()
	brCache = bridgeState{}
	brMu.Unlock()
	r := askAI(aiRequest{Messages: []aiMessage{textMsg("user", "hi")}})
	if !r.OK || r.Via != "llmchat" {
		t.Fatalf("text via bridge: %+v", r)
	}
}

// ---- Phone link ----

func TestPrivateIPOnly(t *testing.T) {
	for _, s := range []string{"192.168.1.20", "10.0.0.5", "172.20.1.1", "127.0.0.1", "169.254.3.4", "fe80::1"} {
		if !privateIP(net.ParseIP(s)) {
			t.Errorf("%s should be allowed", s)
		}
	}
	for _, s := range []string{"8.8.8.8", "100.64.1.1", "81.2.69.160", "2a00:1450::1"} {
		if privateIP(net.ParseIP(s)) {
			t.Errorf("%s should be refused", s)
		}
	}
}

func TestPhonePairingOverTLS(t *testing.T) {
	fresh(t)
	_ = saveSettings(map[string]string{"steamKey": "STEAMKEY", "claudeKey": "sk-ant-test"}, false)
	_ = updateSettings(func(s *settings) error { s.SteamID = "76561198083722517"; return nil })
	if err := startLAN(lanHandler()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(forgetPhones)
	port := lanPortNum()
	fp := pairingInfo()["fp"].(string)
	base := fmt.Sprintf("https://127.0.0.1:%d", port)

	// The phone pins the certificate it saw: check that's the one the PC reports.
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{
		InsecureSkipVerify: true, // the phone pins by fingerprint instead
		VerifyConnection: func(cs tls.ConnectionState) error {
			h := sha256.Sum256(cs.PeerCertificates[0].Raw)
			if hex.EncodeToString(h[:]) != fp {
				return fmt.Errorf("fingerprint mismatch")
			}
			return nil
		},
	}}}
	post := func(path, tok string, body any) (*http.Response, map[string]any) {
		b, _ := json.Marshal(body)
		req, _ := http.NewRequest("POST", base+path, bytes.NewReader(b))
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		var out map[string]any
		_ = json.NewDecoder(resp.Body).Decode(&out)
		return resp, out
	}
	// No code yet.
	if resp, _ := post("/pair", "", map[string]string{"code": "000000"}); resp.StatusCode != 403 {
		t.Fatal("pairing without a code should fail")
	}
	code, _ := newPairCode()
	// Five wrong tries kill the code.
	for i := 0; i < 5; i++ {
		post("/pair", "", map[string]string{"code": "999999x"})
	}
	if resp, _ := post("/pair", "", map[string]string{"code": code}); resp.StatusCode != 403 {
		t.Fatal("code should be dead after five wrong tries")
	}
	code, _ = newPairCode()
	resp, out := post("/pair", "", map[string]string{"code": code, "device": "Pixel 9"})
	if resp.StatusCode != 200 || out["ok"] != true || out["token"] == "" || out["steamKey"] != "STEAMKEY" {
		t.Fatalf("pair: %v %v", resp.StatusCode, out)
	}
	tok := out["token"].(string)
	if s := snapshotSettings(); len(s.Devices) != 1 || s.Devices[0].Name != "Pixel 9" || s.Devices[0].TokenHash == tok || s.PhoneLink != "on" {
		t.Fatalf("device not stored safely: %+v", s.Devices)
	}
	// The code works once only.
	if resp, _ := post("/pair", "", map[string]string{"code": code}); resp.StatusCode != 403 {
		t.Fatal("code reused")
	}
	// State works with the token, not without.
	get := func(path, tok string) int {
		req, _ := http.NewRequest("GET", base+path, nil)
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		resp, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if get("/api/state", "") != 403 || get("/api/state", "wrong") != 403 || get("/api/state", tok) != 200 {
		t.Fatal("token check")
	}
	// The phone's view never includes the pairing code or the LLM Chat key.
	req, _ := http.NewRequest("GET", base+"/api/state", nil)
	req.Header.Set("Authorization", "Bearer "+tok)
	newPairCode()
	r2, _ := client.Do(req)
	sb, _ := io.ReadAll(r2.Body)
	r2.Body.Close()
	if strings.Contains(string(sb), `"phone"`) || strings.Contains(string(sb), "STEAMKEY") || strings.Contains(string(sb), "sk-ant") {
		t.Fatal("phone state leaks pairing or keys")
	}
	// The phone can change auto mode but not keys.
	post("/api/settings", tok, map[string]string{"auto": "on", "claudeKey": "sk-ant-evil", "bridgeKey": "evil"})
	if s := snapshotSettings(); s.Auto != "on" || unseal(s.ClaudeEnc) != "sk-ant-test" || s.BridgeKey != "" {
		t.Fatalf("phone changed something it shouldn't: %+v", s)
	}
	// The phone can't send pictures through the AI route.
	_, ai := post("/api/ai", tok, map[string]any{"messages": []map[string]any{{"role": "user", "content": []map[string]any{{"type": "image", "source": map[string]string{"type": "base64", "media_type": "image/png", "data": "AAAA"}}}}}})
	if ai["ok"] != false {
		t.Fatal("pictures via /api/ai should be refused")
	}
	// The phone can't touch the session log through the store.
	req, _ = http.NewRequest("POST", base+"/api/store?k=sessions", strings.NewReader(`{"v":"[]"}`))
	req.Header.Set("Authorization", "Bearer "+tok)
	r3, _ := client.Do(req)
	r3.Body.Close()
	if r3.StatusCode != 403 {
		t.Fatal("store should only take ui: keys")
	}
	// Discovery answers on the home network.
	uc, err := net.DialUDP("udp4", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: discoveryPort})
	if err == nil {
		uc.SetDeadline(time.Now().Add(2 * time.Second))
		uc.Write([]byte(discoveryAsk))
		buf := make([]byte, 512)
		n, err := uc.Read(buf)
		uc.Close()
		if err != nil || !strings.Contains(string(buf[:n]), fp) {
			t.Fatalf("discovery reply: %q %v", buf[:n], err)
		}
	}
	// Forget phones closes the link.
	forgetPhones()
	if _, err := client.Get(base + "/api/state"); err == nil {
		t.Fatal("link should be closed")
	}
}

func TestRedact(t *testing.T) {
	s := redact(`Get "https://api.steampowered.com/x?format=json&key=ABC123&steamid=1": dial tcp`, "ABC123")
	if strings.Contains(s, "ABC123") {
		t.Fatal(s)
	}
	if s := redact("?key=OTHER&x=1", ""); strings.Contains(s, "OTHER") {
		t.Fatal(s)
	}
}

func TestTrialCountdown(t *testing.T) {
	fresh(t)
	for in, want := range map[string]int{"7h 32m": 452, "07:32:10": 452, "45 min": 45, "9 hours 5 mins": 545, "2h": 120} {
		if got, ok := parseDuration(in); !ok || got != want {
			t.Errorf("%q: got %d %v, want %d", in, got, ok, want)
		}
	}
	if _, ok := parseDuration("soon"); ok {
		t.Error("nonsense should not parse")
	}
	s := defaultSettings()
	if s.Trial != "on" || s.TrialHours != 10 {
		t.Fatal("trial countdown should default to on, 10 hours")
	}
	now := time.Date(2026, 11, 24, 20, 0, 0, 0, time.Local)
	// From Steam: 3 hours played, plus 30 minutes running now.
	si := sessionInfo{LoggedSec: 2*3600 + 1800, SessionSec: 1800, SessionStart: now.Unix() - 1800}
	st := steamStats{Owned: &ownedGame{AppID: 3405690, Playtime: 180}}
	ti := trialOf(s, st, localSteam{}, si, clubState{}, now)
	if ti.UsedMin != 210 || ti.LeftMin != 390 || ti.Source != "Steam playtime" {
		t.Fatalf("from steam: %+v", ti)
	}
	// The game's own timer wins, less play since it was read.
	observe(now.Add(-40*time.Minute), true, "registry")
	for i := 39; i >= 0; i-- {
		observe(now.Add(-time.Duration(i)*time.Minute), true, "registry")
	}
	cs := clubState{TrialLeft: &stamped[int]{300, now.Add(-20 * time.Minute).Unix(), "Main menu"}}
	ti = trialOf(s, st, localSteam{}, sessionSnapshot(now), cs, now)
	if ti.LeftMin != 280 || ti.Source != "the game's own trial timer" {
		t.Fatalf("from screen: %+v", ti)
	}
	// Out of time never goes negative.
	ti = trialOf(s, steamStats{Owned: &ownedGame{Playtime: 900}}, localSteam{}, sessionInfo{}, clubState{}, now)
	if ti.LeftMin != 0 || ti.Pct != 100 {
		t.Fatalf("over the limit: %+v", ti)
	}
	// Read from a screen reply.
	out, err := parseExplain(`{"screen":"Main menu","explanation":"Your trial has 6h 10m left.","trial_left":"6h 10m"}`)
	if err != nil {
		t.Fatal(err)
	}
	var c2 clubState
	mergeClub(&c2, out, 1)
	if c2.TrialLeft == nil || c2.TrialLeft.V != 370 {
		t.Fatalf("trial left not stored: %+v", c2.TrialLeft)
	}
}
