package uk.deskagent.mobile;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

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
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Desk Agent Mobile. The interface is a web page (assets/index.html) shown in a
 * WebView. This class is the bridge it calls for the things a page can't do:
 * running the AI engine, downloading models, saving data, and fetching web pages.
 * Nothing is sent anywhere except model downloads from Hugging Face and the
 * web searches the AI makes.
 */
public class MainActivity extends Activity {
    private static final String TAG = "DeskAgent";
    private WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private ValueCallback<Uri[]> filePicked;

    private Process engine;
    private String engineModel;
    private int enginePort;
    private final List<String> cancelled = new ArrayList<>();

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        web.addJavascriptInterface(new Bridge(), "Android");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                // Links the AI gives open in the phone's browser, not inside the app.
                startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl()));
                return true;
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePicked != null) filePicked.onReceiveValue(null);
                filePicked = callback;
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("*/*");
                pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(Intent.createChooser(pick, "Attach files"), 1);
                return true;
            }
        });
        web.loadUrl("file:///android_asset/index.html");
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request != 1 || filePicked == null) return;
        Uri[] uris = null;
        if (result == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                uris = new Uri[data.getClipData().getItemCount()];
                for (int i = 0; i < uris.length; i++) uris[i] = data.getClipData().getItemAt(i).getUri();
            } else if (data.getData() != null) {
                uris = new Uri[]{data.getData()};
            }
        }
        filePicked.onReceiveValue(uris);
        filePicked = null;
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.onBack && window.onBack()", value -> {
            if (!"true".equals(value)) MainActivity.super.onBackPressed();
        });
    }

    @Override
    protected void onDestroy() {
        stopEngine();
        super.onDestroy();
    }

    // ------------------------------------------------ talking back to the page

    private void resolve(String id, Object value, String error) {
        try {
            JSONObject msg = new JSONObject();
            msg.put("id", id);
            msg.put("value", value == null ? JSONObject.NULL : value);
            if (error != null) msg.put("error", error);
            String js = "window.__bridgeReply(" + JSONObject.quote(msg.toString()) + ")";
            ui.post(() -> web.evaluateJavascript(js, null));
        } catch (Exception e) {
            Log.e(TAG, "resolve", e);
        }
    }

    private void event(JSONObject ev) {
        String js = "window.__bridgeEvent(" + JSONObject.quote(ev.toString()) + ")";
        ui.post(() -> web.evaluateJavascript(js, null));
    }

    private File dataDir() {
        File d = new File(getFilesDir(), "data");
        d.mkdirs();
        return d;
    }

    private File modelsDir() {
        File d = new File(getFilesDir(), "models");
        d.mkdirs();
        return d;
    }

    /** Only plain names inside our folders: no "..", no absolute paths. */
    private File safe(File root, String rel) throws Exception {
        File f = new File(root, rel).getCanonicalFile();
        if (!f.getPath().startsWith(root.getCanonicalPath() + File.separator)) throw new Exception("bad path");
        return f;
    }

    // ------------------------------------------------ the AI engine

    private synchronized int startEngine(String model, int context) throws Exception {
        if (model.equals(engineModel) && engine != null && engine.isAlive() && healthy(enginePort)) return enginePort;
        stopEngine();
        File folder = safe(modelsDir(), model);
        File[] files = folder.listFiles((d, n) -> n.endsWith(".gguf"));
        if (files == null || files.length == 0) throw new Exception("The " + model + " model isn't downloaded.");
        File exe = new File(getApplicationInfo().nativeLibraryDir, "libllamaserver.so");
        if (!exe.exists()) throw new Exception("The AI engine is missing from this build.");
        int port;
        try (ServerSocket s = new ServerSocket(0)) { port = s.getLocalPort(); }
        int threads = Math.max(2, Math.min(6, Runtime.getRuntime().availableProcessors() - 2));
        ProcessBuilder pb = new ProcessBuilder(exe.getPath(), "-m", files[0].getPath(), "--host", "127.0.0.1",
                "--port", String.valueOf(port), "-c", String.valueOf(context), "--jinja", "-t", String.valueOf(threads));
        pb.redirectErrorStream(true);
        pb.redirectOutput(new File(getFilesDir(), "engine.log"));
        engine = pb.start();
        long deadline = System.currentTimeMillis() + 240_000;
        while (System.currentTimeMillis() < deadline) {
            if (!engine.isAlive()) throw new Exception("The model failed to load: " + tail(new File(getFilesDir(), "engine.log")));
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

    private synchronized void stopEngine() {
        if (engine != null) engine.destroy();
        engine = null;
        engineModel = null;
    }

    private boolean healthy(int port) {
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

    private void download(String key, String repo, String quant) throws Exception {
        String api = "https://huggingface.co/api/models/" + repo;
        JSONObject info = new JSONObject(new String(get(api, 5_000_000), StandardCharsets.UTF_8));
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
                        progress(key, done, total, "cancelled");
                        return;
                    }
                }
                out.write(buf, 0, n);
                done += n;
                long now = System.currentTimeMillis();
                if (now - lastReport > 500) {
                    lastReport = now;
                    progress(key, done, total, null);
                }
            }
        }
        if (!part.renameTo(new File(folder, name))) throw new Exception("Couldn't save the model file.");
        progress(key, done, total, "done");
    }

    private void progress(String key, long done, long total, String status) {
        try {
            JSONObject ev = new JSONObject();
            ev.put("type", "download");
            ev.put("key", key);
            ev.put("done", done);
            ev.put("total", total);
            if (status != null) ev.put("status", status);
            event(ev);
        } catch (Exception ignored) {
        }
    }

    private static byte[] get(String url, int limit) throws Exception {
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

    // ------------------------------------------------ what the page can call

    class Bridge {
        // Small, quick file reads and writes for chats, projects, agents and settings.

        @JavascriptInterface
        public String read(String path) {
            try {
                File f = safe(dataDir(), path);
                return f.exists() ? new String(Files.readAllBytes(f.toPath()), StandardCharsets.UTF_8) : null;
            } catch (Exception e) {
                return null;
            }
        }

        @JavascriptInterface
        public boolean write(String path, String text) {
            try {
                File f = safe(dataDir(), path);
                f.getParentFile().mkdirs();
                File tmp = new File(f.getPath() + ".tmp");
                Files.write(tmp.toPath(), text.getBytes(StandardCharsets.UTF_8));
                return tmp.renameTo(f);
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public String list(String dir) {
            JSONArray out = new JSONArray();
            try {
                File[] files = safe(dataDir(), dir).listFiles();
                if (files != null) for (File f : files) if (f.getName().endsWith(".json")) out.put(f.getName());
            } catch (Exception ignored) {
            }
            return out.toString();
        }

        @JavascriptInterface
        public boolean remove(String path) {
            try {
                return safe(dataDir(), path).delete();
            } catch (Exception e) {
                return false;
            }
        }

        /** Which models are downloaded, as {key: sizeInBytes}. */
        @JavascriptInterface
        public String models() {
            JSONObject out = new JSONObject();
            File[] dirs = modelsDir().listFiles();
            if (dirs != null) for (File d : dirs) {
                File[] g = d.listFiles((x, n) -> n.endsWith(".gguf"));
                try {
                    if (g != null && g.length > 0) out.put(d.getName(), g[0].length());
                } catch (Exception ignored) {
                }
            }
            return out.toString();
        }

        @JavascriptInterface
        public boolean deleteModel(String key) {
            try {
                if (key.equals(engineModel)) stopEngine();
                File[] files = safe(modelsDir(), key).listFiles();
                if (files != null) for (File f : files) f.delete();
                return safe(modelsDir(), key).delete();
            } catch (Exception e) {
                return false;
            }
        }

        @JavascriptInterface
        public void cancelDownload(String key) {
            synchronized (cancelled) {
                cancelled.add(key);
            }
        }

        @JavascriptInterface
        public long freeSpace() {
            return getFilesDir().getUsableSpace();
        }

        @JavascriptInterface
        public long memory() {
            android.app.ActivityManager.MemoryInfo mi = new android.app.ActivityManager.MemoryInfo();
            ((android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE)).getMemoryInfo(mi);
            return mi.totalMem;
        }

        @JavascriptInterface
        public String loaded() {
            return engineModel;
        }

        /** Slow jobs: the answer comes back through window.__bridgeReply(id). */
        @JavascriptInterface
        public void call(String id, String method, String argsJson) {
            pool.execute(() -> {
                try {
                    JSONObject a = new JSONObject(argsJson == null ? "{}" : argsJson);
                    switch (method) {
                        case "startEngine":
                            resolve(id, startEngine(a.getString("model"), a.optInt("context", 8192)), null);
                            break;
                        case "stopEngine":
                            stopEngine();
                            resolve(id, true, null);
                            break;
                        case "download":
                            resolve(id, true, null);
                            try {
                                download(a.getString("key"), a.getString("repo"), a.getString("quant"));
                            } catch (Exception e) {
                                Log.e(TAG, "download", e);
                                JSONObject ev = new JSONObject();
                                ev.put("type", "download");
                                ev.put("key", a.getString("key"));
                                ev.put("status", "error");
                                ev.put("error", String.valueOf(e.getMessage()));
                                event(ev);
                            }
                            break;
                        case "httpGet":
                            resolve(id, new String(get(a.getString("url"), 3_000_000), StandardCharsets.UTF_8), null);
                            break;
                        case "saveDownload":
                            resolve(id, saveToDownloads(a.getString("name"), a.getString("content"), a.optString("mime", "text/plain")), null);
                            break;
                        default:
                            resolve(id, null, "Unknown method " + method);
                    }
                } catch (Exception e) {
                    Log.e(TAG, method, e);
                    resolve(id, null, e.getMessage() == null ? e.toString() : e.getMessage());
                }
            });
        }
    }

    /** Saves a file the user asked for into the phone's Downloads folder. */
    private String saveToDownloads(String name, String content, String mime) throws Exception {
        String clean = name.replaceAll("[^\\w .()-]", "_");
        ContentValues v = new ContentValues();
        v.put(MediaStore.Downloads.DISPLAY_NAME, clean);
        v.put(MediaStore.Downloads.MIME_TYPE, mime);
        v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
        Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
        if (uri == null) throw new Exception("Couldn't save to Downloads.");
        try (OutputStream out = getContentResolver().openOutputStream(uri)) {
            out.write(content.getBytes(StandardCharsets.UTF_8));
        }
        return "Downloads/" + clean;
    }
}
