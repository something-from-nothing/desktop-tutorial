package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"image"
	"image/color"
	"image/jpeg"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// "Current screen explained": capture only the FC 26 window, send it to Claude, show a plain
// English explanation and next steps, and pick up club facts (Career Mode squad, table,
// fixtures and budget; Ultimate Team coins, rating and division) from whatever screen it was.

var fmTopics = []string{"Attacking", "Finishing", "Defending", "Skill moves", "Set pieces", "Tactics",
	"Formations", "Ultimate Team", "Squad building", "Chemistry", "Transfers", "Career Mode", "Player development", "Menus and settings"}

const explainSystem = `You are an EA SPORTS FC 26 coach sitting beside Pete while he plays FC 26 on PC through Steam and the EA app, on the EA Play 10 hour trial (his progress carries over if he buys the game). He wants to get better at the game, and his trial time is limited, so favour advice that makes the most of it.
You are given a screenshot of the FC 26 window only.
Reply with ONE JSON object and nothing else, in this shape:
{
 "screen": "short name of the screen, e.g. Ultimate Team Squad, Career Mode Hub, Transfer Market, Tactics, Match (in play), Half time stats, Skill Games",
 "mode": "Ultimate Team, Career Mode, Clubs, Kick Off, Rush, Tournaments, Menus, or null",
 "explanation": "plain British English, markdown allowed, 60 to 160 words: what he is looking at and what the important numbers mean",
 "next": ["2 to 4 short, concrete next actions on this screen or the next one, with PC controls when useful"],
 "topics": ["which of these the screen is about: TOPICS"],
 "mistakes": [{"topic": "one of the topics", "note": "a problem you can see on screen, e.g. low chemistry, a player out of position, poor match stats; empty list if none"}],
 "game_date": "the Career Mode date shown on screen as yyyy-mm-dd, or null",
 "trial_left": "time left on his EA Play trial if the screen shows it, exactly as shown (e.g. 7h 32m or 07:32:10), or null",
 "club": {
   "name": "his club name or null",
   "league": "competition name or null",
   "position": league position as a number, or null,
   "next_fixture": {"opponent": "", "date": "yyyy-mm-dd or as shown", "venue": "H or A", "competition": ""} or null,
   "table": [{"pos": 1, "team": "", "played": 0, "gd": 0, "points": 0}] only if a league table is on screen, else null,
   "squad": [{"name": "", "position": "", "age": 0, "rating": "overall rating as shown", "status": "e.g. injured, unhappy, or empty"}] only if a squad list is on screen, else null,
   "finances": {"balance": "", "transfer_budget": "as shown, e.g. €45M", "wage_budget": "", "wage_spend": ""} only if visible, else null
 },
 "ultimate_team": {"coins": "as shown or null", "fc_points": "as shown or null", "squad_rating": number or null, "chemistry": "e.g. 31/33 or null", "division": "e.g. Division 6 or null", "objectives": "short summary of objectives or SBC progress shown, or null"} or null
}
Only fill in what you can actually read on screen. Never guess numbers. Use null for anything you can't see.`

type explainOut struct {
	Screen      string    `json:"screen"`
	Mode        string    `json:"mode"`
	UT          *utRead   `json:"ultimate_team"`
	Explanation string    `json:"explanation"`
	Next        []string  `json:"next"`
	Topics      []string  `json:"topics"`
	Mistakes    []mistake `json:"mistakes"`
	GameDate    *string   `json:"game_date"`
	TrialLeft   *string   `json:"trial_left"`
	Club        *clubRead `json:"club"`
}

type utRead struct {
	Coins       *string `json:"coins"`
	FCPoints    *string `json:"fc_points"`
	SquadRating *int    `json:"squad_rating"`
	Chemistry   *string `json:"chemistry"`
	Division    *string `json:"division"`
	Objectives  *string `json:"objectives"`
}

type utState struct {
	Coins       string `json:"coins,omitempty"`
	FCPoints    string `json:"fcPoints,omitempty"`
	SquadRating int    `json:"squadRating,omitempty"`
	Chemistry   string `json:"chemistry,omitempty"`
	Division    string `json:"division,omitempty"`
	Objectives  string `json:"objectives,omitempty"`
}

type mistake struct {
	Topic string `json:"topic"`
	Note  string `json:"note"`
}

type fixture struct {
	Opponent    string `json:"opponent"`
	Date        string `json:"date"`
	Venue       string `json:"venue"`
	Competition string `json:"competition"`
}

type tableRow struct {
	Pos    int    `json:"pos"`
	Team   string `json:"team"`
	Played int    `json:"played"`
	GD     int    `json:"gd"`
	Points int    `json:"points"`
}

