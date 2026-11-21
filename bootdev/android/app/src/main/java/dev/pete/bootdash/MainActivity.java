package dev.pete.bootdash;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.HashSet;
import java.util.Set;

/** Two tabs: boot.dev itself (you sign in once, cookies are kept) and the live dashboard. */
public class MainActivity extends Activity {
    static final String START = "https://www.boot.dev/dashboard";

    WebView boot, dash;
    Host host;
    String reader;
    private TextView tabBoot, tabDash;
    private boolean onDash;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        reader = asset("reader.js");

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF0F1117);
        FrameLayout frame = new FrameLayout(this);
        root.addView(frame, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        boot = new WebView(this);
        dash = new WebView(this);
        setup(boot);
        setup(dash);
        String ua = boot.getSettings().getUserAgentString();
        boot.getSettings().setUserAgentString(ua.replace("; wv", ""));
        frame.addView(boot, new FrameLayout.LayoutParams(-1, -1));
        frame.addView(dash, new FrameLayout.LayoutParams(-1, -1));

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        cm.setAcceptThirdPartyCookies(boot, true);

        host = new Host(this);
        boot.addJavascriptInterface(host.bootSide, "BootHost");
        dash.addJavascriptInterface(host.dashSide, "Android");

        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            Set<String> origins = new HashSet<>();
            origins.add("https://www.boot.dev");
            origins.add("https://boot.dev");
            WebViewCompat.addDocumentStartJavaScript(boot, reader, origins);
        }
        boot.setWebChromeClient(new WebChromeClient());
        boot.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                if (isBootDev(u)) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (Exception e) { }
                return true;
            }
            @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) { inject(url); }
            @Override public void onPageFinished(WebView v, String url) { inject(url); }
        });
        dash.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, r.getUrl())); } catch (Exception e) { }
                return true;
            }
        });

        LinearLayout bar = new LinearLayout(this);
        bar.setBackgroundColor(0xFF171A23);
        tabBoot = tab("Boot.dev");
        tabDash = tab("Dashboard");
        bar.addView(tabBoot, new LinearLayout.LayoutParams(0, -1, 1f));
        bar.addView(tabDash, new LinearLayout.LayoutParams(0, -1, 1f));
        tabBoot.setOnClickListener(v -> show(false));
        tabDash.setOnClickListener(v -> show(true));
        root.addView(bar, new LinearLayout.LayoutParams(-1, dp(56)));
        setContentView(root);

        boot.loadUrl(START);
        dash.loadDataWithBaseURL("https://bootdash.local/", asset("dashboard.html"), "text/html", "utf-8", null);
        show(false);
    }

    private void setup(WebView w) {
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setTextZoom(100);
        w.setBackgroundColor(0xFF0F1117);
    }

    static boolean isBootDev(Uri u) {
        String h = u.getHost();
        return "https".equals(u.getScheme()) && h != null && (h.equals("boot.dev") || h.endsWith(".boot.dev"));
    }

    void inject(String url) {
        if (url == null || !isBootDev(Uri.parse(url))) return;
        boot.evaluateJavascript(reader, null);
    }

    private TextView tab(String label) {
        TextView t = new TextView(this);
        t.setText(label);
        t.setGravity(Gravity.CENTER);
        t.setTextSize(15);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    void show(boolean dashboard) {
        onDash = dashboard;
        boot.setVisibility(dashboard ? View.GONE : View.VISIBLE);
        dash.setVisibility(dashboard ? View.VISIBLE : View.GONE);
        tabBoot.setTextColor(dashboard ? 0xFF9AA0B4 : 0xFFF4C04E);
        tabDash.setTextColor(dashboard ? 0xFFF4C04E : 0xFF9AA0B4);
    }

    void openBoot(String url) {
        Uri u = Uri.parse(url == null ? START : url);
        if (!isBootDev(u)) u = Uri.parse(START);
        String now = boot.getUrl();
        if (now == null || !now.equals(u.toString())) boot.loadUrl(u.toString());
        show(false);
    }

    private int dp(int v) { return Math.round(v * getResources().getDisplayMetrics().density); }

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

    @Override public void onBackPressed() {
        if (onDash) { show(false); return; }
        if (boot.canGoBack()) { boot.goBack(); return; }
        super.onBackPressed();
    }

    @Override protected void onPause() {
        CookieManager.getInstance().flush();
        boot.onPause();
        dash.onPause();
        super.onPause();
    }

    @Override protected void onResume() {
        super.onResume();
        boot.onResume();
        dash.onResume();
    }

    @Override protected void onDestroy() {
        if (host != null) host.shutdown();
        boot.removeJavascriptInterface("BootHost");
        dash.removeJavascriptInterface("Android");
        boot.destroy();
        dash.destroy();
        super.onDestroy();
    }

}
