package main

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// Signing in to boot.dev inside the app's own window, with the email and password saved in
// Settings. It uses boot.dev's own email and password sign in (the same request boot.dev's
// sign in form sends), from inside the boot.dev page, so boot.dev keeps you signed in after.
// Google sign in can't work in this window: Google blocks browsers that apps control.

// signInJS is shared with the Android app (same request, same result shape).
func signInJS(email, pass string) string {
	e, _ := json.Marshal(email)
	p, _ := json.Marshal(pass)
	return fmt.Sprintf(`(async function (e, p) {
  var out;
  try {
    var r = await fetch('https://api.boot.dev/v1/auth/password/login', {
      method: 'POST', credentials: 'include', mode: 'cors',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: e, password: p })
    });
    var msg = '';
    try { var j = await r.json(); msg = j.error || j.message || ''; } catch (x) {}
    out = { type: 'login', ok: r.ok, status: r.status, error: String(msg).slice(0, 200) };
  } catch (err) {
    out = { type: 'login', ok: false, status: 0, error: String(err).slice(0, 200) };
  }
  if (out.ok) setTimeout(function () { location.href = 'https://www.boot.dev/dashboard'; }, 400);
  return JSON.stringify(out);
})(%s, %s)`, e, p)
}

var (
	signMu     sync.Mutex
	signLast   time.Time
	signWindow time.Time
	signTries  int
	signBadPw  string // the saved password that boot.dev rejected; not retried automatically
)

type signResult struct {
	OK     bool   `json:"ok"`
	Status int    `json:"status"`
	Error  string `json:"error,omitempty"`
}

func signIn(force bool) signResult {
	email, pass := savedLogin()
	if email == "" || pass == "" {
		return signResult{Error: "Add your boot.dev email and password in Settings first."}
	}
	signMu.Lock()
	if !force {
		if signBadPw == pass || time.Since(signLast) < 45*time.Second {
			signMu.Unlock()
			return signResult{Error: "waiting"}
		}
		if time.Since(signWindow) > 10*time.Minute {
			signWindow, signTries = time.Now(), 0
		}
		if signTries >= 3 {
			signMu.Unlock()
			return signResult{Error: "waiting"}
		}
		signTries++
	}
	signLast = time.Now()
	signMu.Unlock()

	// The boot.dev window must be open (it may be on a Google page after a Google attempt).
	if len(liveConns()) == 0 {
		go startBootDevWindow(startURL)
		for i := 0; i < 60 && len(liveConns()) == 0; i++ {
			time.Sleep(500 * time.Millisecond)
		}
	}
	cs := liveConns()
	if len(cs) == 0 {
		return report(signResult{Error: "The boot.dev window didn't open."})
	}
	c := cs[0]
	var cur struct {
		Result struct {
			Value string `json:"value"`
		} `json:"result"`
	}
	if raw, err := c.call("Runtime.evaluate", map[string]any{"expression": "location.href", "returnByValue": true}, 5*time.Second); err == nil {
		_ = json.Unmarshal(raw, &cur)
	}
	if !isBootDevURL(cur.Result.Value) {
		_, _ = c.call("Page.navigate", map[string]any{"url": "https://www.boot.dev/"}, 10*time.Second)
		time.Sleep(4 * time.Second)
	}
	raw, err := c.call("Runtime.evaluate", map[string]any{"expression": signInJS(email, pass), "awaitPromise": true, "returnByValue": true}, 30*time.Second)
	if err != nil {
		return report(signResult{Error: "Couldn't reach boot.dev: " + err.Error()})
	}
	var ev struct {
		Result struct {
			Value string `json:"value"`
		} `json:"result"`
	}
	_ = json.Unmarshal(raw, &ev)
	var res signResult
	if json.Unmarshal([]byte(ev.Result.Value), &res) != nil {
		res = signResult{Error: "boot.dev gave an unexpected answer."}
	}
	if !res.OK {
		switch res.Status {
		case 400, 401, 403, 404:
			signMu.Lock()
			signBadPw = pass
			signMu.Unlock()
			if res.Error == "" {
				res.Error = "boot.dev didn't accept that email and password."
			}
		case 429:
			res.Error = "Too many sign in attempts. Wait a minute, then press Sign in again."
		}
	}
	return report(res)
}

// report also shows the result on the dashboard.
func report(r signResult) signResult {
	b, _ := json.Marshal(map[string]any{"type": "login", "ok": r.OK, "status": r.Status, "error": r.Error, "at": time.Now().UnixMilli()})
	pushEvent(b, false)
	return r
}
