package uk.deskagent.mobile;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.ServerSocket;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;

/**
 * The parts shared by the app window and the background runner: the AI engine,
 * model downloads, the data folder and fetching web pages. One per app process.
 */
final class Core {
    static final String CHANNEL_RUNS = "runs";
    static final String CHANNEL_RESULTS = "results";

    private static Core instance;
    private final Context app;
    private Process engine;
    private String engineModel;
    private int enginePort;
    /** Every open page, so download progress reaches whichever is showing. */
    final List<Bridge> bridges = new ArrayList<>();

    static synchronized Core get(Context context) {
        if (instance == null) instance = new Core(context.getApplicationContext());
        return instance;
    }

    private Core(Context app) {
        this.app = app;
        NotificationManager nm = app.getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_RUNS, "Working in the background", NotificationManager.IMPORTANCE_LOW));
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_RESULTS, "Scheduled chat results", NotificationManager.IMPORTANCE_DEFAULT));
    }

    File dataDir() {
        File d = new File(app.getFilesDir(), "data");
        d.mkdirs();
        return d;
    }

    File modelsDir() {
        File d = new File(app.getFilesDir(), "models");
        d.mkdirs();
        return d;
    }

    /** Only plain names inside our folders: no "..", no absolute paths. */
    static File safe(File root, String rel) throws Exception {
        File f = new File(root, rel).getCanonicalFile();
        if (!f.getPath().startsWith(root.getCanonicalPath() + File.separator)) throw new Exception("bad path");
        return f;
    }

    String readData(String path) {
        try {
            File f = safe(dataDir(), path);
            return f.exists() ? new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8) : null;
        } catch (Exception e) {
            return null;
        }
    }

    // ------------------------------------------------ the AI engine

    synchronized String engineModel() {
        return engineModel;
    }

    synchronized int startEngine(String model, int context) throws Exception {
        if (model.equals(engineModel) && engine != null && engine.isAlive() && healthy(enginePort)) return enginePort;
        stopEngine();
        File modelFile = modelFile(model);
        if (modelFile == null) throw new Exception("The " + model + " model isn't downloaded.");
        File exe = new File(app.getApplicationInfo().nativeLibraryDir, "libllamaserver.so");
        if (!exe.exists()) throw new Exception("The AI engine is missing from this build.");
        int port;
        try (ServerSocket s = new ServerSocket(0)) { port = s.getLocalPort(); }
        int threads = Math.max(2, Math.min(6, Runtime.getRuntime().availableProcessors() - 2));
        List<String> args = new ArrayList<>(java.util.Arrays.asList(exe.getPath(), "-m", modelFile.getPath(), "--host", "127.0.0.1",
                "--port", String.valueOf(port), "-c", String.valueOf(context), "--jinja", "--reasoning-format", "none",
                "-t", String.valueOf(threads)));
        // A model that sees photos has its picture reader in the same folder.
        File[] reader = modelFile.getParentFile().listFiles((d, n) -> n.startsWith("mmproj") && n.endsWith(".gguf"));
        if (reader != null && reader.length > 0) { args.add("--mmproj"); args.add(reader[0].getPath()); }
        ProcessBuilder pb = new ProcessBuilder(args);
        pb.redirectErrorStream(true);
        File log = new File(app.getFilesDir(), "engine.log");
        pb.redirectOutput(log);
        engine = pb.start();
        long deadline = System.currentTimeMillis() + 240_000;
        while (System.currentTimeMillis() < deadline) {
            if (!engine.isAlive()) throw new Exception("The model failed to load: " + tail(log));
            if (healthy(port)) {
                engineModel = model;
                enginePort = port;
                return port;
            }
            Thread.sleep(400);
        }
        stopEngine();
        throw new Exception("The model took too long to load.");
    }

    /**
     * A model that came inside the app. The complete APK carries one as
     * lib/arm64-v8a/libmodel_<key>.so, which Android unpacks to the native library
     * folder when the app is installed, so it works with no download.
     */
    File bundledModel(String key) {
        File f = new File(app.getApplicationInfo().nativeLibraryDir, "libmodel_" + key + ".so");
        return f.exists() && f.length() > 1_000_000 ? f : null;
    }

    /** Where a model's file is: downloaded ones first (older app versions kept them in
     *  modelsDir, newer ones where Android's download manager puts them), then one that
     *  came with the app. */
    File modelFile(String key) {
        for (File root : new File[]{modelsDir(), ModelDownloads.dir(app)}) {
            if (root == null) continue;
            try {
                File[] files = safe(root, key).listFiles((d, n) -> n.endsWith(".gguf") && !n.startsWith("mmproj"));
                if (files != null && files.length > 0) return files[0];
            } catch (Exception ignored) {
            }
        }
        return bundledModel(key);
    }

    synchronized void stopEngine() {
        if (engine != null) engine.destroy();
        engine = null;
        engineModel = null;
    }

    private static boolean healthy(int port) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/health").openConnection();
            c.setConnectTimeout(1000);
            c.setReadTimeout(1500);
            return c.getResponseCode() == 200;
        } catch (Exception e) {
            return false;
        }
    }

    /** The last part of the engine's own log, for the Dashboard when something's wrong. */
    String engineLog() {
        try {
            String s = new String(Files.readAllBytes(new File(app.getFilesDir(), "engine.log").toPath()), StandardCharsets.UTF_8);
            return s.length() > 6000 ? s.substring(s.length() - 6000) : s;
        } catch (Exception e) {
            return "";
        }
    }

    /** Whether the engine is running now (not just whether a model was loaded once). */
    synchronized boolean engineRunning() {
        return engine != null && engine.isAlive();
    }

    private static String tail(File f) {
        try {
            String s = new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8);
            return s.length() > 400 ? s.substring(s.length() - 400) : s;
        } catch (Exception e) {
            return "";
        }
    }

    // ------------------------------------------------ downloading models

    void cancelDownload(String key) {
        ModelDownloads.cancel(app, key);
        ModelDownloads.cancel(app, key + "#mmproj");
    }

    /** Finds the model's file on Hugging Face and hands it to Android's download manager. */
    void download(String key, String repo, String quant, String mmproj) throws Exception {
        JSONObject info = new JSONObject(new String(get("https://huggingface.co/api/models/" + repo, 5_000_000), StandardCharsets.UTF_8));
        JSONArray siblings = info.optJSONArray("siblings");
        String name = null, reader = null;
        for (int i = 0; siblings != null && i < siblings.length(); i++) {
            String f = siblings.getJSONObject(i).getString("rfilename");
            String lower = f.toLowerCase();
            if (!lower.endsWith(".gguf") || f.contains("-of-")) continue;
            if (lower.startsWith("mmproj")) {
                // The photo reader: the smallest one (f16 rather than f32).
                if (!mmproj.isEmpty() && (reader == null || f.length() < reader.length() || lower.contains("f16"))) reader = f;
            } else if (lower.contains(quant.toLowerCase()) && (name == null || f.length() < name.length())) name = f;
        }
        if (name == null) throw new Exception("No " + quant + " file found in " + repo);
        if (!mmproj.isEmpty() && reader == null) throw new Exception("No photo reader (mmproj) found in " + repo);

        String base = "https://huggingface.co/" + repo + "/resolve/main/";
        if (reader != null) ModelDownloads.start(app, key + "#mmproj", reader, base + URLEncoder.encode(reader, "UTF-8").replace("+", "%20"));
        ModelDownloads.start(app, key, name, base + URLEncoder.encode(name, "UTF-8").replace("+", "%20"));
        if (reader != null) new Thread(() -> {
            try { watch(key + "#mmproj"); } catch (InterruptedException ignored) { }
        }).start();
        watch(key);
    }

    /** Passes a download's progress to the page until it ends. The download itself
     *  doesn't depend on this: it carries on if the app is closed. */
    void watch(String key) throws InterruptedException {
        while (true) {
            long[] s = ModelDownloads.check(app, key);
            if (s[2] == 1) { progress(key, s[0], s[1], "done", null); return; }
            if (s[2] == 2) { progress(key, s[0], s[1], "error", ModelDownloads.lastError); return; }
            if (s[2] == 3) { progress(key, s[0], s[1], "cancelled", null); return; }
            progress(key, s[0], Math.max(s[1], 0), null, null);
            Thread.sleep(700);
        }
    }

    /** When the app opens: finish downloads that ended while it was closed, and show the
     *  progress of ones still going. */
    void resumeDownloads() {
        for (String key : ModelDownloads.pending(app)) {
            new Thread(() -> {
                try { watch(key); } catch (InterruptedException ignored) { }
            }).start();
        }
    }

    void progress(String key, long done, long total, String status, String error) {
        try {
            JSONObject ev = new JSONObject();
            ev.put("type", "download");
            ev.put("key", key);
            ev.put("done", done);
            ev.put("total", total);
            if (status != null) ev.put("status", status);
            if (error != null) ev.put("error", error);
            synchronized (bridges) {
                for (Bridge b : bridges) b.event(ev);
            }
        } catch (Exception ignored) {
        }
    }

    static byte[] get(String url, int limit) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setInstanceFollowRedirects(true);
        c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android) DeskAgentMobile");
        c.setConnectTimeout(20_000);
        c.setReadTimeout(30_000);
        if (c.getResponseCode() >= 400) throw new Exception("HTTP " + c.getResponseCode() + " from " + url);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1 << 15];
            int n;
            while ((n = in.read(buf)) > 0 && out.size() < limit) out.write(buf, 0, n);
            return out.toByteArray();
        }
    }

    /**
     * Sends a POST, for MCP servers. Returns {status, type, session, body}: the page
     * needs the content type (JSON or a stream of events) and the MCP session id.
     */
    static JSONObject post(String url, JSONObject headers, String body) throws Exception {
        if (!url.startsWith("https://") && !url.startsWith("http://127.0.0.1") && !url.startsWith("http://localhost")) {
            throw new Exception("Connectors must use https.");
        }
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(20_000);
        c.setReadTimeout(120_000);
        c.setRequestProperty("User-Agent", "DeskAgentMobile");
        if (headers != null) {
            for (java.util.Iterator<String> it = headers.keys(); it.hasNext(); ) {
                String k = it.next();
                c.setRequestProperty(k, headers.getString(k));
            }
        }
        try (OutputStream out = c.getOutputStream()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }
        int status = c.getResponseCode();
        InputStream in = status >= 400 ? c.getErrorStream() : c.getInputStream();
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        if (in != null) {
            try (InputStream s = in) {
                byte[] b = new byte[1 << 15];
                int n;
                while ((n = s.read(b)) > 0 && buf.size() < 3_000_000) buf.write(b, 0, n);
            }
        }
        JSONObject out = new JSONObject();
        out.put("status", status);
        out.put("type", c.getContentType() == null ? "" : c.getContentType());
        out.put("session", c.getHeaderField("Mcp-Session-Id") == null ? "" : c.getHeaderField("Mcp-Session-Id"));
        out.put("body", buf.toString("UTF-8"));
        return out;
    }

    /** Saves a file the user asked for into the phone's Downloads folder. */
    String saveToDownloads(String name, String content, String mime) throws Exception {
        return saveBytes(name, content.getBytes(StandardCharsets.UTF_8), mime);
    }

    /** Saves a picture or video (any bytes) into Downloads. */
    String saveBytes(String name, byte[] bytes, String mime) throws Exception {
        String clean = name.replaceAll("[^\\w .()-]", "_");
        ContentValues v = new ContentValues();
        v.put(MediaStore.Downloads.DISPLAY_NAME, clean);
        v.put(MediaStore.Downloads.MIME_TYPE, mime);
        v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
        Uri uri = app.getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        if (uri == null) throw new Exception("Couldn't save to Downloads.");
        try (OutputStream out = app.getContentResolver().openOutputStream(uri)) {
            out.write(bytes);
        }
        return "Downloads/" + clean;
    }
}
