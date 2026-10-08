#!/usr/bin/env python3
"""Generate a real AI video (Google Veo 3.1, with native audio) from one idea of a plan.html,
then optionally send it to Telegram.

Needs a Gemini API key with billing enabled (Veo has no free tier; a Google AI Pro/One
subscription does NOT cover API usage).

  export GEMINI_API_KEY=...                       # your key
  export TELEGRAM_BOT_TOKEN=...                   # optional: to send the result to Telegram
  python3 veo.py plan.html 1 --dry-run            # prints the prompt + estimated cost, calls nothing
  python3 veo.py plan.html 1                      # Veo 3.1 Lite, 720p, 8 s, 9:16 (cheapest)
  python3 veo.py plan.html 1 --model fast --res 1080p
  python3 veo.py plan.html 1 --model standard --res 4k --telegram
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

from video import parse_plan

API = "https://generativelanguage.googleapis.com/v1beta"
MODELS = {"lite": "veo-3.1-lite-generate-preview", "fast": "veo-3.1-fast-generate-preview", "standard": "veo-3.1-generate-preview"}
# USD per second (public list prices, check the current pricing page before relying on them)
PRICE = {("lite", "720p"): 0.05, ("lite", "1080p"): 0.08, ("fast", "720p"): 0.10, ("fast", "1080p"): 0.12,
         ("fast", "4k"): 0.30, ("standard", "720p"): 0.40, ("standard", "1080p"): 0.40, ("standard", "4k"): 0.60}
DEFAULT_CHAT = ""


def build_prompt(client, item):
    cues = re.findall(r"\[VISUEL\s*:?\s*([^\]]+)\]", item["script"])
    scene = "; ".join(c.strip() for c in cues[:2]) or "an elegant modern office with a view over Brussels"
    return (
        f"Vertical 9:16 social-media video for the brand \"{client}\". Subject: {item['title']}. "
        f"A confident French-speaking Belgian business owner in his 40s, smart-casual, speaks directly to the camera "
        f"in a bright upscale interior. Shot on a smartphone gimbal, natural warm light, shallow depth of field, "
        f"subtle push-in. Visual idea: {scene}. "
        f"He says in French, with a natural voice: \"{item['hook']}\". "
        f"Clean, professional, authentic, no on-screen text, no logos."
    )


def call(url, key, body=None):
    req = urllib.request.Request(url, headers={"x-goog-api-key": key, "content-type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {url.split('?')[0]}\n{e.read().decode(errors='replace')[:1500]}")


def send_telegram(token, chat_id, path, caption):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption[:1000]), ("supports_streaming", "true")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="{os.path.basename(path)}"\r\n'
                  f'Content-Type: video/mp4\r\n\r\n').encode() + open(path, "rb").read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendVideo", data=b"".join(parts),
                                 headers={"content-type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", nargs="?", default="plan.html")
    ap.add_argument("idea", nargs="?", type=int, default=1)
    ap.add_argument("--model", choices=MODELS, default="lite")
    ap.add_argument("--res", choices=["720p", "1080p", "4k"], default="720p")
    ap.add_argument("--seconds", choices=["4", "6", "8"], default="8")
    ap.add_argument("--telegram", action="store_true", help="send the result to Telegram (TELEGRAM_BOT_TOKEN)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if (a.model, a.res) not in PRICE:
        sys.exit(f"{a.model} does not support {a.res}")
    if a.res != "720p" and a.seconds != "8":
        a.seconds = "8"  # 1080p/4k require 8 s

    client, items = parse_plan(a.plan)
    item = items[a.idea - 1]
    prompt = build_prompt(client, item)
    cost = PRICE[(a.model, a.res)] * int(a.seconds)
    print(f"Idea {a.idea}: {item['title']}\nModel {MODELS[a.model]} · {a.res} · {a.seconds}s · 9:16 · ~${cost:.2f}\n\nPROMPT:\n{prompt}\n")
    if a.dry_run:
        return
    key = os.environ.get("GEMINI_API_KEY") or sys.exit("Set GEMINI_API_KEY")

    op = call(f"{API}/models/{MODELS[a.model]}:predictLongRunning", key, {
        "instances": [{"prompt": prompt}],
        "parameters": {"aspectRatio": "9:16", "resolution": a.res, "durationSeconds": int(a.seconds)},
    })
    print("Operation:", op["name"])
    while not op.get("done"):
        time.sleep(10)
        op = call(f"{API}/{op['name']}", key)
        print(" ...generating")
    if "error" in op:
        sys.exit(json.dumps(op["error"], indent=2))
    uri = op["response"]["generateVideoResponse"]["generatedSamples"][0]["video"]["uri"]

    out_dir = os.path.join(os.path.dirname(os.path.abspath(a.plan)), "out")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"veo-{a.idea}-{a.model}-{a.res}.mp4")
    req = urllib.request.Request(uri, headers={"x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=300) as r, open(out, "wb") as f:
        f.write(r.read())
    print("Saved", out)

    if a.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("Set TELEGRAM_BOT_TOKEN")
        res = send_telegram(token, os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT), out,
                            f"🎬 {item['title']}\n(Veo 3.1, généré automatiquement)")
        print("Telegram:", "sent" if res.get("ok") else res)


if __name__ == "__main__":
    main()
