package main

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"
)

type explainRecord struct {
	ID          int64     `json:"id"`
	At          int64     `json:"at"`
	Auto        bool      `json:"auto"`
	Screen      string    `json:"screen,omitempty"`
	Explanation string    `json:"explanation,omitempty"`
	Next        []string  `json:"next,omitempty"`
	Topics      []string  `json:"topics,omitempty"`
	Mistakes    []mistake `json:"mistakes,omitempty"`
	Via         string    `json:"via,omitempty"`
	Thumb       string    `json:"thumb,omitempty"`
	Window      string    `json:"window,omitempty"`
	Error       string    `json:"error,omitempty"`
	Chat        []chatMsg `json:"chat,omitempty"`
}

type chatMsg struct {
	Q string `json:"q"`
	A string `json:"a"`
}

var (
	exMu      sync.Mutex
	exBusy    bool
	exLatest  *explainRecord
	exHistory []explainRecord // newest first, no thumbnails
	exLastFP  fingerprint     // fingerprint of the last screen that was explained
	autoLimit limiter
	autoNote  string
	errBusy   = errors.New("already explaining a screen")
)

func loadExplains() {
	exMu.Lock()
	defer exMu.Unlock()
	_ = storeLoad("explains", &exHistory)
	if len(exHistory) > 0 {
		e := exHistory[0]
		exLatest = &e
	}
}

// explainNow captures the Football Manager window and asks for an explanation.
// auto is true when it was started by auto mode rather than the button or hotkey.
func explainNow(auto bool) (*explainRecord, error) {
	exMu.Lock()
	if exBusy {
		exMu.Unlock()
		return nil, errBusy
	}
	exBusy = true
	exMu.Unlock()
	defer func() { exMu.Lock(); exBusy = false; exMu.Unlock() }()

	rec := &explainRecord{ID: time.Now().UnixNano(), At: time.Now().Unix(), Auto: auto}
	img, title, err := captureGame()
	if err != nil {
		rec.Error = err.Error()
		publishExplain(rec, false)
		return rec, err
	}
	rec.Window = title
	fp := fingerprintOf(img)
	if isBlank(fp) {
		rec.Error = "The capture came back blank. Football Manager is probably in exclusive full screen: in FM, open Preferences, then Interface (or Display), and set it to Borderless or Windowed."
		publishExplain(rec, false)
		return rec, errors.New(rec.Error)
	}
	jpg, small := prepImage(img, 1568)
	rec.Thumb = thumbJPEG(small, 360)

	content := []map[string]any{
		{"type": "image", "source": map[string]string{"type": "base64", "media_type": "image/jpeg", "data": base64.StdEncoding.EncodeToString(jpg)}},
		{"type": "text", "text": explainContext() + "Explain this Football Manager screen."},
	}
	cb, _ := json.Marshal(content)
	s := snapshotSettings()
	res := askAI(aiRequest{
		System:    strings.Replace(explainSystem, "TOPICS", strings.Join(fmTopics, ", "), 1),
		Messages:  []aiMessage{{Role: "user", Content: cb}},
		MaxTokens: 2000,
		Model:     s.Model,
	})
	if !res.OK {
		rec.Error = res.Error
		publishExplain(rec, false)
		return rec, errors.New(res.Error)
	}
	out, perr := parseExplain(res.Text)
	if perr != nil {
		// Still show what came back, as plain text.
		rec.Explanation = strings.TrimSpace(res.Text)
		rec.Via = res.Via
		publishExplain(rec, true)
		return rec, nil
	}
	rec.Screen, rec.Explanation, rec.Next, rec.Topics, rec.Mistakes, rec.Via = out.Screen, out.Explanation, out.Next, out.Topics, out.Mistakes, res.Via

	// Club facts and the in game date.
	clubMu.Lock()
	cs := clubLoad()
	mergeClub(&cs, out, rec.At)
	storeJSON("club", cs)
	clubMu.Unlock()
	if cs.GameDate != nil && s.DemoStart == "" {
		// The first date seen is taken as the start of the demo (you can change it in Settings).
		d := cs.GameDate.V
		_ = updateSettings(func(n *settings) error {
			if n.DemoStart == "" {
				n.DemoStart = d
			}
			return nil
		})
	}
	// Weak topics: a screen you asked about counts as an ask; problems on screen count double.
	if !auto {
		for _, t := range rec.Topics {
			weakBump(t, "ask", "")
		}
	}
	for _, m := range rec.Mistakes {
		if strings.TrimSpace(m.Note) != "" {
			weakBump(m.Topic, "mistake", m.Note)
		}
	}
	exMu.Lock()
	exLastFP = fp
	exMu.Unlock()
	publishExplain(rec, true)
	return rec, nil
}

// explainContext gives the AI a little memory: the club so far and the weakest topics.
func explainContext() string {
	var b strings.Builder
	clubMu.Lock()
	cs := clubLoad()
	clubMu.Unlock()
	if cs.Name != nil {
		b.WriteString("His club: " + cs.Name.V)
		if cs.League != nil {
			b.WriteString(" (" + cs.League.V + ")")
		}
		b.WriteString(". ")
	}
	if w := weakRanked(); len(w) > 0 {
		var names []string
		for i, t := range w {
			if i >= 3 {
				break
			}
			names = append(names, t.Topic)
		}
		b.WriteString("Topics he finds hardest: " + strings.Join(names, ", ") + ". ")
	}
	return b.String()
}

