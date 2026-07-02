#!/usr/bin/env python3
"""
linkedin_to_video.py

Turns a LinkedIn post (something you wrote or spoke) plus one photo into a
short vertical video for LinkedIn, using Claude to write the motion prompt and
Higgsfield to animate the photo.

WHY IT IS BUILT THIS WAY
  The post text comes from a local file or from paste, NOT a live LinkedIn pull.
  LinkedIn does not give reliable read access to your own posts at the normal
  API tier, and scraping breaks their terms. So you drop the text in, the script
  does the rest.

WHAT IT DOES
  1. Reads your post text (file or stdin)
  2. Asks Claude to distil it into: a caption + an image to video motion prompt
  3. Sends your photo to Higgsfield image to video with your render settings
  4. Waits until done, downloads the clip(s)
  5. Caps total length at 30s. If one clip is shorter, it generates several and
     stitches them with ffmpeg so you land near 30s.

SETUP
  pip install higgsfield-client anthropic requests
  export HF_KEY="your-higgsfield-key:your-higgsfield-secret"
  export ANTHROPIC_API_KEY="your-anthropic-key"
  (ffmpeg only needed if you stitch multiple clips: sudo apt install ffmpeg)

RUN
  python linkedin_to_video.py --post post.txt --photo headshot.jpg
  # a public image URL works too:
  python linkedin_to_video.py --post post.txt --photo https://example.com/me.jpg
  # or paste the post:
  python linkedin_to_video.py --photo headshot.jpg   (then paste, Ctrl-D to end)

A NOTE ON ORIENTATION
  Higgsfield's DoP image-to-video model keeps the orientation of the source
  photo. For a vertical LinkedIn-on-mobile video, feed it a vertical (9:16) photo.
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG  (the values you are most likely to change sit here)
# ----------------------------------------------------------------------------

# Claude model. The post -> brief step is a small extraction task, so a Sonnet
# tier model is plenty; swap to "claude-opus-4-8" if you want the highest
# quality captions. Update the string if the API ever rejects it.
CLAUDE_MODEL = "claude-sonnet-5"

# Higgsfield image to video model. This is the DoP ("Director of Photography")
# image-to-video endpoint. If your installed higgsfield-client version rejects
# this slug, check the exact string in the Higgsfield API docs
# (cloud.higgsfield.ai) or `python -c "import higgsfield_client; help(...)"`.
HIGGSFIELD_I2V_MODEL = "/v1/image2video/dop"

# Render settings. DoP produces short (~5s) clips whose orientation follows the
# source image; it does not take aspect_ratio / resolution / duration.
RENDER = {
    "dop_model": "dop-turbo",   # dop-lite (fast/cheap) | dop-turbo | dop-preview (best)
    "clip_seconds": 5,          # DoP clips are ~5s; used only to plan how many to make
}

MAX_TOTAL_SECONDS = 30          # hard ceiling on the finished video
OUTPUT_DIR = Path("output")


# ----------------------------------------------------------------------------
# STEP 1  read the post
# ----------------------------------------------------------------------------

def read_post(post_arg: str | None) -> str:
    if post_arg:
        text = Path(post_arg).read_text(encoding="utf-8").strip()
    else:
        print("Paste your LinkedIn post, then press Ctrl-D:\n")
        text = sys.stdin.read().strip()
    if not text:
        sys.exit("No post text found. Give me a file with --post or paste some text.")
    return text


# ----------------------------------------------------------------------------
# STEP 2  Claude turns the post into a caption + a motion prompt
# ----------------------------------------------------------------------------

# JSON Schema for the brief. Passing this via structured outputs guarantees the
# response is valid JSON matching this shape, so we don't have to strip code
# fences or hope Claude behaved.
BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "motion_prompt": {"type": "string"},
        "on_screen_text": {"type": "string"},
    },
    "required": ["caption", "motion_prompt", "on_screen_text"],
    "additionalProperties": False,
}


def build_brief(post_text: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    instruction = f"""You are turning a LinkedIn post into a short vertical video brief.
The video is one photo brought to life with subtle camera motion, under 30 seconds.

Fill in these fields:
  "caption"        a 1 to 2 line LinkedIn caption in the writer's own plain voice
  "motion_prompt"  a short image to video prompt describing camera and subject
                   motion only (slow push in, gentle parallax, etc). No new
                   objects, no text overlays. Keep it grounded and professional.
  "on_screen_text" one short line of text to overlay, 6 words max, or "" for none

