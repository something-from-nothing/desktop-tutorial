package dev.pete.fc26dash;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.webkit.JavascriptInterface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * Everything between the dashboard page and the outside world.
 *
 * Paired and the PC is on: every request goes to the PC over the home network (HTTPS, pinned
 * to the PC's own certificate, with this phone's token). The PC never needs to be reachable
 * from the internet.
 *
 * PC off or away from home: the phone answers itself. Steam stats come straight from the Steam
 * Web API, quizzes from LLM Chat on this phone (or Claude with a backup key), and everything
 * else (club, streak, weak topics) shows as it was the last time the PC was seen. Quiz results
 * are kept and sent to the PC next time it's reachable.
 */
final class Host {
    private static final int DISCOVERY_PORT = 47831;
    private static final String DISCOVERY_ASK = "FC26DASH?1";
    private static final String STEAM = "https://api.steampowered.com";
    // EA SPORTS FC 26 on Steam: the main listing, a second listing and the Showcase, or anything named FC 26.
    private static final Set<Integer> GAME_IDS = new HashSet<>(java.util.Arrays.asList(3405690, 4328110, 3629260));

    static boolean isGame(int id, String name) {
        if (GAME_IDS.contains(id)) return true;
        String n = (name == null ? "" : name).replace("\u2122", "").replace("\u00ae", "").toUpperCase().replaceAll("\\s+", " ").trim();
        return n.contains("EA SPORTS FC 26") || n.equals("FC 26");
    }

    private final MainActivity act;
    private final SharedPreferences prefs;
    private final ExecutorService pool = Executors.newFixedThreadPool(3);
    private volatile long pcFailedAt = 0;
    private volatile boolean pcOk = false;
    private volatile JSONObject steamCache = null;
    private volatile long steamAt = 0;

    Host(MainActivity act) {
        this.act = act;
        prefs = act.getSharedPreferences("fc26dash", Context.MODE_PRIVATE);
    }

    void shutdown() { pool.shutdownNow(); }

    // ---- the page's one entry point ----

    @JavascriptInterface public void call(final String id, final String method, final String path, final String body) {
        pool.execute(() -> {
            String out;
            try {
                out = route(method, path, body == null ? "" : body).toString();
            } catch (Exception e) {
                out = err(e.getMessage() == null ? e.toString() : e.getMessage()).toString();
            }
            act.done(id, out);
        });
    }

    private JSONObject route(String method, String path, String body) throws Exception {
        if (path.startsWith("/local/")) return local(path, body);
        JSONObject in = body.isEmpty() ? new JSONObject() : new JSONObject(body);
        boolean paired = !prefs.getString("pcHost", "").isEmpty();

        // Settings are split: AI keys for when the PC is off live on the phone; everything
        // else (auto mode and its limits) lives on the PC.
        if (path.equals("/api/settings")) {
            if (method.equals("POST")) {
                saveLocalSettings(in);
                if (paired && pcLikelyUp()) {
                    try { pc("POST", path, body, 8000); } catch (IOException e) { markDown(); }
                }
                return ok();
            }
            JSONObject s = new JSONObject();
            if (paired && pcLikelyUp()) {
                try { s = new JSONObject(pc("GET", path, "", 4000)); } catch (IOException e) { markDown(); }
            }
            s.put("bridgeKey", prefs.getString("bridgeKey", ""));
            s.put("hasClaudeKey", !prefs.getString("claudeEnc", "").isEmpty());
            if (!s.has("model")) s.put("model", prefs.getString("model", "claude-sonnet-5-5"));
            s.put("pc", prefs.getString("pcName", ""));
            s.put("pcReachable", pcOk);
            return s;
        }

        if (paired && pcLikelyUp()) {
            int timeout = (path.equals("/api/explain") || path.equals("/api/ask") || path.equals("/api/ai")) ? 240000 : 5000;
            try {
                String r = pc(method, path, body, timeout);
                if (path.equals("/api/state")) {
                    prefs.edit().putString("lastState", r).apply();
                    flushQueue();
                }
                return new JSONObject(r);
            } catch (IOException e) {
                markDown();
                pool.execute(this::rediscover);
            }
        }
        return offline(method, path, in);
    }

    // ---- when the PC is off ----