type squadRow struct {
	Name     string `json:"name"`
	Position string `json:"position"`
	Age      int    `json:"age"`
	Rating   string `json:"rating"`
	Status   string `json:"status"`
}

type finances struct {
	Balance        string `json:"balance"`
	TransferBudget string `json:"transfer_budget"`
	WageBudget     string `json:"wage_budget"`
	WageSpend      string `json:"wage_spend"`
}

type clubRead struct {
	Name        *string    `json:"name"`
	League      *string    `json:"league"`
	Position    *int       `json:"position"`
	NextFixture *fixture   `json:"next_fixture"`
	Table       []tableRow `json:"table"`
	Squad       []squadRow `json:"squad"`
	Finances    *finances  `json:"finances"`
}

// stamped wraps one club fact with when and from which screen it was read.
type stamped[T any] struct {
	V      T      `json:"v"`
	At     int64  `json:"at"`
	Screen string `json:"screen"`
}

type clubState struct {
	Name        *stamped[string]     `json:"name,omitempty"`
	League      *stamped[string]     `json:"league,omitempty"`
	Position    *stamped[int]        `json:"position,omitempty"`
	NextFixture *stamped[fixture]    `json:"nextFixture,omitempty"`
	Table       *stamped[[]tableRow] `json:"table,omitempty"`
	Squad       *stamped[[]squadRow] `json:"squad,omitempty"`
	Finances    *stamped[finances]   `json:"finances,omitempty"`
	GameDate    *stamped[string]     `json:"gameDate,omitempty"`
	Mode        *stamped[string]     `json:"mode,omitempty"`
	TrialLeft   *stamped[int]        `json:"trialLeft,omitempty"` // minutes left, as read from the screen
	UT          *stamped[utState]    `json:"ut,omitempty"`
}

var jsonObjRe = regexp.MustCompile(`(?s)\{.*\}`)

// parseExplain pulls the JSON object out of the AI's reply, tolerating code fences or a
// sentence around it.
func parseExplain(text string) (explainOut, error) {
	var out explainOut
	raw := jsonObjRe.FindString(text)
	if raw == "" {
		return out, errors.New("no JSON in the reply")
	}
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return out, err
	}
	if strings.TrimSpace(out.Explanation) == "" {
		return out, errors.New("the reply had no explanation")
	}
	out.Topics = normTopics(out.Topics)
	for i := range out.Mistakes {
		if t := normTopics([]string{out.Mistakes[i].Topic}); len(t) > 0 {
			out.Mistakes[i].Topic = t[0]
		}
	}
	return out, nil
}

func normTopics(in []string) []string {
	var out []string
	seen := map[string]bool{}
	for _, t := range in {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		name := t
		for _, k := range fmTopics {
			if strings.EqualFold(k, t) || strings.EqualFold(strings.ReplaceAll(k, " ", ""), strings.ReplaceAll(t, " ", "")) {
				name = k
			}
		}
		if len(name) > 40 {
			name = name[:40]
		}
		if !seen[name] {
			seen[name] = true
			out = append(out, name)
		}
	}
	return out
}

var dateRe = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)

