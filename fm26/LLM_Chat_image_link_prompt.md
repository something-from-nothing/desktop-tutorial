# LLM Chat: let the local link take screen pictures (for FM26 Dashboard)

Give this whole file to whoever builds LLM Chat (the Windows EXE and the Android APK). It only adds to the local link that the Boot.dev Dashboard already uses. Nothing that works today should change.

## Why

FM26 Dashboard captures the Football Manager window (only that window, never the full desktop) and asks Claude to explain it. Today the LLM Chat link only takes text, so FM26 Dashboard sends pictures to Claude directly with a backup key. With this change, pictures go through LLM Chat like everything else.

## What to change on Windows (127.0.0.1:47821/bridge/v1)

1. **Accept the new source.** Requests from FM26 Dashboard carry `"source": "fm26-dashboard"`. Accept it with the same link key as `bootdev-dashboard`. If LLM Chat keeps a list of allowed sources, add this one.

2. **Accept picture content in `/chat`.** Each message's `content` can now be either a plain string (as today) or a list of Claude content blocks, exactly as the Claude Messages API takes them:

   ```json
   {"role": "user", "content": [
     {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "<base64>"}},
     {"type": "text", "text": "Explain this Football Manager screen."}
   ]}
   ```

   Pass these blocks through to Claude unchanged. Allowed media types: `image/jpeg` and `image/png`. Refuse a request with more than 2 images or more than 5 MB of image data in total, with `{"ok": false, "error": "picture too large"}`. FM26 Dashboard sends one JPEG of at most 1568 pixels on the long edge, usually 150 to 400 KB.

3. **Raise the body limit for `/chat`** to at least 8 MB (base64 makes the picture about a third bigger).

4. **Say so in `/ping`.** Add a features list to the ping reply so FM26 Dashboard knows it can send pictures:

   ```json
   {"ok": true, "features": ["images"]}
   ```

   FM26 Dashboard only sends pictures to LLM Chat when `images` is in that list. Without it, nothing breaks: pictures go to Claude directly with the backup key, and text still goes through LLM Chat.

5. **Keep it private.** Don't write the picture to LLM Chat's chat history, logs or disk. Keep only the text of the reply, if anything, labelled as coming from FM26 Dashboard. The link must keep answering only on 127.0.0.1.

6. **Model.** Use the `model` in the request if it's one LLM Chat allows, otherwise LLM Chat's default Claude model. The model must support pictures (Claude Sonnet, Opus and Haiku all do).

7. **Reply** exactly as today: `{"ok": true, "text": "...", "model": "..."}` or `{"ok": false, "error": "..."}`.

## Android (content provider `app.llmchat.bridge`)

Only one change is needed on the phone: accept `"source": "fm26-dashboard"` with the same key, for text requests. The phone app never sends pictures (the game only runs on the PC), so no picture support is needed in the content provider. Also add `"features": []` to the `ping` reply, so both sides look the same.

## How to check it worked

1. Open LLM Chat, then FM26 Dashboard on the same PC.
2. The AI pill at the top of the dashboard should say **LLM Chat** rather than **LLM Chat (text only)**.
3. Open Football Manager, press Ctrl+Shift+E, and the explanation should show **via LLM Chat**.
