package uk.deskagent.mobile;

import android.app.DownloadManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;

import java.io.File;
import java.util.HashSet;
import java.util.Set;

/**
 * Model downloads go through Android's own download manager, so they carry on with the
 * screen off or the app closed, wait out a dropped connection and continue after a
 * restart. The file arrives as NAME.part in the app's own storage and is renamed when
 * it's complete, so a half finished model is never mistaken for a working one.
 */
final class ModelDownloads {
    private ModelDownloads() {
    }

    /** Where downloaded models live: Android/data/uk.deskagent.mobile/files/models/KEY/. */
    static File dir(Context context) {
        File d = context.getExternalFilesDir("models");
        if (d != null) d.mkdirs();
        return d;
    }

    private static SharedPreferences prefs(Context context) {
        return context.getSharedPreferences("model_downloads", Context.MODE_PRIVATE);
    }

    private static DownloadManager dm(Context context) {
        return context.getSystemService(DownloadManager.class);
    }

    static Set<String> pending(Context context) {
        return new HashSet<>(prefs(context).getStringSet("keys", new HashSet<>()));
    }

    static void start(Context context, String key, String name, String url) throws Exception {
        cancel(context, key);
        File folder = Core.safe(dir(context), key);
        folder.mkdirs();
        File part = new File(folder, name + ".part");
        if (part.exists() && !part.delete()) throw new Exception("Couldn't clear an old part of the download.");
        DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url))
                .setTitle("Desk Agent model: " + key)
                .setDescription("Downloading an AI model to use offline")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationUri(Uri.fromFile(part))
                .setAllowedOverRoaming(false)
                .addRequestHeader("User-Agent", "DeskAgentMobile");
        long id = dm(context).enqueue(r);
        Set<String> keys = pending(context);
        keys.add(key);
        prefs(context).edit().putStringSet("keys", keys).putLong("id_" + key, id).putString("name_" + key, name).apply();
    }

    static void cancel(Context context, String key) {
        long id = prefs(context).getLong("id_" + key, -1);
        if (id >= 0) dm(context).remove(id);
        forget(context, key);
    }

    private static void forget(Context context, String key) {
        Set<String> keys = pending(context);
        keys.remove(key);
        prefs(context).edit().putStringSet("keys", keys).remove("id_" + key).remove("name_" + key).apply();
    }

    /**
     * Where one download is up to: {done, total, state}, where state is 0 running,
     * 1 finished (and renamed), 2 failed, 3 gone (cancelled). The failure reason is in
     * lastError.
     */
    static String lastError = "";

    static long[] check(Context context, String key) {
        long id = prefs(context).getLong("id_" + key, -1);
        if (id < 0) return new long[]{0, 0, 3};
        try (Cursor c = dm(context).query(new DownloadManager.Query().setFilterById(id))) {
            if (c == null || !c.moveToFirst()) {
                forget(context, key);
                return new long[]{0, 0, 3};
            }
            long done = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR));
            long total = c.getLong(c.getColumnIndexOrThrow(DownloadManager.COLUMN_TOTAL_SIZE_BYTES));
            int status = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
            if (status == DownloadManager.STATUS_SUCCESSFUL) {
                String name = prefs(context).getString("name_" + key, null);
                File folder = Core.safe(dir(context), key);
                File part = new File(folder, name + ".part");
                if (name == null || !part.renameTo(new File(folder, name))) {
                    lastError = "The download finished but couldn't be saved.";
                    forget(context, key);
                    return new long[]{done, total, 2};
                }
                forget(context, key);
                return new long[]{done, total, 1};
            }
            if (status == DownloadManager.STATUS_FAILED) {
                int reason = c.getInt(c.getColumnIndexOrThrow(DownloadManager.COLUMN_REASON));
                lastError = reason == DownloadManager.ERROR_INSUFFICIENT_SPACE ? "There isn't enough free space on the phone."
                        : reason == DownloadManager.ERROR_HTTP_DATA_ERROR || reason == DownloadManager.ERROR_TOO_MANY_REDIRECTS ? "The connection kept failing. Try again on Wi-Fi."
                        : "The download failed (code " + reason + "). Try again.";
                dm(context).remove(id);
                forget(context, key);
                return new long[]{done, total, 2};
            }
            return new long[]{done, total, 0};
        } catch (Exception e) {
            lastError = String.valueOf(e.getMessage());
            return new long[]{0, 0, 2};
        }
    }

    /** Android says a download finished, even with the app closed: rename it into place. */
    public static class Done extends BroadcastReceiver {
        @Override
        public void onReceive(Context context, Intent intent) {
            for (String key : pending(context)) check(context, key);
        }
    }
}
