package dev.pete.fc26dash;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

/** One screen: the same dashboard page as the Windows app, fed by the PC or by the phone itself. */
public class MainActivity extends Activity {
    WebView web;
    Host host;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setTextZoom(100);
        web.setBackgroundColor(0xFF0F1117);
        host = new Host(this);
        web.addJavascriptInterface(host, "Android");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, r.getUrl())); } catch (Exception e) { }
                return true;
            }
        });
        setContentView(web);
        web.loadDataWithBaseURL("https://fc26dash.local/", asset("dashboard.html"), "text/html", "utf-8", null);
    }

    /** Hands a result back to the page. */
    void done(String id, String json) {
        final String js = "window.__done(" + org.json.JSONObject.quote(id) + "," + org.json.JSONObject.quote(json) + ")";
        runOnUiThread(() -> { if (web != null) web.evaluateJavascript(js, null); });
    }

    String asset(String name) {
        try {
            InputStream in = getAssets().open(name);
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] b = new byte[65536];
            int n;
            while ((n = in.read(b)) > 0) buf.write(b, 0, n);
            in.close();
            return buf.toString("UTF-8");
        } catch (Exception e) {
            return "";
        }
    }

    @Override protected void onPause() { web.onPause(); super.onPause(); }
    @Override protected void onResume() { super.onResume(); web.onResume(); }

    @Override protected void onDestroy() {
        if (host != null) host.shutdown();
        web.removeJavascriptInterface("Android");
        web.destroy();
        web = null;
        super.onDestroy();
    }
}
