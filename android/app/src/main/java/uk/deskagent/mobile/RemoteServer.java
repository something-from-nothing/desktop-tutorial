package uk.deskagent.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Collections;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Remote control: lets Desk Agent on another device on the same Wi-Fi (the Windows app,
 * or this app on another phone or Chromebook) use this phone's Desk Agent. Off until
 * switched on in Settings. A device pairs once with the 6-digit code shown on this
 * phone and gets its own key; nothing works without that key. Requests are answered
 * by the app page itself (remoteRequest in index.html), so they see what you see.
 */
final class RemoteServer {
    static final int PORT = 8765;
    private static final String TAG = "DeskAgentRemote";
    private static final SecureRandom random = new SecureRandom();
    private static RemoteServer instance;

    private final Context app;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Map<String, String[]> replies = new ConcurrentHashMap<>();
    private final Map<String, CountDownLatch> waiting = new ConcurrentHashMap<>();
    private ServerSocket socket;
    private volatile WebView page;
    private String code;
    // When Desk Agent's window is closed, a hidden copy of its page answers instead.
    private WebView hidden;
    private Bridge hiddenBridge;
    private volatile CountDownLatch hiddenLoaded;

    private RemoteServer(Context app) {
        this.app = app.getApplicationContext();
    }

    static synchronized RemoteServer get(Context context) {
        if (instance == null) instance = new RemoteServer(context);
        return instance;
    }

    /** The app page that answers requests (the main window's). Called on the main thread. */
    void attach(WebView web) {
        page = web;
        if (web != null && hidden != null) {  // the real window is back: only one copy of the page at a time
            hiddenBridge.detach();
            hidden.destroy();
            hidden = null;
            hiddenLoaded = null;
        }
    }

    /** Starts the hidden copy of the page (if needed) and waits for it to load. */
    private WebView hiddenPage() throws InterruptedException {
        CountDownLatch loaded = new CountDownLatch(1);
        CountDownLatch made = new CountDownLatch(1);
        ui.post(() -> {
            if (page == null && hidden == null) {
                hiddenLoaded = loaded;
                hidden = new WebView(app);
                hidden.getSettings().setJavaScriptEnabled(true);
                hidden.getSettings().setDomStorageEnabled(true);
                hidden.getSettings().setAllowFileAccess(true);
                hiddenBridge = new Bridge(app, hidden, true, null);
                hidden.addJavascriptInterface(hiddenBridge, "Android");
                hidden.setWebViewClient(new android.webkit.WebViewClient() {
                    @Override
                    public void onPageFinished(WebView view, String url) {
                        loaded.countDown();
                    }
                });
                hidden.loadUrl("file:///android_asset/index.html?remote=1");
            }
            made.countDown();
        });
        made.await(5, TimeUnit.SECONDS);
        CountDownLatch l = hiddenLoaded;
        if (l != null) l.await(20, TimeUnit.SECONDS);
        return page != null ? page : hidden;
    }

    /** A key for a device, as pairing gives. Desk Agent Dashboard on this phone gets one without a code. */
    String issueKey() {
        byte[] k = new byte[24];
        random.nextBytes(k);
        StringBuilder key = new StringBuilder();
        for (byte b : k) key.append(String.format("%02x", b));
        Set<String> keys = new HashSet<>(prefs().getStringSet("keys", new HashSet<>()));
        keys.add(key.toString());
        prefs().edit().putStringSet("keys", keys).apply();
        return key.toString();
    }

    boolean knows(String key) {
        return paired(key == null ? "" : key);
    }

    private SharedPreferences prefs() {
        return app.getSharedPreferences("remote", Context.MODE_PRIVATE);
    }

    synchronized boolean running() {
        return socket != null && !socket.isClosed();
    }

    /** Switches it on and returns what to show: the addresses, port and pairing code. */
    synchronized JSONObject start() throws Exception {
        if (!running()) {
            socket = new ServerSocket(PORT);
            Thread t = new Thread(this::serve, "remote-control");
            t.setDaemon(true);
            t.start();
        }
        if (code == null) code = String.format("%06d", random.nextInt(1_000_000));
        prefs().edit().putBoolean("on", true).apply();
        return status();
    }

    synchronized void stop() {
        try {
            if (socket != null) socket.close();
        } catch (Exception ignored) {
        }
        socket = null;
        code = null;
        prefs().edit().putBoolean("on", false).apply();
    }

    /** Removes every paired device, so each has to pair again. */
    void forgetDevices() {
        prefs().edit().remove("keys").apply();
    }

    synchronized JSONObject status() throws Exception {
        JSONObject s = new JSONObject();
        s.put("on", running());
        s.put("port", PORT);
        s.put("code", running() ? code : JSONObject.NULL);
        JSONArray addresses = new JSONArray();
        for (String a : addresses()) addresses.put(a + ":" + PORT);
        s.put("addresses", addresses);
        s.put("devices", prefs().getStringSet("keys", new HashSet<>()).size());
        return s;
    }

