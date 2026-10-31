package uk.deskagent.mobile;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Person;
import android.app.RemoteInput;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.drawable.Icon;
import android.os.Bundle;
import android.service.notification.StatusBarNotification;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Android Auto. Google only lets a few kinds of app onto a car's screen, and a chat
 * assistant isn't one of them, so Desk Agent appears the way messaging apps do: as a
 * conversation the car reads out loud and that you answer by voice. Your reply comes
 * back here, Desk Agent answers in a hidden copy of the page (RunService), and the
 * answer is posted to the same conversation for the car to read out.
 */
final class Car {
    static final String CHANNEL = "car";
    static final String KEY_TEXT = "text";
    private static final int NOTE_ID = 7070;
    private static final int KEEP = 8;  // messages shown in the conversation

    private Car() {
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("car", Context.MODE_PRIVATE);
    }

    static boolean on(Context c) {
        return prefs(c).getBoolean("on", false);
    }

    static void setOn(Context c, boolean on) {
        prefs(c).edit().putBoolean("on", on).apply();
        if (on) ready(c, false);
        else c.getSystemService(NotificationManager.class).cancel(NOTE_ID);
    }

    static boolean showing(Context c) {
        for (StatusBarNotification n : c.getSystemService(NotificationManager.class).getActiveNotifications())
            if (n.getId() == NOTE_ID) return true;
        return false;
    }

    /** Puts the conversation in the notifications so there's something to reply to in the car. */
    static void ready(Context c, boolean silent) {
        if (!on(c)) return;
        if (history(c).length() == 0 || !showing(c)) add(c, false, "Ready when you are. Reply to ask me anything.");
        show(c, silent);
    }

    static synchronized void add(Context c, boolean me, String text) {
        JSONArray h = history(c), out = new JSONArray();
        try {
            h.put(new JSONObject().put("me", me).put("text", text).put("at", System.currentTimeMillis()));
            for (int i = Math.max(0, h.length() - KEEP); i < h.length(); i++) out.put(h.get(i));
        } catch (Exception ignored) {
        }
        prefs(c).edit().putString("history", out.toString()).apply();
    }

    private static JSONArray history(Context c) {
        try {
            return new JSONArray(prefs(c).getString("history", "[]"));
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    static void channel(Context c) {
        NotificationChannel ch = new NotificationChannel(CHANNEL, "Android Auto", NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Desk Agent's answers, read out in the car");
        c.getSystemService(NotificationManager.class).createNotificationChannel(ch);
    }

    /** The conversation as Android Auto expects it: messaging style, a voice reply and mark as read. */
    static void show(Context c, boolean silent) {
        channel(c);
        Person you = new Person.Builder().setName("You").setKey("you").build();
        Person agent = new Person.Builder().setName("Desk Agent").setKey("deskagent").setBot(true)
                .setIcon(Icon.createWithResource(c, R.mipmap.ic_launcher)).build();
        Notification.MessagingStyle style = new Notification.MessagingStyle(you).setGroupConversation(false);
        JSONArray h = history(c);
        for (int i = 0; i < h.length(); i++) {
            JSONObject m = h.optJSONObject(i);
            if (m == null) continue;
            style.addMessage(new Notification.MessagingStyle.Message(m.optString("text"), m.optLong("at"), m.optBoolean("me") ? null : agent));
        }

        RemoteInput input = new RemoteInput.Builder(KEY_TEXT).setLabel("Ask Desk Agent").build();
        PendingIntent replyPi = PendingIntent.getBroadcast(c, 70, new Intent(c, Reply.class).setAction("reply"),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
        Notification.Action reply = new Notification.Action.Builder(Icon.createWithResource(c, R.drawable.ic_notify), "Reply", replyPi)
                .addRemoteInput(input).setAllowGeneratedReplies(false)
                .setSemanticAction(Notification.Action.SEMANTIC_ACTION_REPLY)
                .addExtras(noUi(Notification.Action.SEMANTIC_ACTION_REPLY)).build();
        PendingIntent readPi = PendingIntent.getBroadcast(c, 71, new Intent(c, Reply.class).setAction("read"), PendingIntent.FLAG_IMMUTABLE);
        Notification.Action read = new Notification.Action.Builder(Icon.createWithResource(c, R.drawable.ic_notify), "Mark as read", readPi)
                .setSemanticAction(Notification.Action.SEMANTIC_ACTION_MARK_AS_READ)
                .addExtras(noUi(Notification.Action.SEMANTIC_ACTION_MARK_AS_READ)).build();
        PendingIntent open = PendingIntent.getActivity(c, 72, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);

        Notification n = new Notification.Builder(c, CHANNEL)
                .setSmallIcon(R.drawable.ic_notify)
                .setStyle(style)
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setContentIntent(open)
                .addAction(reply)
                .addAction(read)
                .setOnlyAlertOnce(silent)
                // A quiet update (what was heard, marked as read, back after a restart): no sound or buzz.
                // Android has no plain "silent" switch; a grouped notification that only lets the
                // group summary alert is how AndroidX does it.
                .setGroup(silent ? "car-quiet" : null)
                .setGroupAlertBehavior(silent ? Notification.GROUP_ALERT_SUMMARY : Notification.GROUP_ALERT_ALL)
                .build();
        c.getSystemService(NotificationManager.class).notify(NOTE_ID, n);
    }

    /** Android Auto reads these through AndroidX: the action must not open the phone's screen. */
    private static Bundle noUi(int semantic) {
        Bundle b = new Bundle();
        b.putBoolean("android.support.action.showsUserInterface", false);
        b.putInt("android.support.action.semanticAction", semantic);
        return b;
    }

    /** An answer is ready: add it and let the car read it out. */
    static void answer(Context c, String text) {
        add(c, false, text.isEmpty() ? "I couldn't come up with an answer." : text);
        show(c, false);
    }

    /** Spoken replies and "mark as read" from the car (or the phone's notification). */
    public static class Reply extends BroadcastReceiver {
        @Override
        public void onReceive(Context c, Intent intent) {
            if ("read".equals(intent.getAction())) {
                show(c, true);
                return;
            }
            Bundle r = RemoteInput.getResultsFromIntent(intent);
            CharSequence said = r == null ? null : r.getCharSequence(KEY_TEXT);
            if (said == null || said.toString().trim().isEmpty()) {
                show(c, true);
                return;
            }
            String text = said.toString().trim();
            add(c, true, text);
            show(c, true);  // stops the reply spinner and shows what was heard
            c.startForegroundService(new Intent(c, RunService.class).setAction(RunService.ACTION_CAR).putExtra("text", text));
        }
    }

    /** Getting into the car (its Bluetooth connects): put the conversation back if it was cleared. */
    static void bluetoothConnected(Context c, Intent intent) {
        if (!on(c) || c.checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED) return;
        @SuppressWarnings("deprecation") BluetoothDevice d = intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE);
        try {
            BluetoothClass k = d == null ? null : d.getBluetoothClass();
            if (k == null || k.getDeviceClass() != BluetoothClass.Device.AUDIO_VIDEO_CAR_AUDIO) return;
        } catch (SecurityException e) {
            return;
        }
        if (!showing(c)) ready(c, false);
    }
}