    private JSONObject offline(String method, String path, JSONObject in) throws Exception {
        boolean paired = !prefs.getString("pcHost", "").isEmpty();
        switch (path) {
            case "/api/state": {
                String last = prefs.getString("lastState", "");
                JSONObject st = last.isEmpty() ? new JSONObject() : new JSONObject(last);
                st.put("version", "phone");
                st.put("offline", true);
                st.put("paired", paired);
                st.put("pc", prefs.getString("pcName", ""));
                st.put("steamSignedIn", !prefs.getString("steamId", "").isEmpty());
                st.put("steamName", prefs.getString("steamName", ""));
                JSONObject steam = steamStats();
                if (steam != null) st.put("steam", steam);
                JSONObject ses = st.optJSONObject("session");
                if (ses == null) ses = new JSONObject();
                ses.put("running", steam != null && steam.optBoolean("playing"));
                ses.put("source", "steam api");
                st.put("session", ses);
                JSONObject ex = st.optJSONObject("explain");
                if (ex != null) { ex.put("busy", false); ex.put("autoNote", ""); }
                JSONObject ai = new JSONObject();
                ai.put("llmchat", Ai.ping(act, prefs.getString("bridgeKey", "")));
                ai.put("llmchatImages", false);
                ai.put("claude", !prefs.getString("claudeEnc", "").isEmpty());
                st.put("ai", ai);
                applyQueue(st);
                return st;
            }
            case "/api/ai": {
                String claude = Secrets.open(act, prefs.getString("claudeEnc", ""));
                return Ai.ask(act, prefs.getString("bridgeKey", ""), claude, in.toString(), prefs.getString("model", "claude-sonnet-5-5"));
            }
            case "/api/weak": {
                if (method.equals("POST") && !in.optBoolean("reset")) {
                    JSONArray q = new JSONArray(prefs.getString("weakQueue", "[]"));
                    q.put(in);
                    prefs.edit().putString("weakQueue", q.toString()).apply();
                }
                return ok();
            }
            case "/api/explain":
            case "/api/ask":
                return err(paired ? "Your PC isn't reachable. Explaining a screen needs the PC on, with FC26 Dashboard open, on the same WiFi." : "Pair with your PC first (Settings).");
            case "/api/steam/refresh":
                steamAt = 0;
                return ok();
            default:
                return err("Needs your PC.");
        }
    }

    /** Applies queued quiz results to the last known weak topics, so the phone shows them. */
    private void applyQueue(JSONObject st) throws Exception {
        JSONArray q = new JSONArray(prefs.getString("weakQueue", "[]"));
        if (q.length() == 0) return;
        JSONArray weak = st.optJSONArray("weak");
        if (weak == null) weak = new JSONArray();
        for (int i = 0; i < q.length(); i++) {
            JSONObject e = q.getJSONObject(i);
            String topic = e.optString("topic");
            JSONObject w = null;
            for (int j = 0; j < weak.length(); j++) if (weak.getJSONObject(j).optString("topic").equalsIgnoreCase(topic)) w = weak.getJSONObject(j);
            if (w == null) { w = new JSONObject(); w.put("topic", topic); w.put("score", 0); weak.put(w); }
            boolean right = "quizRight".equals(e.optString("kind"));
            w.put("score", Math.max(0, w.optInt("score") + (right ? -1 : 2)));
            String f = right ? "quizRight" : "quizWrong";
            w.put(f, w.optInt(f) + 1);
        }
        st.put("weak", weak);
    }

    private void flushQueue() {
        String q = prefs.getString("weakQueue", "[]");
        if (q.equals("[]")) return;
        try {
            JSONArray a = new JSONArray(q);
            for (int i = 0; i < a.length(); i++) pc("POST", "/api/weak", a.getJSONObject(i).toString(), 5000);
            prefs.edit().putString("weakQueue", "[]").apply();
        } catch (Exception e) { /* try again next time */ }
    }

    private void saveLocalSettings(JSONObject in) throws Exception {
        SharedPreferences.Editor ed = prefs.edit();
        if (in.has("bridgeKey")) ed.putString("bridgeKey", in.optString("bridgeKey").trim());
        if (!in.optString("claudeKey").isEmpty()) ed.putString("claudeEnc", Secrets.seal(act, in.optString("claudeKey").trim()));
        if (!in.optString("model").isEmpty()) ed.putString("model", in.optString("model"));
        ed.apply();
    }

    // ---- Steam straight from the phone ----

