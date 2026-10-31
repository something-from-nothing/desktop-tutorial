package uk.deskagent.mobile;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;

/**
 * Desk Agent Dashboard (the small separate app) asks here to connect, so there's no code
 * to type on the same phone. Only an app signed with the same key as Desk Agent gets an
 * answer: it switches remote control on and hands back a key. No screen of its own.
 */
public class ConnectActivity extends Activity {
    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        String caller = getCallingPackage();
        if (caller == null || getPackageManager().checkSignatures(caller, getPackageName()) != PackageManager.SIGNATURE_MATCH) {
            setResult(RESULT_CANCELED, new Intent().putExtra("error", "Only Desk Agent's own apps can connect this way."));
            finish();
            return;
        }
        try {
            RemoteServer server = RemoteServer.get(this);
            server.start();
            // Keeps Desk Agent answering with its window closed and the screen off.
            startForegroundService(new Intent(this, RunService.class).setAction(RunService.ACTION_KEEP_ALIVE).putExtra("text", "Remote control is on"));
            String had = getIntent().getStringExtra("key");
            String key = server.knows(had) ? had : server.issueKey();
            setResult(RESULT_OK, new Intent().putExtra("key", key).putExtra("port", RemoteServer.PORT));
        } catch (Exception e) {
            setResult(RESULT_CANCELED, new Intent().putExtra("error", "Desk Agent couldn't switch remote control on: " + e.getMessage()));
        }
        finish();
    }
}
