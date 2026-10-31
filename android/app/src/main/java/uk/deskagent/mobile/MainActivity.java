package uk.deskagent.mobile;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.drawable.Icon;
import android.net.Uri;
import android.os.Bundle;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

/**
 * Desk Agent Mobile. The interface is a web page (assets/index.html) shown in a
 * WebView; Bridge is what the page calls for things a page can't do itself.
 * Nothing is sent anywhere except model downloads from Hugging Face and the web
 * searches the AI makes.
 */
public class MainActivity extends Activity {
    /** Whether the app is on screen right now; read by Bridge.onScreen(). */
    static volatile boolean onScreen;
    private WebView web;
    private Bridge bridge;
    private ValueCallback<Uri[]> filePicked;
    private boolean pageReady;
    private String openChatWhenReady;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowContentAccess(false);
        s.setMediaPlaybackRequiresUserGesture(true);
        bridge = new Bridge(this, web, false, () -> requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 2));
        web.addJavascriptInterface(bridge, "Android");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
                // Links the AI gives open in the phone's browser, not inside the app.
                startActivity(new Intent(Intent.ACTION_VIEW, req.getUrl()));
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                pageReady = true;
                if (openChatWhenReady != null) openChat(openChatWhenReady);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (filePicked != null) filePicked.onReceiveValue(null);
                filePicked = callback;
                Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("*/*");
                pick.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                startActivityForResult(Intent.createChooser(pick, "Attach files"), 1);
                return true;
            }
        });
        openChatWhenReady = getIntent().getStringExtra("chat");
        web.loadUrl("file:///android_asset/index.html");
        offerHomeScreenIcon();
    }

    /**
     * The Pixel launcher only puts Play Store installs on the home screen, so the
     * first time the app opens it asks to add its own icon there.
     */
    private void offerHomeScreenIcon() {
        SharedPreferences prefs = getSharedPreferences("app", MODE_PRIVATE);
        if (prefs.getBoolean("icon_offered", false)) return;
        prefs.edit().putBoolean("icon_offered", true).apply();
        try {
            ShortcutManager sm = getSystemService(ShortcutManager.class);
            if (sm == null || !sm.isRequestPinShortcutSupported()) return;
            Intent open = new Intent(this, MainActivity.class).setAction(Intent.ACTION_MAIN);
            sm.requestPinShortcut(new ShortcutInfo.Builder(this, "home")
                    .setShortLabel("Desk Agent")
                    .setIcon(Icon.createWithResource(this, R.mipmap.ic_launcher))
                    .setIntent(open)
                    .build(), null);
        } catch (Exception ignored) {
        }
    }

    /** Tapping a scheduled chat's notification opens that chat. */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String chat = intent.getStringExtra("chat");
        if (chat == null) return;
        if (pageReady) openChat(chat); else openChatWhenReady = chat;
    }

    private void openChat(String id) {
        openChatWhenReady = null;
        web.evaluateJavascript("window.openChat && openChat(" + JSONObject.quote(id) + ")", null);
    }

    @Override
    protected void onPause() {
        onScreen = false;
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        onScreen = true;
        // Coming back from Settings: let the page notice phone control or alarms were switched on.
        if (pageReady) web.evaluateJavascript("window.onResumeApp && onResumeApp()", null);
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        if (request != 1 || filePicked == null) return;
        Uri[] uris = null;
        if (result == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                uris = new Uri[data.getClipData().getItemCount()];
                for (int i = 0; i < uris.length; i++) uris[i] = data.getClipData().getItemAt(i).getUri();
            } else if (data.getData() != null) {
                uris = new Uri[]{data.getData()};
            }
        }
        filePicked.onReceiveValue(uris);
        filePicked = null;
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.onBack && window.onBack()", value -> {
            if (!"true".equals(value)) MainActivity.super.onBackPressed();
        });
    }

    @Override
    protected void onDestroy() {
        bridge.detach();
        // The engine keeps running only if a scheduled chat or phone control needs it.
        super.onDestroy();
    }
}