    private JSONObject steamStats() {
        String id = prefs.getString("steamId", "");
        String key = Secrets.open(act, prefs.getString("steamKeyEnc", ""));
        if (id.isEmpty() || key.isEmpty()) return steamCache;
        if (steamCache != null && System.currentTimeMillis() - steamAt < 120000) return steamCache;
        JSONObject st = new JSONObject();
        try {
            st.put("at", System.currentTimeMillis() / 1000);
            JSONObject sum = steamGet(key, "/ISteamUser/GetPlayerSummaries/v2/?steamids=" + id);
            JSONArray players = sum.getJSONObject("response").optJSONArray("players");
            if (players != null && players.length() > 0) {
                JSONObject p = players.getJSONObject(0);
                st.put("persona", p.optString("personaname"));
                st.put("avatar", p.optString("avatarmedium"));
                st.put("public", p.optInt("communityvisibilitystate") == 3);
                int gid = 0;
                try { gid = Integer.parseInt(p.optString("gameid", "0")); } catch (Exception ignored) { }
                st.put("playing", isGame(gid, p.optString("gameextrainfo")));
                if (!p.optString("gameid").isEmpty() && !st.optBoolean("playing")) st.put("playingOther", p.optString("gameextrainfo"));
            }
            JSONObject owned = steamGet(key, "/IPlayerService/GetOwnedGames/v1/?include_appinfo=1&include_played_free_games=1&skip_unvetted_apps=0&steamid=" + id).optJSONObject("response");
            if (owned != null && owned.has("game_count")) {
                st.put("detailsOk", true);
                JSONArray g = owned.optJSONArray("games");
                for (int i = 0; g != null && i < g.length(); i++) if (isGame(g.getJSONObject(i).optInt("appid"), g.getJSONObject(i).optString("name"))) st.put("owned", g.getJSONObject(i));
            }
            if (!st.optBoolean("public") || !st.optBoolean("detailsOk")) {
                st.put("privacy", "Your Steam game details are private, so Steam won't share your playtime. In Steam: your name, Profile, Edit Profile, Privacy Settings. Set My profile and Game details to Public, and untick \"Always keep my total playtime private\".");
            }
            JSONObject last = prefs.getString("lastState", "").isEmpty() ? null : new JSONObject(prefs.getString("lastState", "")).optJSONObject("steam");
            if (last != null) {
                if (last.has("achNote")) st.put("achNote", last.optString("achNote"));
                if (last.has("achievements")) st.put("achievements", last.optJSONArray("achievements"));
            }
        } catch (Exception e) {
            try { st.put("error", "Couldn't reach Steam" + (e instanceof IOException && e.getMessage() != null && e.getMessage().contains("403") ? ": the Web API key was rejected" : "")); } catch (Exception ignored) { }
            if (steamCache != null) return steamCache;
        }
        steamCache = st;
        steamAt = System.currentTimeMillis();
        return st;
    }

    private static JSONObject steamGet(String key, String pathAndQuery) throws Exception {
        HttpURLConnection h = (HttpURLConnection) new URL(STEAM + pathAndQuery + "&format=json&key=" + URLEncoder.encode(key, "UTF-8")).openConnection();
        h.setConnectTimeout(10000);
        h.setReadTimeout(15000);
        try {
            int code = h.getResponseCode();
            if (code >= 400) throw new IOException("HTTP " + code);
            return new JSONObject(read(h.getInputStream()));
        } finally {
            h.disconnect();
        }
    }

    // ---- the PC link ----

    private boolean pcLikelyUp() {
        // After a failure, wait 6 seconds before trying the PC again, so the page stays quick.
        return pcOk || System.currentTimeMillis() - pcFailedAt > 6000;
    }

    private void markDown() {
        pcOk = false;
        pcFailedAt = System.currentTimeMillis();
    }

    private String pc(String method, String path, String body, int readTimeout) throws IOException {
        String host = prefs.getString("pcHost", "");
        int port = prefs.getInt("pcPort", 47830);
        String tok = Secrets.open(act, prefs.getString("tokenEnc", ""));
        String r = https(host, port, prefs.getString("pcFp", ""), method, path, body, tok, readTimeout);
        pcOk = true;
        return r;
    }

