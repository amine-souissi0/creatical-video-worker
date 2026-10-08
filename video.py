#!/usr/bin/env python3
"""Turn one script from a generated plan (plan.html) into a vertical 1080x1920 video:
macOS TTS voice-over + animated captions, assembled with ffmpeg.

Usage: python3 video.py [plan.html] [idea_number=1] [--voice Jacques]
"""
import html
import os
import re
import subprocess
import sys
import tempfile

W, H = 1080, 1920
def _pick(*paths):
    return next((p for p in paths if os.path.exists(p)), paths[0])


FONT = _pick("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_LIGHT = _pick("/System/Library/Fonts/Supplemental/Arial.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
ACCENT = (232, 67, 45)


def parse_plan(path):
    s = open(path, encoding="utf-8").read()
    client = html.unescape(re.search(r"<h1>Plan de contenu — (.*?)</h1>", s).group(1))
    items = []
    for sec in s.split('<section class="card">')[1:]:
        g = lambda p: html.unescape(re.search(p, sec, re.S).group(1)).strip()
        script, cta = g(r"<pre>(.*?)\n\nCTA : .*?</pre>"), g(r"CTA : (.*?)</pre>")
        items.append({
            "pillar": g(r'<span class="pill">(.*?)</span>'),
            "title": g(r"<h2>\d+\. (.*?)</h2>"),
            "hook": g(r'<p class="hook">« (.*?) »</p>'),
            "script": script,
            "cta": cta,
        })
    return client, items


def narration(item):
    text = re.sub(r"\[VISUEL[^\]]*\]", " ", item["script"])
    return re.sub(r"\s+", " ", text).strip() + " " + item["cta"]


def chunks(text, max_words=6):
    out = []
    for sent in re.split(r"(?<=[.!?:])\s+", text):
        words = sent.split()
        for i in range(0, len(words), max_words):
            out.append(" ".join(words[i:i + max_words]))
    # merge tiny tails into the previous chunk
    merged = []
    for c in out:
        if merged and len(c.split()) <= 2:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def frame(client, item, caption, progress, path):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):  # vertical gradient
        t = y / H
        c = (int(18 + 40 * t), int(18 + 8 * t), int(28 + 30 * t))
        for x in range(W):
            px[x, y] = c
    d = ImageDraw.Draw(img)
    f_pill, f_title = ImageFont.truetype(FONT, 38), ImageFont.truetype(FONT, 54)
    f_cap, f_small = ImageFont.truetype(FONT, 96), ImageFont.truetype(FONT_LIGHT, 40)

    pw = d.textlength(item["pillar"].upper(), font=f_pill) + 64
    d.rounded_rectangle((70, 150, 70 + pw, 226), radius=38, fill=ACCENT)
    d.text((102, 164), item["pillar"].upper(), font=f_pill, fill="white")

    y = 290
    for line in wrap(d, item["title"], f_title, W - 140):
        d.text((70, y), line, font=f_title, fill=(235, 235, 235))
        y += 70

    lines = wrap(d, caption, f_cap, W - 140)
    block = len(lines) * 124
    y = (H - block) // 2 + 40
    for line in lines:
        x = (W - d.textlength(line, font=f_cap)) // 2
        d.text((x + 4, y + 4), line, font=f_cap, fill=(0, 0, 0))
        d.text((x, y), line, font=f_cap, fill="white")
        y += 124

    d.text((70, H - 230), client, font=f_small, fill=(200, 200, 200))
    d.rectangle((70, H - 160, W - 70, H - 148), fill=(70, 70, 80))
    d.rectangle((70, H - 160, 70 + int((W - 140) * progress), H - 148), fill=ACCENT)
    img.save(path)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    voice = "Jacques"
    if "--voice" in sys.argv:
        voice = sys.argv[sys.argv.index("--voice") + 1]
        args = [a for a in args if a != voice]
    plan = args[0] if args else "plan.html"
    idx = int(args[1]) if len(args) > 1 else 1
    client, items = parse_plan(plan)
    item = items[idx - 1]
    text = narration(item)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(plan)), "out")
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp()

    aiff, wav = os.path.join(tmp, "v.aiff"), os.path.join(tmp, "v.wav")
    subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, wav], check=True)
    dur = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", wav]).decode())

    caps = chunks(text)
    total = sum(len(c) for c in caps)
    lst, acc = [], 0
    for i, c in enumerate(caps):
        p = os.path.join(tmp, f"f{i:03d}.png")
        d = dur * len(c) / total
        frame(client, item, c, (acc + d / 2) / dur, p)
        lst.append(f"file '{p}'\nduration {d:.3f}")
        acc += d
    lst.append(f"file '{os.path.join(tmp, f'f{len(caps) - 1:03d}.png')}'")
    listfile = os.path.join(tmp, "list.txt")
    open(listfile, "w").write("\n".join(lst))

    out = os.path.join(out_dir, f"video-{idx}.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listfile,
                    "-i", wav, "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-c:a", "aac",
                    "-shortest", "-movflags", "+faststart", out], check=True)
    print(f"{out}  ({dur:.0f}s, {len(caps)} captions, voice {voice})")


if __name__ == "__main__":
    main()
