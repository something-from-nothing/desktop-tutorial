package main

import (
	"encoding/json"
	"errors"
	neturl "net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// The boot.dev window is started with a local debugging port. This file talks to it
// (Chrome DevTools Protocol) to add the reader script and receive what it sends back.

type cdpConn struct {
	id      string
	ws      *wsConn
	wmu     sync.Mutex
	next    int64
	pmu     sync.Mutex
	pending map[int64]chan json.RawMessage
	dead    atomic.Bool
}

var (
	cdpMu    sync.Mutex
	cdpPort  int
	cdpConns = map[string]*cdpConn{}
)

func setDevPort(p int) {
	cdpMu.Lock()
	cdpPort = p
	cdpMu.Unlock()
}

func devPort() int {
	cdpMu.Lock()
	defer cdpMu.Unlock()
	return cdpPort
}

type target struct {
	ID    string `json:"id"`
	Type  string `json:"type"`
	URL   string `json:"url"`
	WSURL string `json:"webSocketDebuggerUrl"`
}

func listTargets(port int) ([]target, error) {
	resp, err := quick.Get("http://127.0.0.1:" + itoa(port) + "/json/list")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var ts []target
	err = json.NewDecoder(resp.Body).Decode(&ts)
	return ts, err
}

// cdpLoop keeps a connection to every boot.dev page in the boot.dev window.
func cdpLoop() {
	for range time.Tick(1500 * time.Millisecond) {
		port := devPort()
		if port == 0 {
			continue
		}
		ts, err := listTargets(port)
		if err != nil {
			continue
		}
		seen := map[string]bool{}
		for _, t := range ts {
			if t.Type != "page" || t.WSURL == "" {
				continue
			}
			seen[t.ID] = true
			cdpMu.Lock()
			c := cdpConns[t.ID]
			cdpMu.Unlock()
			if c != nil && !c.dead.Load() {
				continue
			}
			if nc, err := attach(t); err == nil {
				cdpMu.Lock()
				cdpConns[t.ID] = nc
				cdpMu.Unlock()
			}
		}
		cdpMu.Lock()
		for id, c := range cdpConns {
			if !seen[id] || c.dead.Load() {
				if !c.dead.Load() {
					c.ws.Close()
				}
				delete(cdpConns, id)
			}
		}
		cdpMu.Unlock()
	}
}

func attach(t target) (*cdpConn, error) {
	ws, err := wsDial(t.WSURL, 4*time.Second)
	if err != nil {
		return nil, err
	}
	c := &cdpConn{id: t.ID, ws: ws, pending: map[int64]chan json.RawMessage{}}
	go c.readLoop()
	steps := []struct {
		m string
		p any
	}{
		{"Runtime.enable", map[string]any{}},
		{"Runtime.addBinding", map[string]any{"name": "__bdHost"}},
		{"Page.enable", map[string]any{}},
		{"Page.addScriptToEvaluateOnNewDocument", map[string]any{"source": readerJS}},
		{"Runtime.evaluate", map[string]any{"expression": readerJS}},
	}
	for _, s := range steps {
		if _, err := c.call(s.m, s.p, 5*time.Second); err != nil {
			ws.Close()
			return nil, err
		}
	}
	return c, nil
}

func (c *cdpConn) call(method string, params any, timeout time.Duration) (json.RawMessage, error) {
	if c.dead.Load() {
		return nil, errors.New("closed")
	}
	id := atomic.AddInt64(&c.next, 1)
	ch := make(chan json.RawMessage, 1)
	c.pmu.Lock()
	c.pending[id] = ch
	c.pmu.Unlock()
	c.wmu.Lock()
	err := c.ws.WriteJSON(map[string]any{"id": id, "method": method, "params": params})
	c.wmu.Unlock()
	if err != nil {
		return nil, err
	}
	select {
	case r := <-ch:
		return r, nil
	case <-time.After(timeout):
		c.pmu.Lock()
		delete(c.pending, id)
		c.pmu.Unlock()
		return nil, errors.New("timeout")
	}
}

func (c *cdpConn) readLoop() {
	defer c.dead.Store(true)
	for {
		data, err := c.ws.ReadMessage()
		if err != nil {
			return
		}
		var m struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			Result json.RawMessage `json:"result"`
		}
		if json.Unmarshal(data, &m) != nil {
			continue
		}
		if m.ID != 0 {
			c.pmu.Lock()
			ch := c.pending[m.ID]
			delete(c.pending, m.ID)
			c.pmu.Unlock()
			if ch != nil {
				ch <- m.Result
			}
			continue
		}
		if m.Method == "Runtime.bindingCalled" {
			var p struct {
				Name    string `json:"name"`
				Payload string `json:"payload"`
			}
			if json.Unmarshal(m.Params, &p) == nil && p.Name == "__bdHost" {
				pushEvent([]byte(p.Payload), true)
			}
		}
	}
}

func liveConns() []*cdpConn {
	cdpMu.Lock()
	defer cdpMu.Unlock()
	var out []*cdpConn
	for _, c := range cdpConns {
		if !c.dead.Load() {
			out = append(out, c)
		}
	}
	return out
}

// cdpEvalAll runs a small expression in every boot.dev page (used for signing in).
func cdpEvalAll(expr string) {
	for _, c := range liveConns() {
		_, _ = c.call("Runtime.evaluate", map[string]any{"expression": expr}, 5*time.Second)
	}
}

// openBootDev shows a boot.dev page: in the open window if there is one, else a new window.
func openBootDev(url string) {
	if !strings.HasPrefix(url, "https://www.boot.dev/") && !strings.HasPrefix(url, "https://boot.dev/") && !sameHost(url, startURL) {
		url = startURL
	}
	if cs := liveConns(); len(cs) > 0 {
		_, _ = cs[0].call("Page.navigate", map[string]any{"url": url}, 5*time.Second)
		_, _ = cs[0].call("Page.bringToFront", map[string]any{}, 3*time.Second)
		return
	}
	if usesOwnChrome() {
		openDefault(url) // your normal browser, where the extension is
		return
	}
	go startBootDevWindow(url)
}

func itoa(i int) string {
	b, _ := json.Marshal(i)
	return string(b)
}

func sameHost(a, b string) bool {
	ua, e1 := neturl.Parse(a)
	ub, e2 := neturl.Parse(b)
	return e1 == nil && e2 == nil && ua.Scheme == ub.Scheme && ua.Host == ub.Host
}
