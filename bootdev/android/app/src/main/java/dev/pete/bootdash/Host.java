package dev.pete.bootdash;

import android.content.Context;
import android.content.SharedPreferences;
import android.webkit.JavascriptInterface;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Everything between the boot.dev page, the dashboard page and the phone. */
final class Host {
    private final MainActivity act;
    private final SharedPreferences prefs, store;
    private final ExecutorService pool = Executors.newFixedThreadPool(2);

    // Events from the reader, newest last.
    private long seq = 0;
    private final List<Object[]> ring = new ArrayList<>(); // {Long seq, String json}
    private String latestPage = null;
    private final LinkedHashMap<String, String> lessons = new LinkedHashMap<>();
    private long lastFill = 0, fillWindow = 0;
    private int fills = 0;

    final BootSide bootSide = new BootSide();
    final DashSide dashSide = new DashSide();

    Host(MainActivity act) {
        this.act = act;
        prefs = act.getSharedPreferences("bootdash", Context.MODE_PRIVATE);
        store = act.getSharedPreferences("bootdash_store", Context.MODE_PRIVATE);
    }

    void shutdown() { pool.shutdownNow(); }

    private synchronized void push(String json) {
        JSONObject o;
        try { o = new JSONObject(json); } catch (Exception e) { return; }
        String type = o.optString("type");
        if (type.isEmpty()) return;
        if (type.equals("page") && !o.optString("url").startsWith("https://www.boot.dev/") && !o.optString("url").startsWith("https://boot.dev/")) return;
        seq++;
        ring.add(new Object[]{seq, json});
        if (ring.size() > 600) ring.subList(0, 100).clear();
        if (type.equals("page")) {
            latestPage = json;
            if (o.optBoolean("login")) maybeSignIn();
        } else if (type.equals("lesson_data")) {
            String id = o.optString("id");
            lessons.remove(id);
            lessons.put(id, json);
            if (lessons.size() > 30) {
                Iterator<String> it = lessons.keySet().iterator();
                it.next();
                it.remove();
            }
        }
    }

    private synchronized String events(long since) {
        StringBuilder sb = new StringBuilder("{\"events\":[");
        long last = since;
        int n = 0;
        if (since <= 0) {
            for (String l : lessons.values()) { if (n++ > 0) sb.append(','); sb.append(l); }
            if (latestPage != null) { if (n++ > 0) sb.append(','); sb.append(latestPage); }
            last = seq;
        } else {
            for (Object[] e : ring) {
                long s = (Long) e[0];
                if (s <= since) continue;
                if (n >= 300) break;
                if (n++ > 0) sb.append(',');
                sb.append((String) e[1]);
                last = s;
            }
        }
        return sb.append("],\"seq\":").append(last).append('}').toString();
    }

    /** Fills boot.dev's sign in form with the saved account (a few tries at most). */
    private void maybeSignIn() {
        long now = System.currentTimeMillis();
        if (now - lastFill < 20000) return;
        if (now - fillWindow > 300000) { fillWindow = now; fills = 0; }
        if (fills >= 4) return;
        final String email = prefs.getString("email", "");
        final String pass = Secrets.open(act, prefs.getString("passwordEnc", ""));
        if (email.isEmpty() || pass.isEmpty()) return;
        fills++;
        lastFill = now;
        final String js = "window.__bdFill ? window.__bdFill(" + JSONObject.quote(email) + "," + JSONObject.quote(pass) + ") : 'noreader'";
        act.boot.postDelayed(() -> act.boot.evaluateJavascript(js, null), 700);
        act.boot.postDelayed(() -> act.boot.evaluateJavascript(
            "(document.querySelector('input[type=password]') && !document.querySelector('input[type=password]').value) ? " + js + " : 'done'", null), 3200);
    }

    private JSONObject publicSettings() {
        JSONObject o = new JSONObject();
        try {
            o.put("email", prefs.getString("email", ""));
            o.put("hasPassword", !prefs.getString("passwordEnc", "").isEmpty());
            o.put("bridgeKey", prefs.getString("bridgeKey", ""));
            o.put("hasClaudeKey", !prefs.getString("claudeEnc", "").isEmpty());
            o.put("model", prefs.getString("model", "claude-sonnet-5-5"));
            o.put("level", prefs.getString("level", "hints"));
            o.put("auto", prefs.getString("auto", "on"));
        } catch (Exception e) { }
        return o;
    }

    private void done(String id, String json) {
        final String js = "window.__aiDone(" + JSONObject.quote(id) + "," + JSONObject.quote(json) + ")";
        act.dash.post(() -> act.dash.evaluateJavascript(js, null));
    }

    final class BootSide {
        @JavascriptInterface public void push(String json) { Host.this.push(json); }
    }

    final class DashSide {
        @JavascriptInterface public String events(String since) {
            long s = 0;
            try { s = Long.parseLong(since); } catch (Exception e) { }
            return Host.this.events(s);
        }

        @JavascriptInterface public String getSettings() { return publicSettings().toString(); }

        @JavascriptInterface public String saveSettings(String json) {
            try {
                JSONObject in = new JSONObject(json);
                SharedPreferences.Editor ed = prefs.edit();
                for (String k : new String[]{"email", "bridgeKey", "model", "level", "auto"}) {
                    if (in.has(k)) ed.putString(k, in.optString(k));
                }
                if (!in.optString("password").isEmpty()) ed.putString("passwordEnc", Secrets.seal(act, in.optString("password")));
                String bk = in.optString("bridgeKey").trim();
                if (bk.startsWith("sk-ant-")) {
                    // A Claude API key pasted into the link box: keep it encrypted as the Claude key.
                    ed.putString("bridgeKey", "");
                    if (in.optString("claudeKey").isEmpty()) in.put("claudeKey", bk);
                }
                if (!in.optString("claudeKey").isEmpty()) ed.putString("claudeEnc", Secrets.seal(act, in.optString("claudeKey")));
                ed.apply();
                return "{\"ok\":true}";
            } catch (Exception e) {
                return "{\"ok\":false,\"error\":" + JSONObject.quote(String.valueOf(e.getMessage())) + "}";
            }
        }

        @JavascriptInterface public String storeGet(String k) { return store.getString(k, ""); }

        @JavascriptInterface public void storeSet(String k, String v) {
            if (v == null || v.isEmpty()) store.edit().remove(k).apply();
            else store.edit().putString(k, v).apply();
        }

        @JavascriptInterface public void ai(final String id, final String req) {
            pool.execute(() -> {
                String key = prefs.getString("bridgeKey", "");
                String claude = Secrets.open(act, prefs.getString("claudeEnc", ""));
                done(id, Ai.ask(act, key, claude, req, prefs.getString("model", "claude-sonnet-5-5")).toString());
            });
        }

        @JavascriptInterface public void bridgeStatus(final String id) {
            pool.execute(() -> {
                JSONObject o = new JSONObject();
                try {
                    o.put("llmchat", Ai.ping(act, prefs.getString("bridgeKey", "")));
                    o.put("claude", !prefs.getString("claudeEnc", "").isEmpty());
                } catch (Exception e) { }
                done(id, o.toString());
            });
        }

        @JavascriptInterface public void openBoot(final String url) {
            act.runOnUiThread(() -> act.openBoot(url));
        }
    }

}