// mergeClub copies in what this screen showed, leaving older facts from other screens alone.
func mergeClub(cs *clubState, e explainOut, at int64) {
	sc := e.Screen
	if e.GameDate != nil && dateRe.MatchString(*e.GameDate) {
		if _, err := time.Parse("2006-01-02", *e.GameDate); err == nil {
			cs.GameDate = &stamped[string]{*e.GameDate, at, sc}
		}
	}
	if e.TrialLeft != nil {
		if m, ok := parseDuration(*e.TrialLeft); ok {
			cs.TrialLeft = &stamped[int]{m, at, sc}
		}
	}
	if m := strings.TrimSpace(e.Mode); m != "" && !strings.EqualFold(m, "null") {
		cs.Mode = &stamped[string]{m, at, sc}
	}
	if u := e.UT; u != nil {
		var cur utState
		if cs.UT != nil {
			cur = cs.UT.V
		}
		changed := false
		set := func(dst *string, v *string) {
			if v != nil && strings.TrimSpace(*v) != "" && !strings.EqualFold(*v, "null") {
				*dst = strings.TrimSpace(*v)
				changed = true
			}
		}
		set(&cur.Coins, u.Coins)
		set(&cur.FCPoints, u.FCPoints)
		set(&cur.Chemistry, u.Chemistry)
		set(&cur.Division, u.Division)
		set(&cur.Objectives, u.Objectives)
		if u.SquadRating != nil && *u.SquadRating > 0 && *u.SquadRating < 100 {
			cur.SquadRating = *u.SquadRating
			changed = true
		}
		if changed {
			cs.UT = &stamped[utState]{cur, at, sc}
		}
	}
	c := e.Club
	if c == nil {
		return
	}
	if c.Name != nil && strings.TrimSpace(*c.Name) != "" {
		cs.Name = &stamped[string]{strings.TrimSpace(*c.Name), at, sc}
	}
	if c.League != nil && strings.TrimSpace(*c.League) != "" {
		cs.League = &stamped[string]{strings.TrimSpace(*c.League), at, sc}
	}
	if c.Position != nil && *c.Position > 0 && *c.Position < 100 {
		cs.Position = &stamped[int]{*c.Position, at, sc}
	}
	if c.NextFixture != nil && strings.TrimSpace(c.NextFixture.Opponent) != "" {
		cs.NextFixture = &stamped[fixture]{*c.NextFixture, at, sc}
	}
	if len(c.Table) >= 4 {
		t := c.Table
		if len(t) > 30 {
			t = t[:30]
		}
		cs.Table = &stamped[[]tableRow]{t, at, sc}
		// The table also tells us the position if the club name is known.
		if cs.Name != nil {
			for _, r := range t {
				if strings.EqualFold(r.Team, cs.Name.V) && r.Pos > 0 {
					cs.Position = &stamped[int]{r.Pos, at, sc}
				}
			}
		}
	}
	if len(c.Squad) >= 3 {
		s := c.Squad
		if len(s) > 60 {
			s = s[:60]
		}
		cs.Squad = &stamped[[]squadRow]{s, at, sc}
	}
	if f := c.Finances; f != nil && (f.Balance != "" || f.TransferBudget != "" || f.WageBudget != "" || f.WageSpend != "") {
		if cs.Finances != nil {
			// Keep figures this screen didn't show.
			old := cs.Finances.V
			if f.Balance == "" {
				f.Balance = old.Balance
			}
			if f.TransferBudget == "" {
				f.TransferBudget = old.TransferBudget
			}
			if f.WageBudget == "" {
				f.WageBudget = old.WageBudget
			}
			if f.WageSpend == "" {
				f.WageSpend = old.WageSpend
			}
		}
		cs.Finances = &stamped[finances]{*f, at, sc}
	}
}

// ---- EA Play trial countdown ----

var (
	hmRe    = regexp.MustCompile(`(?i)(\d+)\s*h(?:ours?|rs?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?`)
	onlyMRe = regexp.MustCompile(`(?i)^(\d+)\s*m(?:in(?:ute)?s?)?$`)
	clockRe = regexp.MustCompile(`^(\d{1,2}):(\d{2})(?::(\d{2}))?$`)
)

// parseDuration reads trial time as the game shows it ("7h 32m", "45 min", "07:32:10") as minutes.
func parseDuration(s string) (int, bool) {
	s = strings.TrimSpace(s)
	if m := clockRe.FindStringSubmatch(s); m != nil {
		h, _ := strconv.Atoi(m[1])
		mm, _ := strconv.Atoi(m[2])
		return h*60 + mm, h <= 100
	}
	if m := onlyMRe.FindStringSubmatch(s); m != nil {
		mm, _ := strconv.Atoi(m[1])
		return mm, true
	}
	if m := hmRe.FindStringSubmatch(s); m != nil {
		h, _ := strconv.Atoi(m[1])
		mm, _ := strconv.Atoi(m[2])
		return h*60 + mm, h <= 100
	}
	return 0, false
}

type trialInfo struct {
	On         bool    `json:"on"`
	LimitMin   int     `json:"limitMin"`
	UsedMin    int     `json:"usedMin"`
	LeftMin    int     `json:"leftMin"`
	Pct        float64 `json:"pct"`
	Source     string  `json:"source"`
	ScreenAt   int64   `json:"screenAt,omitempty"`
	ScreenLeft int     `json:"screenLeft,omitempty"`
}

