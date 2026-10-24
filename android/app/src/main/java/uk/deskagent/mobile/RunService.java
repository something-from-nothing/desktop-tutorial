package uk.deskagent.mobile;

import android.app.Notification;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.webkit.WebView;

import org.json.JSONObject;

import java.util.ArrayDeque;

/**
 * Keeps Desk Agent working when its window isn't on screen: while the AI is using
 * other apps, and to run scheduled chats in a hidden copy of the page. Android
 * shows its notification the whole time, with a Stop button.
 */
public class RunService extends Service {
    static final String ACTION_RUN = "run";
    static final String ACTION_KEEP_ALIVE = "keep";
    static final String ACTION_STOP = "stop";
    static final String ACTION_STOP_JOB = "stopjob";
    private static final long RUN_LIMIT_MS = 20 * 60_000;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ArrayDeque<String> queue = new ArrayDeque<>();
    private WebView hidden;
    private Bridge hiddenBridge;
    private boolean keepAlive;
    private final Runnable giveUp = this::finishRun;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_STOP : intent.getAction();
        if (ACTION_RUN.equals(action)) {
            String id = intent.getStringExtra("schedule");
            showNotification("Running a scheduled chat");
            if (id != null) queue.add(id);
            if (hidden == null) runNext();
        } else if (ACTION_KEEP_ALIVE.equals(action)) {
            keepAlive = true;
            showNotification("Desk Agent is using your phone");
        } else if (ACTION_STOP_JOB.equals(action)) {
            try {
                JSONObject ev = new JSONObject().put("type", "stop");
                Core core = Core.get(this);
                synchronized (core.bridges) {
                    for (Bridge b : core.bridges) b.event(ev);
                }
            } catch (Exception ignored) {
            }
            keepAlive = false;
            stopIfIdle();
        } else {
            keepAlive = false;
            stopIfIdle();
        }
        return START_NOT_STICKY;
    }

    private void showNotification(String text) {
        Intent stop = new Intent(this, RunService.class).setAction(ACTION_STOP_JOB);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stop, PendingIntent.FLAG_IMMUTABLE);
        PendingIntent open = PendingIntent.getActivity(this, 2, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, Core.CHANNEL_RUNS)
                .setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Desk Agent")
                .setContentText(text)
                .setContentIntent(open)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Stop", stopPi).build())
                .build();
        startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
    }

    private void runNext() {
        String id = queue.poll();
        if (id == null) {
            stopIfIdle();
            return;
        }
        hidden = new WebView(this);
        hidden.getSettings().setJavaScriptEnabled(true);
        hidden.getSettings().setDomStorageEnabled(true);
        hidden.getSettings().setAllowFileAccess(true);
        hiddenBridge = new Bridge(this, hidden, true, null);
        hiddenBridge.onScheduledDone = this::finishRun;
        hidden.addJavascriptInterface(hiddenBridge, "Android");
        hidden.loadUrl("file:///android_asset/index.html?run=" + id);
        ui.postDelayed(giveUp, RUN_LIMIT_MS);
    }

    private void finishRun() {
        ui.removeCallbacks(giveUp);
        if (hidden != null) {
            hiddenBridge.detach();
            hidden.destroy();
            hidden = null;
        }
        Scheduler.rescheduleAll(this);
        runNext();
    }

    private void stopIfIdle() {
        if (hidden == null && queue.isEmpty() && !keepAlive) {
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    @Override
    public void onDestroy() {
        if (hidden != null) {
            hiddenBridge.detach();
            hidden.destroy();
        }
        super.onDestroy();
    }
}