    /** HTTPS to the PC, trusting only the certificate whose fingerprint was saved when pairing. */
    private static String https(String host, int port, String fp, String method, String path, String body, String token, int readTimeout) throws IOException {
        HttpsURLConnection h = (HttpsURLConnection) new URL("https://" + host + ":" + port + path).openConnection();
        try {
            h.setSSLSocketFactory(pinned(fp));
            h.setHostnameVerifier((name, session) -> true); // identity is the pinned certificate, not a name
            h.setConnectTimeout(2500);
            h.setReadTimeout(readTimeout);
            h.setRequestMethod(method);
            if (token != null && !token.isEmpty()) h.setRequestProperty("Authorization", "Bearer " + token);
            if (method.equals("POST")) {
                h.setDoOutput(true);
                h.setRequestProperty("Content-Type", "application/json");
                byte[] b = (body == null || body.isEmpty() ? "{}" : body).getBytes("UTF-8");
                h.setFixedLengthStreamingMode(b.length);
                OutputStream o = h.getOutputStream();
                o.write(b);
                o.close();
            }
            int code = h.getResponseCode();
            InputStream in = code >= 400 ? h.getErrorStream() : h.getInputStream();
            String r = in == null ? "" : read(in);
            if (code == 403 && path.startsWith("/api/")) throw new IOException("The PC didn't accept this phone. Pair again.");
            if (code >= 500) throw new IOException("PC error " + code);
            return r.isEmpty() ? "{}" : r;
        } catch (SocketTimeoutException e) {
            throw new IOException("The PC didn't answer in time.");
        } finally {
            h.disconnect();
        }
    }

