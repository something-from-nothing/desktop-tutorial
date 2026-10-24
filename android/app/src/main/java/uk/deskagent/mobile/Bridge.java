package uk.deskagent.mobile;

import android.Manifest;
import android.app.ActivityManager;
import android.app.AlarmManager;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * What the page (assets/index.html) can ask Android to do, as window.Android. The
 * app window and the background runner for scheduled chats each have one.
 */
class Bridge {
    private static final String TAG = "DeskAgent";
    private static final ExecutorService pool = Executors.newCachedThreadPool();
    private final Context context;
    private final WebView web;
    private final Core core;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final boolean background;
    private final Runnable askNotifications;
    /** Set by the background runner: called when a scheduled chat finishes. */
    Runnable onScheduledDone;

    Bridge(Context context, WebView web, boolean background, Runnable askNotifications) {
        this.context = context;
        this.web = web;
        this.core = Core.get(context);
        this.background = background;
        this.askNotifications = askNotifications;
        synchronized (core.bridges) {
            core.bridges.add(this);
        }
    }

    void detach() {
        synchronized (core.bridges) {
            core.bridges.remove(this);
        }
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

    void event(JSONObject ev) {
        String js = "window.__bridgeEvent(" + JSONObject.quote(ev.toString()) + ")";
        ui.post(() -> web.evaluateJavascript(js, null));
    }

    // ------------------------------------------------ quick calls

    @JavascriptInterface
    public String read(String path) {
        return core.readData(path);
    }

    @JavascriptInterface
    public boolean write(String path, String text) {
        try {
            File f = Core.safe(core.dataDir(), path);
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
            File[] files = Core.safe(core.dataDir(), dir).listFiles();
            if (files != null) for (File f : files) if (f.getName().endsWith(".json")) out.put(f.getName());
        } catch (Exception ignored) {
        }
        return out.toString();
    }

    @JavascriptInterface
    public boolean remove(String path) {
        try {
            return Core.safe(core.dataDir(), path).delete();
        } catch (Exception e) {
            return false;
        }
    }

    /** Which models are downloaded, as {key: sizeInBytes}. */
    @JavascriptInterface
    public String models() {
        JSONObject out = new JSONObject();
        File[] dirs = core.modelsDir().listFiles();
        if (dirs != null) for (File d : dirs) {
            File[] g = d.listFiles((x, n) -> n.endsWith(".gguf"));
            try {
                if (g != null && g.length > 0) out.put(d.getName(), g[0].length());
            } catch (Exception ignored) {
            }
        }
        // Models that came inside the app count as downloaded.
        File[] libs = new File(context.getApplicationInfo().nativeLibraryDir).listFiles((x, n) -> n.startsWith("libmodel_") && n.endsWith(".so"));
        if (libs != null) for (File f : libs) {
            String key = f.getName().substring(9, f.getName().length() - 3);
            try {
                if (!out.has(key) && core.bundledModel(key) != null) out.put(key, f.length());
            } catch (Exception ignored) {
            }
        }
        return out.toString();
    }

    /** Models that came inside the app, which can't be deleted. */
    @JavascriptInterface
    public String builtInModels() {
        JSONArray out = new JSONArray();
        File[] libs = new File(context.getApplicationInfo().nativeLibraryDir).listFiles((x, n) -> n.startsWith("libmodel_") && n.endsWith(".so"));
        if (libs != null) for (File f : libs) out.put(f.getName().substring(9, f.getName().length() - 3));
        return out.toString();
    }

    @JavascriptInterface
    public boolean deleteModel(String key) {
        try {
            if (key.equals(core.engineModel())) core.stopEngine();
            File[] files = Core.safe(core.modelsDir(), key).listFiles();
            if (files != null) for (File f : files) f.delete();
            return Core.safe(core.modelsDir(), key).delete();
        } catch (Exception e) {
            return false;
        }
    }

    @JavascriptInterface
    public void cancelDownload(String key) {
        core.cancelDownload(key);
    }

    @JavascriptInterface
    public long freeSpace() {
        return context.getFilesDir().getUsableSpace();
    }

    @JavascriptInterface
    public long memory() {
        ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
        context.getSystemService(ActivityManager.class).getMemoryInfo(mi);
        return mi.totalMem;
    }

    @JavascriptInterface
    public String loaded() {
        return core.engineModel();
    }

    /** True in the hidden page that runs scheduled chats. */
    @JavascriptInterface
    public boolean isBackground() {
        return background;
    }

    // ------------------------------------------------ phone control

    @JavascriptInterface
    public boolean phoneEnabled() {
        return PhoneControl.enabled();
    }

    @JavascriptInterface
    public void openPhoneSettings() {
        PhoneControl.openSettings(context);
    }

    /** Keeps the app running while the AI uses other apps, with a Stop button in the notification. */
    @JavascriptInterface
    public void keepAlive(boolean on) {
        Intent i = new Intent(context, RunService.class).setAction(on ? RunService.ACTION_KEEP_ALIVE : RunService.ACTION_STOP);
        if (on) context.startForegroundService(i); else context.startService(i);
    }

    /**
     * Opens Claude, Gemini or Perplexity's own website in a browser tab over the app,
     * where you sign in with your own account. Only these three sites.
     */
    @JavascriptInterface
    public void openWebsite(String which) {
        String url;
        switch (which) {
            case "claude": url = "https://claude.ai/new"; break;
            case "gemini": url = "https://gemini.google.com/app"; break;
            case "perplexity": url = "https://www.perplexity.ai/"; break;
            default: return;
        }
        // A Chrome Custom Tab: the browser's own tab, so Google and other sign ins work
        // and you stay signed in, shown on top of Desk Agent.
        Bundle extras = new Bundle();
        extras.putBinder("android.support.customtabs.extra.SESSION", null);
        Intent tab = new Intent(Intent.ACTION_VIEW, Uri.parse(url)).putExtras(extras)
                .putExtra("android.support.customtabs.extra.TOOLBAR_COLOR", 0xFF1F5F4A)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(tab);
    }

    @JavascriptInterface
    public void bringToFront() {
        context.startActivity(new Intent(context, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT));
    }

    // ------------------------------------------------ scheduled chats

    @JavascriptInterface
    public boolean exactAlarmsAllowed() {
        return android.os.Build.VERSION.SDK_INT < 31 || context.getSystemService(AlarmManager.class).canScheduleExactAlarms();
    }

    @JavascriptInterface
    public void openAlarmSettings() {
        if (android.os.Build.VERSION.SDK_INT < 31) return;
        context.startActivity(new Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, Uri.parse("package:" + context.getPackageName()))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    @JavascriptInterface
    public boolean notificationsAllowed() {
        return android.os.Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED;
    }

    @JavascriptInterface
    public void askForNotifications() {
        if (askNotifications != null) ui.post(askNotifications);
    }

    /** The page saved schedules.json; set the alarms to match. */
    @JavascriptInterface
    public void schedulesChanged() {
        Scheduler.rescheduleAll(context);
    }

    /** A scheduled chat finished: tell the user, then let the runner stop. */
    @JavascriptInterface
    public void scheduledDone(String chatId, String title, String text) {
        if (notificationsAllowed()) {
            Intent open = new Intent(context, MainActivity.class).putExtra("chat", chatId).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            PendingIntent tap = PendingIntent.getActivity(context, chatId.hashCode(), open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            android.app.Notification n = new android.app.Notification.Builder(context, Core.CHANNEL_RESULTS)
                    .setSmallIcon(R.drawable.ic_notify)
                    .setContentTitle(title)
                    .setContentText(text)
                    .setStyle(new android.app.Notification.BigTextStyle().bigText(text))
                    .setContentIntent(tap)
                    .setAutoCancel(true)
                    .build();
            context.getSystemService(NotificationManager.class).notify(chatId.hashCode(), n);
        }
        if (onScheduledDone != null) ui.post(onScheduledDone);
    }

    // ------------------------------------------------ slow calls, answered through window.__bridgeReply(id)

    @JavascriptInterface
    public void call(String id, String method, String argsJson) {
        pool.execute(() -> {
            try {
                JSONObject a = new JSONObject(argsJson == null ? "{}" : argsJson);
                switch (method) {
                    case "startEngine":
                        resolve(id, core.startEngine(a.getString("model"), a.optInt("context", 8192)), null);
                        break;
                    case "stopEngine":
                        core.stopEngine();
                        resolve(id, true, null);
                        break;
                    case "download":
                        resolve(id, true, null);
                        try {
                            core.download(a.getString("key"), a.getString("repo"), a.getString("quant"));
                        } catch (Exception e) {
                            Log.e(TAG, "download", e);
                            core.progress(a.getString("key"), 0, 0, "error", String.valueOf(e.getMessage()));
                        }
                        break;
                    case "httpGet":
                        resolve(id, new String(Core.get(a.getString("url"), 3_000_000), StandardCharsets.UTF_8), null);
                        break;
                    case "saveDownload":
                        resolve(id, core.saveToDownloads(a.getString("name"), a.getString("content"), a.optString("mime", "text/plain")), null);
                        break;
                    case "phoneRead":
                        resolve(id, PhoneControl.readScreen(), null);
                        break;
                    case "phoneTap":
                        resolve(id, PhoneControl.tap(a.getInt("item")), null);
                        break;
                    case "phoneType":
                        resolve(id, PhoneControl.type(a.optInt("item", 0), a.getString("text")), null);
                        break;
                    case "phoneScroll":
                        resolve(id, PhoneControl.scroll(a.optString("direction", "down"), a.optInt("item", 0)), null);
                        break;
                    case "phonePress":
                        resolve(id, PhoneControl.press(a.getString("button")), null);
                        break;
                    case "phoneOpen":
                        resolve(id, PhoneControl.openApp(context, a.getString("name")), null);
                        break;
                    case "phoneApps":
                        resolve(id, PhoneControl.listApps(context), null);
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
