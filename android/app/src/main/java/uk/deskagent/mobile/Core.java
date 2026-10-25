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
    private final List<String> cancelled = new ArrayList<>();
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
        ProcessBuilder pb = new ProcessBuilder(exe.getPath(), "-m", modelFile.getPath(), "--host", "127.0.0.1",
                "--port", String.valueOf(port), "-c", String.valueOf(context), "--jinja", "--reasoning-format", "none",
                "-t", String.valueOf(threads));
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

    /** Where a model's file is: downloaded ones first, then one that came with the app. */
    File modelFile(String key) {
        try {
            File[] files = safe(modelsDir(), key).listFiles((d, n) -> n.endsWith(".gguf"));
            if (files != null && files.length > 0) return files[0];
        } catch (Exception ignored) {
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
        synchronized (cancelled) {
            cancelled.add(key);
        }
    }

    void download(String key, String repo, String quant) throws Exception {
        JSONObject info = new JSONObject(new String(get("https://huggingface.co/api/models/" + repo, 5_000_000), StandardCharsets.UTF_8));
        JSONArray siblings = info.optJSONArray("siblings");
        String name = null;
        for (int i = 0; siblings != null && i < siblings.length(); i++) {
            String f = siblings.getJSONObject(i).getString("rfilename");
            String lower = f.toLowerCase();
            if (lower.endsWith(".gguf") && lower.contains(quant.toLowerCase()) && !f.contains("-of-")
                    && (name == null || f.length() < name.length())) name = f;
        }
        if (name == null) throw new Exception("No " + quant + " file found in " + repo);

        File folder = safe(modelsDir(), key);
        folder.mkdirs();
        File part = new File(folder, name + ".part");
        long done = part.exists() ? part.length() : 0;
        HttpURLConnection c = (HttpURLConnection) new URL("https://huggingface.co/" + repo + "/resolve/main/"
                + URLEncoder.encode(name, "UTF-8").replace("+", "%20")).openConnection();
        c.setInstanceFollowRedirects(true);
        c.setRequestProperty("User-Agent", "DeskAgentMobile");
        c.setRequestProperty("Range", "bytes=" + done + "-");
        c.setConnectTimeout(30_000);
        c.setReadTimeout(60_000);
        if (c.getResponseCode() != 206) done = 0;  // the server ignored the resume request, start again
        long total = done + c.getContentLengthLong();
        long lastReport = 0;
        try (InputStream in = c.getInputStream(); OutputStream out = new FileOutputStream(part, done > 0)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) {
                synchronized (cancelled) {
                    if (cancelled.remove(key)) {
                        progress(key, done, total, "cancelled", null);
                        return;
                    }
                }
                out.write(buf, 0, n);
                done += n;
                long now = System.currentTimeMillis();
                if (now - lastReport > 500) {
                    lastReport = now;
                    progress(key, done, total, null, null);
                }
            }
        }
        if (!part.renameTo(new File(folder, name))) throw new Exception("Couldn't save the model file.");
        progress(key, done, total, "done", null);
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

    /** Saves a file the user asked for into the phone's Downloads folder. */
    String saveToDownloads(String name, String content, String mime) throws Exception {
        String clean = name.replaceAll("[^\\w .()-]", "_");
        ContentValues v = new ContentValues();
        v.put(MediaStore.Downloads.DISPLAY_NAME, clean);
        v.put(MediaStore.Downloads.MIME_TYPE, mime);
        v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
        Uri uri = app.getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        if (uri == null) throw new Exception("Couldn't save to Downloads.");
        try (OutputStream out = app.getContentResolver().openOutputStream(uri)) {
            out.write(content.getBytes(StandardCharsets.UTF_8));
        }
        return "Downloads/" + clean;
    }
}
