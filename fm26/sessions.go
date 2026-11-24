package main

import (
	"sort"
	"sync"
	"time"
)

// The app's own session log. Steam doesn't track streaks, so this does: every time the demo
// runs, a session is written down, and the streak is worked out from the days with a session.

type session struct {
	Start int64 `json:"s"` // unix seconds
	End   int64 `json:"e"` // unix seconds; 0 while it's still going
}

var (
	sesMu     sync.Mutex
	sessions  []session
	openStart int64 // start of the session going on now, 0 if none
	lastBeat  int64
	running   bool
	runSource string // "registry" or "steam api"
)

const minStreakSession = 60 // seconds of play that count towards a streak day

func loadSessions() {
	sesMu.Lock()
	defer sesMu.Unlock()
	_ = storeLoad("sessions", &sessions)
	var open struct {
		Start, Beat int64
	}
	if storeLoad("session:open", &open) && open.Start > 0 {
		// The app closed while the demo was running. Keep the session going if the demo
		// still runs within two minutes; otherwise close it at the last heartbeat.
		openStart, lastBeat = open.Start, open.Beat
	}
}

func saveSessionsLocked() {
	if len(sessions) > 3000 {
		sessions = sessions[len(sessions)-3000:]
	}
	storeJSON("sessions", sessions)
	if openStart > 0 {
		storeJSON("session:open", map[string]int64{"Start": openStart, "Beat": lastBeat})
	} else {
		storeSet("session:open", "")
	}
}

// observe is called every few seconds with whether the demo is running.
func observe(now time.Time, isRunning bool, source string) {
	sesMu.Lock()
	defer sesMu.Unlock()
	t := now.Unix()
	running, runSource = isRunning, source
	if openStart > 0 && !isRunning {
		end := lastBeat
		if end < openStart {
			end = openStart
		}
		if t-lastBeat < 30 {
			end = t
		}
		sessions = append(sessions, session{Start: openStart, End: end})
		openStart = 0
		saveSessionsLocked()
		return
	}
	if isRunning {
		if openStart > 0 && t-lastBeat > 120 {
			// A gap of more than two minutes (PC asleep, app closed): that was a separate session.
			sessions = append(sessions, session{Start: openStart, End: lastBeat})
			openStart = 0
		}
		if openStart == 0 {
			openStart = t
			lastBeat = t
			saveSessionsLocked()
			return
		}
		prevBeat := lastBeat
		lastBeat = t
		if t-prevBeat >= 30 || t/30 != prevBeat/30 {
			saveSessionsLocked()
		}
	}
}

func gameRunning() bool {
	sesMu.Lock()
	defer sesMu.Unlock()
	return running
}

type sessionInfo struct {
	Running      bool     `json:"running"`
	Source       string   `json:"source,omitempty"`
	SessionStart int64    `json:"sessionStart,omitempty"`
	SessionSec   int64    `json:"sessionSec"`
	LoggedSec    int64    `json:"loggedSec"` // total time in the app's own log
	LastPlayed   int64    `json:"lastPlayed,omitempty"`
	Streak       int      `json:"streak"`
	BestStreak   int      `json:"bestStreak"`
	PlayedToday  bool     `json:"playedToday"`
	Days         []string `json:"days"` // the last 28 days, "1" played / "0" not, oldest first
	Count        int      `json:"count"`
}

func sessionSnapshot(now time.Time) sessionInfo {
	sesMu.Lock()
	all := append([]session(nil), sessions...)
	if openStart > 0 {
		all = append(all, session{Start: openStart, End: now.Unix()})
	}
	si := sessionInfo{Running: running, Source: runSource, Count: len(all)}
	if openStart > 0 && running {
		si.SessionStart = openStart
		si.SessionSec = now.Unix() - openStart
	}
	sesMu.Unlock()
	for _, s := range all {
		if s.End > s.Start {
			si.LoggedSec += s.End - s.Start
		}
		if s.End > si.LastPlayed {
			si.LastPlayed = s.End
		}
	}
	si.Streak, si.BestStreak, si.PlayedToday, si.Days = streakOf(all, now, time.Local)
	return si
}

func dayKey(t time.Time) string { return t.Format("2006-01-02") }

// streakOf counts days in a row with at least a minute of play. A streak that ended yesterday
// still counts until today ends (you can keep it alive by playing today).
func streakOf(all []session, now time.Time, loc *time.Location) (cur, best int, today bool, last28 []string) {
	played := map[string]int64{}
	for _, s := range all {
		if s.End <= s.Start {
			continue
		}
		// Split sessions that cross midnight between the two days.
		a, b := time.Unix(s.Start, 0).In(loc), time.Unix(s.End, 0).In(loc)
		for a.Before(b) {
			next := time.Date(a.Year(), a.Month(), a.Day()+1, 0, 0, 0, 0, loc)
			end := b
			if next.Before(b) {
				end = next
			}
			played[dayKey(a)] += int64(end.Sub(a).Seconds())
			a = end
		}
	}
	isDay := func(t time.Time) bool { return played[dayKey(t)] >= minStreakSession }
	n := now.In(loc)
	day := time.Date(n.Year(), n.Month(), n.Day(), 12, 0, 0, 0, loc)
	today = isDay(day)
	d := day
	if !today {
		d = day.AddDate(0, 0, -1)
	}
	for isDay(d) {
		cur++
		d = d.AddDate(0, 0, -1)
	}
	keys := make([]string, 0, len(played))
	for k, v := range played {
		if v >= minStreakSession {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	run := 0
	var prev time.Time
	for _, k := range keys {
		t, _ := time.ParseInLocation("2006-01-02", k, loc)
		t = t.Add(12 * time.Hour)
		if run > 0 && dayKey(prev.AddDate(0, 0, 1)) == k {
			run++
		} else {
			run = 1
		}
		if run > best {
			best = run
		}
		prev = t
	}
	for i := 27; i >= 0; i-- {
		if isDay(day.AddDate(0, 0, -i)) {
			last28 = append(last28, "1")
		} else {
			last28 = append(last28, "0")
		}
	}
	return
}
