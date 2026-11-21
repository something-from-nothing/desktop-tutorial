package dev.pete.bootdash;

import android.content.ContentProviderClient;
import android.content.Context;
import android.os.Bundle;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** Asks LLM Chat on this phone (its "app.llmchat.bridge" link), or Claude directly as a backup. */
final class Ai {
    static final String AUTHORITY = "app.llmchat.bridge";

    static boolean ping(Context c, String key) {
        if (key == null || key.isEmpty()) return false;
        ContentProviderClient cl = null;
        try {
            cl = c.getContentResolver().acquireUnstableContentProviderClient(AUTHORITY);
            if (cl == null) return false;
            Bundle in = new Bundle();
            in.putString("key", key);
            Bundle out = cl.call("ping", null, in);
            return out != null && new JSONObject(out.getString("response", "{}")).optBoolean("ok");
        } catch (Exception e) {
            return false;
        } finally {
            if (cl != null) cl.close();
        }
    }

    static JSONObject ask(Context c, String key, String claudeKey, String reqJson, String defaultModel) {
        JSONObject req;
        try {
            req = new JSONObject(reqJson);
            if (req.optString("model").isEmpty()) req.put("model", defaultModel);
            int mt = req.optInt("max_tokens", 1400);
            if (mt <= 0 || mt > 8000) req.put("max_tokens", 1400);
            req.put("source", "bootdev-dashboard");
        } catch (Exception e) {
            return err("bad request");
        }
        String bridgeErr = "";
        if (key != null && !key.isEmpty()) {
            ContentProviderClient cl = null;
            try {
                cl = c.getContentResolver().acquireUnstableContentProviderClient(AUTHORITY);
                if (cl == null) {
                    bridgeErr = "LLM Chat isn't installed, or its version doesn't have the Boot.dev link yet.";
                } else {
                    Bundle in = new Bundle();
                    in.putString("key", key);
                    in.putString("request", req.toString());
                    Bundle out = cl.call("chat", null, in);
                    JSONObject r = new JSONObject(out == null ? "{}" : out.getString("response", "{}"));
                    if (r.optBoolean("ok")) { r.put("via", "llmchat"); return r; }
                    bridgeErr = r.optString("error", "LLM Chat could not answer.");
                }
            } catch (Exception e) {
                bridgeErr = "LLM Chat link failed: " + e.getMessage();
            } finally {
                if (cl != null) cl.close();
            }
        }
        if (claudeKey != null && !claudeKey.isEmpty()) return claude(claudeKey, req);
        if (bridgeErr.isEmpty()) bridgeErr = "Add the LLM Chat link key (or a Claude key) in Settings.";
        return err(bridgeErr);
    }

    static JSONObject claude(String apiKey, JSONObject req) {
        HttpURLConnection h = null;
        try {
            JSONObject body = new JSONObject();
            body.put("model", req.optString("model"));
            body.put("max_tokens", req.optInt("max_tokens", 1400));
            body.put("system", req.optString("system"));
            body.put("messages", req.optJSONArray("messages"));
            h = (HttpURLConnection) new URL("https://api.anthropic.com/v1/messages").openConnection();
            h.setConnectTimeout(20000);
            h.setReadTimeout(240000);
            h.setRequestMethod("POST");
            h.setRequestProperty("content-type", "application/json");
            h.setRequestProperty("x-api-key", apiKey);
            h.setRequestProperty("anthropic-version", "2023-06-01");
            h.setDoOutput(true);
            byte[] b = body.toString().getBytes("UTF-8");
            h.setFixedLengthStreamingMode(b.length);
            OutputStream o = h.getOutputStream();
            o.write(b);
            o.close();
            int status = h.getResponseCode();
            InputStream in = status >= 400 ? h.getErrorStream() : h.getInputStream();
            JSONObject r = new JSONObject(in == null ? "{}" : read(in));
            if (status != 200) {
                JSONObject e = r.optJSONObject("error");
                return err("Claude returned HTTP " + status + (e != null ? ": " + e.optString("message") : ""));
            }
            StringBuilder sb = new StringBuilder();
            JSONArray content = r.optJSONArray("content");
            for (int i = 0; content != null && i < content.length(); i++) {
                JSONObject part = content.getJSONObject(i);
                if ("text".equals(part.optString("type"))) sb.append(part.optString("text"));
            }
            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("text", sb.toString());
            out.put("via", "claude");
            out.put("model", r.optString("model"));
            return out;
        } catch (Exception e) {
            return err(String.valueOf(e.getMessage()));
        } finally {
            if (h != null) h.disconnect();
        }
    }

    private static JSONObject err(String m) {
        JSONObject o = new JSONObject();
        try { o.put("ok", false); o.put("error", m); } catch (Exception e) { }
        return o;
    }

    private static String read(InputStream in) throws Exception {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        byte[] b = new byte[65536];
        int n;
        while ((n = in.read(b)) > 0) buf.write(b, 0, n);
        in.close();
        return buf.toString("UTF-8");
    }
}