    private static Set<String> addresses() {
        Set<String> out = new HashSet<>();
        try {
            for (NetworkInterface n : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!n.isUp() || n.isLoopback()) continue;
                for (InetAddress a : Collections.list(n.getInetAddresses())) {
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) out.add(a.getHostAddress());
                }
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    /** The page's answer to a request it was handed. */
    void reply(String id, int status, String body) {
        replies.put(id, new String[]{String.valueOf(status), body});
        CountDownLatch l = waiting.get(id);
        if (l != null) l.countDown();
    }

    private void serve() {
        while (running()) {
            try {
                Socket s = socket.accept();
                new Thread(() -> handle(s), "remote-request").start();
            } catch (Exception e) {
                if (running()) Log.w(TAG, "accept", e);
            }
        }
    }

    private static String readLine(InputStream in) throws Exception {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int ch;
        while ((ch = in.read()) >= 0 && ch != '\n') {
            if (ch != '\r') b.write(ch);
            if (b.size() > 16_384) throw new Exception("header too long");
        }
        return ch < 0 && b.size() == 0 ? null : b.toString("UTF-8");
    }

    private void handle(Socket s) {
        try (Socket c = s) {
            c.setSoTimeout(15_000);
            InputStream in = c.getInputStream();
            String line = readLine(in);
            if (line == null) return;
            String[] first = line.split(" ");
            if (first.length < 2) return;
            String method = first[0], path = first[1];
            int length = 0;
            String auth = "";
            while ((line = readLine(in)) != null && !line.isEmpty()) {
                int colon = line.indexOf(':');
                if (colon < 0) continue;
                String name = line.substring(0, colon).trim().toLowerCase(), value = line.substring(colon + 1).trim();
                if (name.equals("content-length")) length = Math.max(0, Math.min(Integer.parseInt(value), 2_000_000));
                if (name.equals("authorization")) auth = value.replaceFirst("(?i)^bearer\\s+", "");
            }
            byte[] data = new byte[length];
            int got = 0;
            while (got < length) {
                int n = in.read(data, got, length - got);
                if (n < 0) break;
                got += n;
            }
            String body = new String(data, 0, got, StandardCharsets.UTF_8);
            OutputStream out = c.getOutputStream();
            if (method.equals("OPTIONS")) { send(out, 204, ""); return; }
            if (path.equals("/api/pair") && method.equals("POST")) { pair(out, body); return; }
            if (!paired(auth)) { send(out, 401, "{\"error\":\"Pair this device first, with the code shown on the phone.\"}"); return; }
            if (!path.startsWith("/api/")) { send(out, 404, "{\"error\":\"not found\"}"); return; }
            String[] answer = ask(method, path, body);
            send(out, Integer.parseInt(answer[0]), answer[1]);
        } catch (Exception e) {
            Log.w(TAG, "request", e);
        }
    }

    /** Hands the request to the app page and waits for its answer. */
    private String[] ask(String method, String path, String body) throws Exception {
        WebView web = page;
        if (web == null) web = hiddenPage();
        if (web == null) return new String[]{"503", "{\"error\":\"Open Desk Agent on the phone.\"}"};
        final WebView target = web;
        String id = Long.toHexString(random.nextLong());
        CountDownLatch done = new CountDownLatch(1);
        waiting.put(id, done);
        String js = "window.remoteRequest ? remoteRequest(" + JSONObject.quote(id) + "," + JSONObject.quote(method) + ","
                + JSONObject.quote(path) + "," + JSONObject.quote(body) + ") : Android.remoteReply(" + JSONObject.quote(id) + ", 503, '{}')";
        ui.post(() -> target.evaluateJavascript(js, null));
        boolean answered = done.await(30, TimeUnit.SECONDS);
        waiting.remove(id);
        String[] r = replies.remove(id);
        if (!answered || r == null) return new String[]{"504", "{\"error\":\"The phone didn't answer in time.\"}"};
        return r;
    }

    private void pair(OutputStream out, String body) throws Exception {
        String given = "";
        try { given = new JSONObject(body).optString("code", ""); } catch (Exception ignored) { }
        String expected;
        synchronized (this) { expected = code; }
        if (expected == null || !expected.equals(given.trim())) {
            Thread.sleep(800);  // slows down guessing
            send(out, 403, "{\"error\":\"That code isn't right. Check the code on the phone in Settings, Remote control.\"}");
            return;
        }
        String key = issueKey();
        synchronized (this) { code = String.format("%06d", random.nextInt(1_000_000)); }  // each code works once
        JSONObject r = new JSONObject();
        r.put("key", key);
        r.put("name", Build.MANUFACTURER + " " + Build.MODEL);
        send(out, 200, r.toString());
        ui.post(() -> { WebView web = page; if (web != null) web.evaluateJavascript("window.onRemotePaired && onRemotePaired()", null); });
    }

    private boolean paired(String key) {
        return !key.isEmpty() && prefs().getStringSet("keys", new HashSet<>()).contains(key);
    }

    private static void send(OutputStream out, int status, String body) throws Exception {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        String reason = status == 200 ? "OK" : status == 204 ? "No Content" : status == 401 ? "Unauthorized" : status == 403 ? "Forbidden" : "Error";
        String head = "HTTP/1.1 " + status + " " + reason + "\r\n"
                + "Content-Type: application/json; charset=utf-8\r\n"
                + "Content-Length: " + b.length + "\r\n"
                // Other devices' Desk Agent pages call this from their own address.
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
                + "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n"
                + "Access-Control-Allow-Private-Network: true\r\n"
                + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.UTF_8));
        out.write(b);
        out.flush();
    }
}