// trialOf works out how much of the trial is left. The game's own figure (read from the screen)
// wins, less any play since it was read. Otherwise it's the most of: Steam's playtime, Steam's
// file on this PC and this app's log, plus the session going on now. EA's own clock is the one
// that counts, so this is an estimate.
func trialOf(s settings, st steamStats, ls localSteam, si sessionInfo, cs clubState, now time.Time) trialInfo {
	ti := trialInfo{On: s.Trial == "on", LimitMin: s.TrialHours * 60}
	closed := int((si.LoggedSec - si.SessionSec) / 60)
	base, src := closed, "this app's log"
	if st.Owned != nil && st.Owned.Playtime > base {
		base, src = st.Owned.Playtime, "Steam playtime"
	}
	if ls.PlaytimeMin > base {
		base, src = ls.PlaytimeMin, "Steam on this PC"
	}
	ti.UsedMin = base + int(si.SessionSec/60)
	if cs.TrialLeft != nil {
		left := cs.TrialLeft.V - int(playedSince(cs.TrialLeft.At, now)/60)
		ti.ScreenAt, ti.ScreenLeft = cs.TrialLeft.At, cs.TrialLeft.V
		ti.UsedMin = ti.LimitMin - left
		src = "the game's own trial timer"
	}
	if ti.UsedMin < 0 {
		ti.UsedMin = 0
	}
	ti.LeftMin = max(0, ti.LimitMin-ti.UsedMin)
	if ti.LimitMin > 0 {
		ti.Pct = min(100, 100*float64(ti.UsedMin)/float64(ti.LimitMin))
	}
	ti.Source = src
	return ti
}

// ---- Weak topics: what you ask about and what goes wrong, ranked ----

type weakTopic struct {
	Score     int      `json:"score"`
	Asks      int      `json:"asks"`
	Mistakes  int      `json:"mistakes"`
	QuizWrong int      `json:"quizWrong"`
	QuizRight int      `json:"quizRight"`
	Notes     []string `json:"notes,omitempty"`
	Last      int64    `json:"last"`
}

var weakMu sync.Mutex

func weakLoad() map[string]*weakTopic {
	m := map[string]*weakTopic{}
	_ = storeLoad("weak", &m)
	return m
}

// weakBump records one event: "ask" (+1), "mistake" (+2), "quizWrong" (+2), "quizRight" (-1).
func weakBump(topic, kind, note string) {
	ts := normTopics([]string{topic})
	if len(ts) == 0 {
		return
	}
	weakMu.Lock()
	defer weakMu.Unlock()
	m := weakLoad()
	w := m[ts[0]]
	if w == nil {
		w = &weakTopic{}
		m[ts[0]] = w
	}
	switch kind {
	case "ask":
		w.Asks++
		w.Score++
	case "mistake":
		w.Mistakes++
		w.Score += 2
	case "quizWrong":
		w.QuizWrong++
		w.Score += 2
	case "quizRight":
		w.QuizRight++
		w.Score--
	default:
		return
	}
	if w.Score < 0 {
		w.Score = 0
	}
	if note = strings.TrimSpace(note); note != "" {
		w.Notes = append([]string{note}, w.Notes...)
		if len(w.Notes) > 5 {
			w.Notes = w.Notes[:5]
		}
	}
	w.Last = time.Now().Unix()
	storeJSON("weak", m)
}

type rankedTopic struct {
	Topic string `json:"topic"`
	weakTopic
}

func weakRanked() []rankedTopic {
	weakMu.Lock()
	m := weakLoad()
	weakMu.Unlock()
	var out []rankedTopic
	for k, v := range m {
		if v.Score > 0 || v.Asks > 0 {
			out = append(out, rankedTopic{k, *v})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].Last > out[j].Last
	})
	return out
}

// ---- Pictures: shrink, compare, encode ----

// prepImage shrinks the capture so the long edge is at most 1568 pixels (Claude's sweet spot)
// and encodes it as JPEG.
func prepImage(img image.Image, maxEdge int) ([]byte, image.Image) {
	b := img.Bounds()
	w, h := b.Dx(), b.Dy()
	if w > maxEdge || h > maxEdge {
		if w >= h {
			h = h * maxEdge / w
			w = maxEdge
		} else {
			w = w * maxEdge / h
			h = maxEdge
		}
	}
	dst := shrink(img, w, h)
	var buf bytes.Buffer
	_ = jpeg.Encode(&buf, dst, &jpeg.Options{Quality: 82})
	return buf.Bytes(), dst
}

func thumbJPEG(img image.Image, width int) string {
	b := img.Bounds()
	h := b.Dy() * width / b.Dx()
	dst := shrink(img, width, h)
	var buf bytes.Buffer
	_ = jpeg.Encode(&buf, dst, &jpeg.Options{Quality: 70})
	return "data:image/jpeg;base64," + base64.StdEncoding.EncodeToString(buf.Bytes())
}

