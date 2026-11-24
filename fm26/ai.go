package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"strings"
	"sync"
	"time"
)

// LLM Chat's local link (the same one the Boot.dev Dashboard uses). Only answers on this PC.
// Screen pictures need LLM Chat's image update (see "LLM Chat image link prompt.md"); until
// LLM Chat has it, pictures go to Claude directly with the backup key.
var bridgeBase = "http://127.0.0.1:47821/bridge/v1"
var claudeURL = "https://api.anthropic.com/v1/messages"

const aiSource = "fm26-dashboard"

// Content is either a plain string or a list of Claude content blocks (text and image).
type aiMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

type aiRequest struct {
	System    string      `json:"system"`
	Messages  []aiMessage `json:"messages"`
	MaxTokens int         `json:"max_tokens"`
	Model     string      `json:"model"`
	Source    string      `json:"source"`
}

type aiResult struct {
	OK    bool   `json:"ok"`
	Text  string `json:"text,omitempty"`
	Via   string `json:"via,omitempty"`
	Model string `json:"model,omitempty"`
	Error string `json:"error,omitempty"`
}

var (
	quick = &http.Client{Timeout: 2 * time.Second}
	slow  = &http.Client{Timeout: 240 * time.Second}
)

func textMsg(role, text string) aiMessage {
	b, _ := json.Marshal(text)
	return aiMessage{Role: role, Content: b}
}

func hasImage(req aiRequest) bool {
	for _, m := range req.Messages {
		if len(m.Content) > 0 && m.Content[0] == '[' && bytes.Contains(m.Content, []byte(`"image"`)) {
			return true
		}
	}
	return false
}

func askAI(req aiRequest) aiResult {
	s := snapshotSettings()
	if req.MaxTokens <= 0 || req.MaxTokens > 8000 {
		req.MaxTokens = 1400
	}
	if req.Model == "" {
		req.Model = s.Model
	}
	req.Source = aiSource
	img := hasImage(req)
	claudeKey := unseal(s.ClaudeEnc)
	var bridgeErr string
	if s.BridgeKey != "" {
		st := bridgeInfo(s.BridgeKey)
		if img && st.reachable && !st.images {
			bridgeErr = "LLM Chat is linked but can't take screen pictures yet. Give LLM Chat the image update (the prompt is in your FM26 Dashboard folder), or add a Claude key as a backup."
		} else {
			r, reachable := askBridge(s.BridgeKey, req)
			if r.OK {
				return r
			}
			bridgeErr = r.Error
			if reachable && !strings.Contains(strings.ToLower(r.Error), "key") && !img && claudeKey == "" {
				return r
			}
		}
	}
	if claudeKey != "" {
		return askClaude(claudeKey, req)
	}
	if bridgeErr == "" {
		bridgeErr = "Add the LLM Chat link key (or a Claude key) in Settings."
	}
	return aiResult{OK: false, Error: bridgeErr}
}

func askBridge(key string, req aiRequest) (aiResult, bool) {
	body, _ := json.Marshal(map[string]any{
		"key": key, "system": req.System, "messages": req.Messages,
		"max_tokens": req.MaxTokens, "model": req.Model, "source": aiSource,
	})
	resp, err := slow.Post(bridgeBase+"/chat", "application/json", bytes.NewReader(body))
	if err != nil {
		return aiResult{OK: false, Error: "LLM Chat isn't open (or isn't linked yet). Open LLM Chat, or add a Claude key as a backup."}, false
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	var out aiResult
	if json.Unmarshal(b, &out) != nil {
		return aiResult{OK: false, Error: fmt.Sprintf("LLM Chat replied with something unexpected (HTTP %d)", resp.StatusCode)}, true
	}
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		out.OK = false
		if out.Error == "" {
			out.Error = "LLM Chat rejected the link key. Copy it again from LLM Chat settings."
		}
	}
	if out.OK {
		out.Via = "llmchat"
	}
	return out, true
}

func askClaude(key string, req aiRequest) aiResult {
	body, _ := json.Marshal(map[string]any{
		"model": req.Model, "max_tokens": req.MaxTokens, "system": req.System, "messages": req.Messages,
	})
	hr, _ := http.NewRequest("POST", claudeURL, bytes.NewReader(body))
	hr.Header.Set("content-type", "application/json")
	hr.Header.Set("x-api-key", key)
	hr.Header.Set("anthropic-version", "2023-06-01")
	resp, err := slow.Do(hr)
	if err != nil {
		return aiResult{OK: false, Error: strings.ReplaceAll(err.Error(), key, "<key>")}
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	var out struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		Model string `json:"model"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	_ = json.Unmarshal(b, &out)
	if resp.StatusCode != 200 {
		msg := fmt.Sprintf("Claude returned HTTP %d", resp.StatusCode)
		if out.Error != nil && out.Error.Message != "" {
			msg += ": " + out.Error.Message
		}
		return aiResult{OK: false, Error: msg}
	}
	var sb strings.Builder
	for _, c := range out.Content {
		if c.Type == "text" {
			sb.WriteString(c.Text)
		}
	}
	if strings.TrimSpace(sb.String()) == "" {
		return aiResult{OK: false, Error: "Claude sent back an empty reply. Try again, or pick another model in Settings."}
	}
	return aiResult{OK: true, Text: sb.String(), Via: "claude", Model: out.Model}
}

type bridgeState struct {
	reachable, linked, images bool
	at                        time.Time
}

var (
	brMu    sync.Mutex
	brCache bridgeState
	brKey   string
)

// bridgeInfo pings LLM Chat (cached for 10 seconds). LLM Chat lists "images" in its ping
// reply once it has the image update.
func bridgeInfo(key string) bridgeState {
	brMu.Lock()
	if brKey == key && time.Since(brCache.at) < 10*time.Second {
		c := brCache
		brMu.Unlock()
		return c
	}
	brMu.Unlock()
	st := bridgeState{at: time.Now()}
	if key != "" {
		if resp, err := quick.Get(bridgeBase + "/ping?key=" + neturl.QueryEscape(key)); err == nil {
			st.reachable = true
			var p struct {
				OK       bool     `json:"ok"`
				Features []string `json:"features"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&p)
			resp.Body.Close()
			st.linked = resp.StatusCode == 200 && p.OK
			for _, f := range p.Features {
				if f == "images" {
					st.images = true
				}
			}
		}
	}
	brMu.Lock()
	brCache, brKey = st, key
	brMu.Unlock()
	return st
}

func aiStatus() map[string]bool {
	s := snapshotSettings()
	st := bridgeInfo(s.BridgeKey)
	return map[string]bool{"llmchat": st.linked, "llmchatImages": st.linked && st.images, "claude": s.ClaudeEnc != ""}
}
