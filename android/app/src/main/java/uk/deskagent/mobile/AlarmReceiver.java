package uk.deskagent.mobile;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Wakes up for a scheduled chat. */
public class AlarmReceiver extends BroadcastReceiver {
    static final String ACTION_RUN = "uk.deskagent.mobile.RUN_SCHEDULE";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (!ACTION_RUN.equals(intent.getAction())) return;
        Intent run = new Intent(context, RunService.class).setAction(RunService.ACTION_RUN).putExtra("schedule", intent.getStringExtra("schedule"));
        context.startForegroundService(run);
    }
}