The post:
---
{post_text}
---"""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": BRIEF_SCHEMA}},
        messages=[{"role": "user", "content": instruction}],
    )

    # With structured outputs the first text block is valid JSON.
    raw = "".join(block.text for block in msg.content if block.type == "text").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"Claude did not return clean JSON. Got:\n{raw}")


# ----------------------------------------------------------------------------
# STEP 3  Higgsfield animates the photo
# ----------------------------------------------------------------------------

def resolve_image(photo: str) -> str:
    """Higgsfield needs the image reachable by URL. If you passed an http(s)
    URL, use it directly. If you passed a local file, upload it via the SDK and
    use the returned URL."""
    if photo.startswith("http://") or photo.startswith("https://"):
        return photo

    p = Path(photo)
    if not p.exists():
        sys.exit(f"Photo not found: {photo}")

    import higgsfield_client as hf

    print(f"  uploading {p.name} to Higgsfield...")
    uploaded = hf.upload_file(str(p))
    # upload_file may return a bare URL string or an object/dict carrying one.
    if isinstance(uploaded, str):
        return uploaded
    if isinstance(uploaded, dict) and uploaded.get("url"):
        return uploaded["url"]
    url = getattr(uploaded, "url", None)
    if url:
        return url
    sys.exit(f"Could not read an image URL from the upload response:\n{uploaded}")


def _extract_video_url(result) -> str | None:
    """Response shape varies by SDK version and model. Handle the common cases."""
    if not isinstance(result, dict):
        return None

    # DoP / job-based shape: {"jobs": [{"results": {"raw": {"url": ...}}}]}
    jobs = result.get("jobs")
    if jobs:
        raw = (jobs[0].get("results") or {}).get("raw") or {}
        if raw.get("url"):
            return raw["url"]

    # Other common shapes.
    if result.get("videos"):
        return result["videos"][0].get("url")
    if isinstance(result.get("video"), dict) and result["video"].get("url"):
        return result["video"]["url"]
    out = result.get("output") or {}
    if isinstance(out, dict) and out.get("media_url"):
        media = out["media_url"]
        return media[0] if isinstance(media, list) else media
    return None


def generate_clip(image_url: str, motion_prompt: str, index: int) -> Path:
    import higgsfield_client as hf

    print(f"  submitting clip {index + 1} to Higgsfield...")
    result = hf.subscribe(
        HIGGSFIELD_I2V_MODEL,
        arguments={
            "model": RENDER["dop_model"],
            "prompt": motion_prompt,
            "input_images": [{"type": "image_url", "image_url": image_url}],
        },
    )

    video_url = _extract_video_url(result)
    if not video_url:
        sys.exit(f"Could not find a video URL in the Higgsfield response:\n{result}")

    OUTPUT_DIR.mkdir(exist_ok=True)
    clip_path = OUTPUT_DIR / f"clip_{index:02d}.mp4"
    clip_path.write_bytes(requests.get(video_url, timeout=120).content)
    print(f"  saved {clip_path}")
    return clip_path


# ----------------------------------------------------------------------------
# STEP 4  cap and stitch to land near 30 seconds
# ----------------------------------------------------------------------------

def clips_needed() -> int:
    n = MAX_TOTAL_SECONDS // RENDER["clip_seconds"]
    return max(1, int(n))


def stitch(clips: list[Path]) -> Path:
    final = OUTPUT_DIR / "linkedin_video.mp4"
    if len(clips) == 1:
        clips[0].rename(final)
        return final

    listfile = OUTPUT_DIR / "concat.txt"
    listfile.write_text("".join(f"file '{c.name}'\n" for c in clips))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", str(final)],
        cwd=OUTPUT_DIR, check=True,
    )
    return final


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="LinkedIn post + photo -> short video")
    ap.add_argument("--post", help="Path to a .txt or .md file with your post")
    ap.add_argument("--photo", required=True,
                    help="Local image path or public image URL of your photo")
    args = ap.parse_args()

    for var in ("HF_KEY", "ANTHROPIC_API_KEY"):
        if not os.getenv(var):
            sys.exit(f"Missing env var {var}. See SETUP at the top of this file.")

    post_text = read_post(args.post)
    print("\nAsking Claude to write the brief...")
    brief = build_brief(post_text)
    print(f"\nCaption:\n  {brief['caption']}")
    print(f"Motion prompt:\n  {brief['motion_prompt']}")
    print(f"On screen text:\n  {brief.get('on_screen_text') or '(none)'}\n")

    image_url = resolve_image(args.photo)
    n = clips_needed()
    print(f"Generating {n} clip(s) of ~{RENDER['clip_seconds']}s each "
          f"(target {min(n * RENDER['clip_seconds'], MAX_TOTAL_SECONDS)}s total)\n")

    clips = [generate_clip(image_url, brief["motion_prompt"], i) for i in range(n)]
    final = stitch(clips)

    print(f"\nDone. Final video: {final}")
    print("Caption to post with it:")
    print(f"\n{brief['caption']}\n")


if __name__ == "__main__":
    main()
