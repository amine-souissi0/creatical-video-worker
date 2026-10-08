#!/usr/bin/env python3
"""Free, fully automatic vertical video from one idea of plan.html, using Gemini only:
  - Gemini image model  -> one background image per scene
  - Gemini TTS          -> voice-over
  - Pillow + ffmpeg     -> animated captions, 1080x1920 mp4
Every Gemini step falls back (macOS `say`, gradient background) if the free quota refuses it.

  export GEMINI_API_KEY=...
  python3 gemini_video.py plan.html 1            # uses Gemini image + TTS (free tier)
  python3 gemini_video.py plan.html 1 --offline  # no Gemini call at all (say + gradients)
  python3 gemini_video.py plan.html 1 --telegram # also sends it to Telegram (TELEGRAM_BOT_TOKEN)
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from video import parse_plan, narration, chunks, wrap, W, H, FONT, FONT_LIGHT, ACCENT

API = "https://generativelanguage.googleapis.com/v1beta/models/"
IMAGE_MODEL = ["gemini-3.1-flash-image", "gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]
TTS_MODEL = ["gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview"]
VOICE = "Kore"


def gemini(model, key, body):
    """`model` may be a list: models are tried in order until one answers."""
    last = None
    for m in (model if isinstance(model, list) else [model]):
        req = urllib.request.Request(f"{API}{m}:generateContent", data=json.dumps(body).encode(),
                                     headers={"x-goog-api-key": key, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"{m} HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    raise last


def inline_data(resp):
    for part in resp["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return base64.b64decode(part["inlineData"]["data"])
    raise RuntimeError("no inline data in response")


def make_voice(text, key, wav, offline):
    if key and not offline:
        try:
            resp = gemini(TTS_MODEL, key, {
                "contents": [{"parts": [{"text": "Dis ceci en français, d'une voix posée, chaleureuse et dynamique : " + text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}},
            })
            pcm = inline_data(resp)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-", wav],
                           input=pcm, check=True)
            return "Gemini TTS"
        except Exception as e:  # quota, model name, network...
            print("  ! Gemini TTS unavailable, using macOS voice:", str(e)[:160])
    if shutil.which("say"):
        aiff = wav + ".aiff"
        subprocess.run(["say", "-v", "Jacques", "-o", aiff, text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, wav], check=True)
        return "macOS voice"
    subprocess.run(["espeak-ng", "-v", "fr", "-s", "150", "-w", wav, text], check=True)
    return "espeak voice"


def scene_prompts(client, item, n):
    cues = [c.strip() for c in re.findall(r"\[VISUEL\s*:?\s*([^\]]+)\]", item["script"])]
    base = "photorealistic vertical 9:16 photo, bright natural light, upscale, no text, no logos, no watermark. "
    fallback = ["elegant Haussmann-style apartment interior in Brussels", "luxury living room with large windows",
                "aerial view of Brussels rooftops at golden hour", "handshake between a smiling agent and a happy couple",
                "modern kitchen in a high-end home"]
    topics = cues + fallback
    return [base + f"Scene for a real-estate video titled '{item['title']}': {topics[i % len(topics)]}." for i in range(n)]


def make_image(prompt, key, offline):
    if key and not offline:
        try:
            resp = gemini(IMAGE_MODEL, key, {"contents": [{"parts": [{"text": prompt}]}],
                                             "generationConfig": {"responseModalities": ["IMAGE"]}})
            return Image.open(io.BytesIO(inline_data(resp))).convert("RGB")
        except Exception as e:
            print("  ! Gemini image unavailable, using gradient:", str(e)[:160])
    return None


STOCK_QUERIES = ["Brussels Art Nouveau house", "Brussels Grand Place", "Brussels rooftops city view",
                 "Brussels Ixelles street architecture", "Brussels Cinquantenaire park"]
UA = {"User-Agent": "creatical-demo/1.0 (portfolio demo script)"}


def stock_image(query, cache_dir, credits):
    """Free fallback: a CC-licensed photo from Wikimedia Commons (no API key). Credits are collected."""
    try:
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "generator": "search", "gsrsearch": query + " filetype:bitmap", "gsrnamespace": 6,
            "gsrlimit": 30, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1500, "format": "json"})
        pages = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))["query"]["pages"].values()
        for p in sorted(pages, key=lambda p: p["index"]):
            ii = p["imageinfo"][0]
            ratio = ii["width"] / ii["height"]
            lic = ii["extmetadata"].get("LicenseShortName", {}).get("value", "")
            if ii["mime"] != "image/jpeg" or ii["width"] < 1500 or not 0.6 < ratio < 1.8 or not lic.startswith(("CC", "Public")):
                continue
            path = os.path.join(cache_dir, re.sub(r"\W+", "_", p["title"])[:80] + ".jpg")
            if not os.path.exists(path):
                with urllib.request.urlopen(urllib.request.Request(ii["thumburl"], headers=UA), timeout=60) as r, open(path, "wb") as f:
                    f.write(r.read())
            artist = re.sub(r"<[^>]+>", "", ii["extmetadata"].get("Artist", {}).get("value", "")).strip()
            credits.append(f"{p['title']} — {artist} — {lic} — {ii['descriptionurl']}")
            return Image.open(path).convert("RGB")
    except Exception as e:
        print("  ! stock photo unavailable, using gradient:", str(e)[:120])
    return None


def cover(img):
    r = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * r) + 1, int(img.height * r) + 1), Image.LANCZOS)
    x, y = (img.width - W) // 2, (img.height - H) // 2
    return img.crop((x, y, x + W, y + H))


def gradient():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(18 + 40 * t), int(18 + 8 * t), int(28 + 30 * t)))
    return img


def render(bg, client, item, caption, progress, path):
    img = ImageEnhance.Brightness(cover(bg)).enhance(0.55) if bg is not None else gradient()
    d = ImageDraw.Draw(img)
    f_pill, f_title = ImageFont.truetype(FONT, 38), ImageFont.truetype(FONT, 54)
    f_cap, f_small = ImageFont.truetype(FONT, 96), ImageFont.truetype(FONT_LIGHT, 40)
    pw = d.textlength(item["pillar"].upper(), font=f_pill) + 64
    d.rounded_rectangle((70, 150, 70 + pw, 226), radius=38, fill=ACCENT)
    d.text((102, 164), item["pillar"].upper(), font=f_pill, fill="white")
    y = 290
    for line in wrap(d, item["title"], f_title, W - 140):
        d.text((70 + 3, y + 3), line, font=f_title, fill=(0, 0, 0))
        d.text((70, y), line, font=f_title, fill=(240, 240, 240))
        y += 70
    lines = wrap(d, caption, f_cap, W - 140)
    y = (H - len(lines) * 124) // 2 + 60
    for line in lines:
        x = (W - d.textlength(line, font=f_cap)) // 2
        d.text((x + 4, y + 4), line, font=f_cap, fill=(0, 0, 0))
        d.text((x, y), line, font=f_cap, fill="white")
        y += 124
    d.text((70, H - 230), client, font=f_small, fill=(215, 215, 215))
    d.rectangle((70, H - 160, W - 70, H - 148), fill=(70, 70, 80))
    d.rectangle((70, H - 160, 70 + int((W - 140) * progress), H - 148), fill=ACCENT)
    img.save(path)


def build_video(client, item, out_dir, key, offline=False, no_stock=False, tag=1):
    """Voice (Gemini TTS) + background photos + animated captions -> mp4. Returns (path, summary)."""
    text = narration(item)
    tmp = tempfile.mkdtemp()
    os.makedirs(out_dir, exist_ok=True)

    wav = os.path.join(tmp, "voice.wav")
    voice = make_voice(text, key, wav, offline)
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav]).decode())

    caps = chunks(text)
    n_scenes = min(5, max(1, len(caps) // 4))
    prompts = scene_prompts(client, item, n_scenes)
    imgs, credits = [], []
    cache = os.path.join(out_dir, "bg")
    os.makedirs(cache, exist_ok=True)
    for i, p in enumerate(prompts):
        print(f"  scene {i + 1}/{n_scenes}: image...")
        img = make_image(p, key, offline)
        if img is None and not no_stock:
            img = stock_image(STOCK_QUERIES[i % len(STOCK_QUERIES)], cache, credits)
        imgs.append(img)
    if credits:
        open(os.path.join(out_dir, f"credits-{tag}.txt"), "w").write("Photos (Wikimedia Commons):\n" + "\n".join(credits) + "\n")

    total = sum(len(c) for c in caps)
    lst, acc = [], 0.0
    for i, c in enumerate(caps):
        d = dur * len(c) / total
        scene = min(n_scenes - 1, int(i * n_scenes / len(caps)))
        p = os.path.join(tmp, f"f{i:03d}.png")
        render(imgs[scene], client, item, c, (acc + d / 2) / dur, p)
        lst.append(f"file '{p}'\nduration {d:.3f}")
        acc += d
    lst.append(f"file '{os.path.join(tmp, f'f{len(caps) - 1:03d}.png')}'")
    listfile = os.path.join(tmp, "list.txt")
    open(listfile, "w").write("\n".join(lst))

    out = os.path.join(out_dir, f"gemini-video-{tag}.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listfile, "-i", wav,
                    "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-c:a", "aac", "-shortest", "-movflags", "+faststart", out],
                   check=True)
    got = sum(i is not None for i in imgs)
    summary = f"{out}  ({dur:.0f}s · voice: {voice} · backgrounds: {got}/{n_scenes} photos, {len(credits)} stock)"
    return out, summary



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", nargs="?", default="plan.html")
    ap.add_argument("idea", nargs="?", type=int, default=1)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--no-stock", action="store_true", help="skip free stock photos (gradient fallback)")
    a = ap.parse_args()
    key = os.environ.get("GEMINI_API_KEY")
    if not key and not a.offline:
        print("GEMINI_API_KEY not set: running offline")
        a.offline = True

    client, items = parse_plan(a.plan)
    item = items[a.idea - 1]
    out_dir = os.path.join(os.path.dirname(os.path.abspath(a.plan)), "out")
    out, summary = build_video(client, item, out_dir, key, a.offline, a.no_stock, a.idea)
    print(summary)

    if a.telegram:
        from veo import send_telegram, DEFAULT_CHAT
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("Set TELEGRAM_BOT_TOKEN")
        r = send_telegram(token, os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT), out, f"🎬 {item['title']}\n(généré automatiquement avec Gemini)")
        print("Telegram:", "sent" if r.get("ok") else r)


if __name__ == "__main__":
    main()
