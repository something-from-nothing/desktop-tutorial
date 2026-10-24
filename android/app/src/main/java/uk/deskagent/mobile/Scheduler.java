package uk.deskagent.mobile;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashSet;
import java.util.Set;

/**
 * Sets an alarm for each scheduled chat. The page works out when each one next
 * runs (next_run, in milliseconds) and saves schedules.json; this just matches the
 * alarms to it. Runs after every change, after a restart and after an update.
 */
final class Scheduler {
    private Scheduler() {
    }

    static void rescheduleAll(Context context) {
        AlarmManager am = context.getSystemService(AlarmManager.class);
        SharedPreferences prefs = context.getSharedPreferences("alarms", Context.MODE_PRIVATE);
        Set<String> before = new HashSet<>(prefs.getStringSet("ids", new HashSet<>()));
        Set<String> now = new HashSet<>();
        try {
            String text = Core.get(context).readData("schedules.json");
            JSONArray list = text == null ? new JSONArray() : new JSONArray(text);
            for (int i = 0; i < list.length(); i++) {
                JSONObject s = list.getJSONObject(i);
                long at = s.optLong("next_run", 0);
                if (!s.optBoolean("enabled", true) || at <= 0) continue;
                String id = s.getString("id");
                // Missed while the phone was off: run a minute from now instead.
                at = Math.max(at, System.currentTimeMillis() + 60_000);
                PendingIntent pi = intent(context, id);
                boolean exact = android.os.Build.VERSION.SDK_INT < 31 || am.canScheduleExactAlarms();
                if (exact) am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
                else am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, at, pi);
                now.add(id);
            }
        } catch (Exception ignored) {
        }
        before.removeAll(now);
        for (String gone : before) am.cancel(intent(context, gone));
        prefs.edit().putStringSet("ids", now).apply();
    }

    private static PendingIntent intent(Context context, String id) {
        Intent i = new Intent(context, AlarmReceiver.class).setAction(AlarmReceiver.ACTION_RUN).putExtra("schedule", id);
        return PendingIntent.getBroadcast(context, id.hashCode(), i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }
}
