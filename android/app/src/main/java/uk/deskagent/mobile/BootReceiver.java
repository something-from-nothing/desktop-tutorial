package uk.deskagent.mobile;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Android forgets alarms on restart and after an update; this puts them back. Also notices getting into the car. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) || Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            Scheduler.rescheduleAll(context);
            Car.ready(context, true);
        } else if (android.bluetooth.BluetoothDevice.ACTION_ACL_CONNECTED.equals(action)) {
            Car.bluetoothConnected(context, intent);
        }
    }
}
