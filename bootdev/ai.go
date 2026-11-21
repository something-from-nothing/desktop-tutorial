package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"strings"
	"time"
)

// LLM Chat's local link (added to LLM Chat with the bridge prompt). Only answers on this PC.
const bridgeBase = "http://127.0.0.1:47821/bridge/v1"

type aiMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
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

func askAI(req aiRequest) aiResult {
	s := snapshotSettings()
	if req.MaxTokens <= 0 || req.MaxTokens > 8000 {
		req.MaxTokens = 1400
	}
	if req.Model == "" {
		req.Model = s.Model
	}
	var bridgeErr string
	if s.BridgeKey != "" {
		r, reachable := askBridge(s.BridgeKey, req)
		if r.OK {
			return r
		}
		bridgeErr = r.Error
		if reachable && !strings.Contains(strings.ToLower(r.Error), "key") {
			// LLM Chat answered but failed (for example its own Claude key is missing): say so,
			// unless there is a direct key to fall back on.
			if s.ClaudeEnc == "" {
				return r
			}
		}
	}
	if k := unseal(s.ClaudeEnc); k != "" {
		return askClaude(k, req)
	}
	if bridgeErr == "" {
		bridgeErr = "Add the LLM Chat link key (or a Claude key) in Settings."
	}
	return aiResult{OK: false, Error: bridgeErr}
}

func askBridge(key string, req aiRequest) (aiResult, bool) {
	body, _ := json.Marshal(map[string]any{
		"key": key, "system": req.System, "messages": req.Messages,
		"max_tokens": req.MaxTokens, "model": req.Model, "source": "bootdev-dashboard",
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
	hr, _ := http.NewRequest("POST", "https://api.anthropic.com/v1/messages", bytes.NewReader(body))
	hr.Header.Set("content-type", "application/json")
	hr.Header.Set("x-api-key", key)
	hr.Header.Set("anthropic-version", "2023-06-01")
	resp, err := slow.Do(hr)
	if err != nil {
		return aiResult{OK: false, Error: err.Error()}
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

func aiStatus() map[string]bool {
	s := snapshotSettings()
	linked := false
	if s.BridgeKey != "" {
		if resp, err := quick.Get(bridgeBase + "/ping?key=" + neturl.QueryEscape(s.BridgeKey)); err == nil {
			var p struct {
				OK bool `json:"ok"`
			}
			_ = json.NewDecoder(resp.Body).Decode(&p)
			resp.Body.Close()
			linked = resp.StatusCode == 200 && p.OK
		}
	}
	return map[string]bool{"llmchat": linked, "claude": s.ClaudeEnc != ""}
}