func publishExplain(rec *explainRecord, ok bool) {
	exMu.Lock()
	defer exMu.Unlock()
	if !ok && exLatest != nil && exLatest.Error == "" && rec.Auto {
		// A failed automatic try shouldn't hide the last good explanation.
		autoNote = rec.Error
		return
	}
	exLatest = rec
	if ok {
		h := *rec
		h.Thumb = ""
		exHistory = append([]explainRecord{h}, exHistory...)
		if len(exHistory) > 15 {
			exHistory = exHistory[:15]
		}
		storeJSON("explains", exHistory)
	}
	bumpSeq()
}

// askAboutScreen answers a follow up question about the latest explained screen (text only:
// the picture isn't sent again, which keeps it cheap).
func askAboutScreen(q string) (string, error) {
	q = strings.TrimSpace(q)
	if q == "" {
		return "", errors.New("empty question")
	}
	exMu.Lock()
	var base explainRecord
	if exLatest != nil {
		base = *exLatest
	}
	exMu.Unlock()
	var msgs []aiMessage
	ctx := explainContext()
	if base.Explanation != "" {
		ctx += "He is on the " + base.Screen + " screen. You already told him:\n" + base.Explanation + "\nNext steps you gave: " + strings.Join(base.Next, "; ")
	}
	chat := base.Chat
	if len(chat) > 6 {
		chat = chat[len(chat)-6:]
	}
	for _, c := range chat {
		msgs = append(msgs, textMsg("user", c.Q), textMsg("assistant", c.A))
	}
	first := q
	if len(msgs) == 0 {
		first = ctx + "\n\nHis question: " + q
	} else {
		msgs[0] = textMsg("user", ctx+"\n\nHis question: "+chat[0].Q)
	}
	msgs = append(msgs, textMsg("user", first))
	sys := "You are a Football Manager 26 coach helping Pete, who is fairly new to the game and is playing the FM26 Demo. Answer in plain British English, short and practical, markdown allowed. Start your reply with a line TOPIC: <one of " + strings.Join(fmTopics, ", ") + "> and then the answer."
	res := askAI(aiRequest{System: sys, Messages: msgs, MaxTokens: 900})
	if !res.OK {
		return "", errors.New(res.Error)
	}
	ans := strings.TrimSpace(res.Text)
	topic := ""
	if strings.HasPrefix(strings.ToUpper(ans), "TOPIC:") {
		line, rest, _ := strings.Cut(ans, "\n")
		topic = strings.TrimSpace(line[6:])
		ans = strings.TrimSpace(rest)
	}
	if topic != "" {
		weakBump(topic, "ask", "")
	} else {
		for _, t := range base.Topics {
			weakBump(t, "ask", "")
		}
	}
	exMu.Lock()
	if exLatest != nil && exLatest.ID == base.ID {
		exLatest.Chat = append(exLatest.Chat, chatMsg{q, ans})
		if len(exHistory) > 0 && exHistory[0].ID == base.ID {
			exHistory[0].Chat = exLatest.Chat
			storeJSON("explains", exHistory)
		}
	}
	exMu.Unlock()
	bumpSeq()
	return ans, nil
}

// autoLoop: when auto mode is on and the demo is running, look at the game window every
// 3 seconds; when the screen has changed and settled, explain it, within the limits.
func autoLoop() {
	var prev fingerprint
	stable := 0
	for range time.Tick(3 * time.Second) {
		s := snapshotSettings()
		if s.Auto != "on" {
			setAutoNote("")
			prev, stable = nil, 0
			continue
		}
		if !gameRunning() {
			setAutoNote("waiting for the demo to start")
			continue
		}
		if !gameIsForeground() {
			setAutoNote("waiting: Football Manager isn't the active window")
			continue
		}
		img, _, err := captureGame()
		if err != nil {
			setAutoNote(err.Error())
			continue
		}
		fp := fingerprintOf(img)
		if isBlank(fp) {
			continue
		}
		if prev != nil && fp.diff(prev) < screenSettled {
			stable++
		} else {
			stable = 0
		}
		prev = fp
		exMu.Lock()
		last := exLastFP
		exMu.Unlock()
		// Changed from the last explained screen, and the same for two looks in a row (6 seconds).
		if stable < 2 || (last != nil && fp.diff(last) < screenChanged) {
			continue
		}
		now := time.Now()
		ok, why := autoLimit.allow(now, time.Duration(s.AutoGap)*time.Second, s.AutoPerHour)
		if !ok {
			setAutoNote(why)
			continue
		}
		autoLimit.record(now)
		setAutoNote("")
		_, _ = explainNow(true)
		stable = 0
	}
}

func setAutoNote(n string) {
	exMu.Lock()
	changed := autoNote != n
	autoNote = n
	exMu.Unlock()
	if changed {
		bumpSeq()
	}
}

// hotkeyPressed runs when Ctrl+Shift+E is pressed anywhere.
var lastHotkey time.Time

func hotkeyPressed() {
	if time.Since(lastHotkey) < 3*time.Second {
		return
	}
	lastHotkey = time.Now()
	go func() { _, _ = explainNow(false) }()
}

// ---- club store ----

var clubMu sync.Mutex

func clubLoad() clubState {
	var cs clubState
	_ = storeLoad("club", &cs)
	return cs
}