    private static SSLSocketFactory pinned(final String fp) throws IOException {
        try {
            TrustManager tm = new X509TrustManager() {
                public void checkClientTrusted(X509Certificate[] c, String a) { }
                public void checkServerTrusted(X509Certificate[] chain, String a) throws java.security.cert.CertificateException {
                    if (chain == null || chain.length == 0 || fp == null || fp.isEmpty() || !fp.equalsIgnoreCase(sha256(chain[0].getEncoded()))) {
                        throw new java.security.cert.CertificateException("This isn't the PC this phone was paired with.");
                    }
                }
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            };
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, new TrustManager[]{tm}, null);
            return ctx.getSocketFactory();
        } catch (Exception e) {
            throw new IOException(e);
        }
    }

    // ---- finding and pairing the PC ----

    private JSONObject local(String path, String body) throws Exception {
        JSONObject in = body.isEmpty() ? new JSONObject() : new JSONObject(body);
        switch (path) {
            case "/local/discover": {
                JSONObject o = ok();
                o.put("found", discover(1800));
                o.put("phoneIps", new JSONArray(myAddresses()));
                return o;
            }
            case "/local/pair":
                return pair(in.optString("addr").trim(), in.optString("code").trim(), in.optString("fp"));
            case "/local/unpair":
                prefs.edit().remove("pcHost").remove("pcPort").remove("pcFp").remove("tokenEnc").remove("pcName")
                    .remove("lastState").remove("steamId").remove("steamName").remove("steamKeyEnc").remove("weakQueue").apply();
                pcOk = false;
                steamCache = null;
                return ok();
            default:
                return err("unknown");
        }
    }

    /** Finds FC26 Dashboard on the home network: a broadcast first, then a direct search. */
    private JSONArray discover(int waitMs) {
        JSONArray out = broadcastDiscover(waitMs);
        if (out.length() == 0) out = sweep();
        return out;
    }

    /** This phone's own home network addresses (to show when nothing is found). */
    private static java.util.List<String> myAddresses() {
        java.util.List<String> out = new java.util.ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof java.net.Inet4Address && a.isSiteLocalAddress()) out.add(a.getHostAddress());
                }
            }
        } catch (Exception ignored) { }
        return out;
    }

    /**
     * Some routers and mesh WiFi drop broadcasts. This knocks on port 47830 on every address in the phone's own network, then asks anything that answers whether
     * it's FC26 Dashboard (/hello). Home networks only; it takes a few seconds.
     */
    private JSONArray sweep() {
        final JSONArray out = new JSONArray();
        final Set<String> seen = Collections.synchronizedSet(new HashSet<>());
        java.util.List<String> hosts = new java.util.ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (!(a instanceof java.net.Inet4Address) || !a.isSiteLocalAddress()) continue;
                    int prefix = Math.max(23, Math.min(30, ia.getNetworkPrefixLength()));
                    byte[] b = a.getAddress();
                    int ip = ((b[0] & 255) << 24) | ((b[1] & 255) << 16) | ((b[2] & 255) << 8) | (b[3] & 255);
                    int mask = prefix == 0 ? 0 : -1 << (32 - prefix);
                    int net = ip & mask, size = ~mask;
                    for (int h = 1; h < size; h++) {
                        int x = net | h;
                        if (x == ip) continue;
                        hosts.add(((x >>> 24) & 255) + "." + ((x >>> 16) & 255) + "." + ((x >>> 8) & 255) + "." + (x & 255));
                    }
                }
            }
        } catch (Exception ignored) { }
        ExecutorService ex = Executors.newFixedThreadPool(64);
        java.util.List<java.util.concurrent.Future<?>> jobs = new java.util.ArrayList<>();
        for (final String h : hosts) {
            for (final int port : new int[]{47830}) {
                jobs.add(ex.submit(() -> {
                    try (java.net.Socket s = new java.net.Socket()) {
                        s.connect(new java.net.InetSocketAddress(h, port), 350);
                    } catch (Exception e) {
                        return;
                    }
                    try {
                        String fp = fetchFingerprint(h, port);
                        JSONObject r = new JSONObject(https(h, port, fp, "GET", "/hello", "", "", 3000));
                        if ("fc26dash".equals(r.optString("app")) && fp.equalsIgnoreCase(r.optString("fp")) && seen.add(h)) {
                            r.put("host", h);
                            r.put("port", port);
                            r.put("via", "search");
                            synchronized (out) { out.put(r); }
                        }
                    } catch (Exception ignored) { }
                }));
            }
        }
        for (java.util.concurrent.Future<?> f : jobs) {
            try { f.get(15, java.util.concurrent.TimeUnit.SECONDS); } catch (Exception ignored) { }
        }
        ex.shutdownNow();
        return out;
    }

    /** Asks the home network "where is FC26 Dashboard?" by broadcast and collects the answers. */
    private JSONArray broadcastDiscover(int waitMs) {
        JSONArray out = new JSONArray();
        Set<String> seen = new HashSet<>();
        try (DatagramSocket s = new DatagramSocket()) {
            s.setBroadcast(true);
            s.setSoTimeout(300);
            byte[] ask = DISCOVERY_ASK.getBytes("UTF-8");
            Set<InetAddress> targets = new HashSet<>();
            targets.add(InetAddress.getByName("255.255.255.255"));
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) if (ia.getBroadcast() != null) targets.add(ia.getBroadcast());
            }
            for (InetAddress t : targets) {
                try { s.send(new DatagramPacket(ask, ask.length, t, DISCOVERY_PORT)); } catch (Exception ignored) { }
            }
            long end = System.currentTimeMillis() + waitMs;
            byte[] buf = new byte[1024];
            while (System.currentTimeMillis() < end) {
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try { s.receive(p); } catch (SocketTimeoutException e) { continue; }
                InetAddress from = p.getAddress();
                if (!(from.isSiteLocalAddress() || from.isLinkLocalAddress())) continue;
                try {
                    JSONObject r = new JSONObject(new String(p.getData(), 0, p.getLength(), "UTF-8"));
                    if (!"fc26dash".equals(r.optString("app"))) continue;
                    String host = from.getHostAddress();
                    if (!seen.add(host)) continue;
                    r.put("host", host);
                    out.put(r);
                } catch (Exception ignored) { }
            }
        } catch (Exception ignored) { }
        return out;
    }

    /** The PC's address changed (DHCP): find the same PC (same certificate) again. */
    private volatile long lastRediscover = 0;

    private synchronized void rediscover() {
        String fp = prefs.getString("pcFp", "");
        if (fp.isEmpty() || System.currentTimeMillis() - lastRediscover < 30000) return;
        lastRediscover = System.currentTimeMillis();
        JSONArray found = discover(1500);
        for (int i = 0; i < found.length(); i++) {
            JSONObject r = found.optJSONObject(i);
            if (r != null && fp.equalsIgnoreCase(r.optString("fp"))) {
                prefs.edit().putString("pcHost", r.optString("host")).putInt("pcPort", r.optInt("port", 47830)).apply();
                pcFailedAt = 0;
                return;
            }
        }
    }

    private JSONObject pair(String addr, String code, String fpHint) throws Exception {
        if (!code.matches("\\d{6}")) return err("The code is 6 digits, shown on the PC.");
        String host = addr;
        int port = 47830;
        int colon = addr.lastIndexOf(':');
        if (colon > 0) {
            host = addr.substring(0, colon);
            try { port = Integer.parseInt(addr.substring(colon + 1)); } catch (Exception e) { return err("That address doesn't look right."); }
        }
        if (host.isEmpty()) {
            JSONArray found = discover(1800);
            if (found.length() == 0) return err("No PC found. Type the address shown on the PC.");
            JSONObject r = found.getJSONObject(0);
            host = r.optString("host");
            port = r.optInt("port", 47830);
            fpHint = r.optString("fp");
        }
        InetAddress ia = InetAddress.getByName(host);
        if (!(ia.isSiteLocalAddress() || ia.isLinkLocalAddress() || ia.isLoopbackAddress())) return err("Only PCs on your home network can be paired.");
        if (fpHint == null || fpHint.isEmpty()) {
            // Typed by hand: ask the PC who it is (discovery reply) to learn its fingerprint.
            JSONArray found = discover(1500);
            for (int i = 0; i < found.length(); i++) if (found.getJSONObject(i).optString("host").equals(ia.getHostAddress())) fpHint = found.getJSONObject(i).optString("fp");
            if (fpHint == null || fpHint.isEmpty()) fpHint = fetchFingerprint(ia.getHostAddress(), port);
        }
        JSONObject req = new JSONObject();
        req.put("code", code);
        req.put("device", Build.MANUFACTURER + " " + Build.MODEL);
        String r;
        try {
            r = https(ia.getHostAddress(), port, fpHint, "POST", "/pair", req.toString(), "", 10000);
        } catch (IOException e) {
            return err("Couldn't reach the PC: " + e.getMessage());
        }
        JSONObject out = new JSONObject(r);
        if (!out.optBoolean("ok")) return out;
        SharedPreferences.Editor ed = prefs.edit();
        ed.putString("pcHost", ia.getHostAddress()).putInt("pcPort", port).putString("pcFp", fpHint)
          .putString("pcName", out.optString("pc")).putString("tokenEnc", Secrets.seal(act, out.optString("token")))
          .putString("steamId", out.optString("steamId")).putString("steamName", out.optString("steamName"));
        if (!out.optString("steamKey").isEmpty()) ed.putString("steamKeyEnc", Secrets.seal(act, out.optString("steamKey")));
        ed.apply();
        pcOk = true;
        pcFailedAt = 0;
        steamCache = null;
        JSONObject res = ok();
        res.put("pc", out.optString("pc"));
        return res;
    }

    /** Reads the PC's certificate fingerprint over TLS (trust on first use, home network only). */
    private static String fetchFingerprint(String host, int port) throws IOException {
        final String[] got = {""};
        try {
            TrustManager tm = new X509TrustManager() {
                public void checkClientTrusted(X509Certificate[] c, String a) { }
                public void checkServerTrusted(X509Certificate[] chain, String a) throws java.security.cert.CertificateException { got[0] = sha256(chain[0].getEncoded()); }
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            };
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, new TrustManager[]{tm}, null);
            try (javax.net.ssl.SSLSocket s = (javax.net.ssl.SSLSocket) ctx.getSocketFactory().createSocket(host, port)) {
                s.setSoTimeout(4000);
                s.startHandshake();
            }
        } catch (Exception e) {
            throw new IOException("Couldn't reach the PC at " + host + ":" + port);
        }
        return got[0];
    }

    // ---- small helpers ----

    static String sha256(byte[] b) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(b);
            StringBuilder sb = new StringBuilder();
            for (byte x : d) sb.append(String.format("%02x", x));
            return sb.toString();
        } catch (Exception e) {
            return "";
        }
    }

    private static String read(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] b = new byte[65536];
        int n;
        while ((n = in.read(b)) > 0) buf.write(b, 0, n);
        in.close();
        return buf.toString("UTF-8");
    }

    private static JSONObject ok() {
        JSONObject o = new JSONObject();
        try { o.put("ok", true); } catch (Exception e) { }
        return o;
    }

    private static JSONObject err(String m) {
        JSONObject o = new JSONObject();
        try { o.put("ok", false); o.put("error", m); } catch (Exception e) { }
        return o;
    }
}