// shrink scales a picture down by averaging the source pixels under each new pixel (no
// outside libraries needed, and sharp enough for reading text).
func shrink(img image.Image, w, h int) *image.RGBA {
	b := img.Bounds()
	if w < 1 {
		w = 1
	}
	if h < 1 {
		h = 1
	}
	src, ok := img.(*image.RGBA)
	if !ok || src.Rect.Min != (image.Point{}) {
		src = image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
		for y := 0; y < b.Dy(); y++ {
			for x := 0; x < b.Dx(); x++ {
				src.Set(x, y, img.At(b.Min.X+x, b.Min.Y+y))
			}
		}
	}
	if w >= b.Dx() && h >= b.Dy() {
		return src
	}
	dst := image.NewRGBA(image.Rect(0, 0, w, h))
	sw, sh := b.Dx(), b.Dy()
	for y := 0; y < h; y++ {
		y0, y1 := y*sh/h, (y+1)*sh/h
		if y1 <= y0 {
			y1 = y0 + 1
		}
		for x := 0; x < w; x++ {
			x0, x1 := x*sw/w, (x+1)*sw/w
			if x1 <= x0 {
				x1 = x0 + 1
			}
			var r, g, bl, n uint32
			for yy := y0; yy < y1; yy++ {
				row := src.Pix[yy*src.Stride:]
				for xx := x0; xx < x1; xx++ {
					p := row[xx*4:]
					r += uint32(p[0])
					g += uint32(p[1])
					bl += uint32(p[2])
					n++
				}
			}
			o := dst.Pix[y*dst.Stride+x*4:]
			o[0], o[1], o[2], o[3] = uint8(r/n), uint8(g/n), uint8(bl/n), 255
		}
	}
	return dst
}

// fingerprint is a 48x27 grey picture used to tell whether the screen changed.
type fingerprint []uint8

func fingerprintOf(img image.Image) fingerprint {
	const fw, fh = 48, 27
	b := img.Bounds()
	fp := make(fingerprint, fw*fh)
	for y := 0; y < fh; y++ {
		for x := 0; x < fw; x++ {
			// Average a small block for each cell.
			x0, x1 := b.Min.X+x*b.Dx()/fw, b.Min.X+(x+1)*b.Dx()/fw
			y0, y1 := b.Min.Y+y*b.Dy()/fh, b.Min.Y+(y+1)*b.Dy()/fh
			var sum, n uint64
			stepX, stepY := max(1, (x1-x0)/6), max(1, (y1-y0)/6)
			for yy := y0; yy < y1; yy += stepY {
				for xx := x0; xx < x1; xx += stepX {
					g := color.GrayModel.Convert(img.At(xx, yy)).(color.Gray)
					sum += uint64(g.Y)
					n++
				}
			}
			if n > 0 {
				fp[y*fw+x] = uint8(sum / n)
			}
		}
	}
	return fp
}

// Thresholds for auto mode, measured on the sample screens: two different screens differ in
// 4 to 5 percent of cells, the same screen with the date ticking over in under 0.1 percent.
const (
	screenChanged = 0.03 // at least this different from the last explained screen
	screenSettled = 0.01 // and no more than this different between two looks (not animating)
)

// diff is how different two fingerprints are: the share of cells (0 to 1) that changed clearly.
func (a fingerprint) diff(b fingerprint) float64 {
	if len(a) == 0 || len(a) != len(b) {
		return 1
	}
	changed := 0
	for i := range a {
		d := int(a[i]) - int(b[i])
		if d < 0 {
			d = -d
		}
		if d > 18 {
			changed++
		}
	}
	return float64(changed) / float64(len(a))
}

// isBlank spots the all black picture some games give when captured in exclusive full screen.
func isBlank(fp fingerprint) bool {
	if len(fp) == 0 {
		return true
	}
	lo, hi := uint8(255), uint8(0)
	for _, v := range fp {
		lo, hi = min(lo, v), max(hi, v)
	}
	return hi-lo < 6
}

// ---- Auto mode limits ----

type limiter struct {
	mu    sync.Mutex
	times []time.Time
}

// allow says whether an automatic explanation may run now, given the minimum gap and hourly cap.
func (l *limiter) allow(now time.Time, gap time.Duration, perHour int) (bool, string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	cut := now.Add(-time.Hour)
	keep := l.times[:0]
	for _, t := range l.times {
		if t.After(cut) {
			keep = append(keep, t)
		}
	}
	l.times = keep
	if n := len(l.times); n > 0 && now.Sub(l.times[n-1]) < gap {
		return false, "waiting: last automatic explanation was under " + gap.String() + " ago"
	}
	if len(l.times) >= perHour {
		return false, "paused: hourly limit reached"
	}
	return true, ""
}

func (l *limiter) record(now time.Time) {
	l.mu.Lock()
	l.times = append(l.times, now)
	l.mu.Unlock()
}

func (l *limiter) countLastHour(now time.Time) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := 0
	for _, t := range l.times {
		if now.Sub(t) < time.Hour {
			n++
		}
	}
	return n
}
