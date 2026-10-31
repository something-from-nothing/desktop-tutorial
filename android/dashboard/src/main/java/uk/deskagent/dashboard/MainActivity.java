package uk.deskagent.dashboard;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.os.Bundle;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

/** Desk Agent Dashboard: one screen, Desk Agent's remote page opened on its Dashboard tab. */
public class MainActivity extends Activity {
    private static final int CONNECT = 9;
    private WebView web;

    /** window.DashApp in the page: connect to Desk Agent on this phone without a code. */
    public class DashApp {
        @JavascriptInterface
        public void connect(String key) {
            runOnUiThread(() -> {
                Intent i = new Intent().setClassName("uk.deskagent.mobile", "uk.deskagent.mobile.ConnectActivity");
                if (key != null && !key.isEmpty()) i.putExtra("key", key);
                try {
                    startActivityForResult(i, CONNECT);
                } catch (ActivityNotFoundException | SecurityException e) {
                    answer(null, "Desk Agent isn't on this phone, or it's an older version. Install the latest DeskAgent-Phone.apk, then open this again.");
                }
            });
        }
    }

    private void answer(String key, String error) {
        String js = "window.onDeskConnected && onDeskConnected(" + (key == null ? "null" : JSONObject.quote(key)) + ","
                + (error == null ? "null" : JSONObject.quote(error)) + ")";
        web.evaluateJavascript(js, null);
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != CONNECT) return;
        String key = data == null ? null : data.getStringExtra("key");
        if (result == RESULT_OK && key != null) answer(key, null);
        else answer(null, data != null && data.getStringExtra("error") != null ? data.getStringExtra("error")
                : "Desk Agent didn't connect. Open Desk Agent once, then try again.");
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);  // remembers the paired phone
        s.setAllowFileAccess(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl()));
                return true;
            }
        });
        web.addJavascriptInterface(new DashApp(), "DashApp");
        setContentView(web);
        if (state != null) web.restoreState(state);
        else web.loadUrl("file:///android_asset/remote.html?app=dashboard");
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        // The page goes back a step (a chat to the list, a tab to the Dashboard) before the app closes.
        web.evaluateJavascript("window.remoteBack ? remoteBack() : false", handled -> {
            if (!"true".equals(handled)) finish();
        });
    }

    @Override
    protected void onDestroy() {
        web.destroy();
        super.onDestroy();
    }
}
