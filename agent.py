#!/usr/bin/env python3
"""Mac agent: polls n8n for approved jobs, builds the free Gemini video, sends it to Telegram.

  export GEMINI_API_KEY=... TELEGRAM_BOT_TOKEN=... AGENT_KEY=...   (AGENT_KEY is in .agent_key)
  python3 agent.py                 # poll n8n every 15 s (keep this Terminal open during the demo)
  python3 agent.py --file job.json --no-send    # test without n8n / Telegram
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from gemini_video import build_video
from veo import DEFAULT_CHAT, send_telegram

N8N = os.environ.get("N8N_BASE", "https://n8n.sntgr.com")
HERE = os.path.dirname(os.path.abspath(__file__))


def agent_key():
    k = os.environ.get("AGENT_KEY")
    if not k and os.path.exists(os.path.join(HERE, ".agent_key")):
        k = open(os.path.join(HERE, ".agent_key")).read().strip()
    return k or sys.exit("AGENT_KEY missing (see .agent_key)")


def poll():
    req = urllib.request.Request(f"{N8N}/webhook/creatical-jobs", headers={"X-Agent-Key": agent_key()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("job")


def tg_message(token, text):
    data = urllib.parse.urlencode({"chat_id": os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT), "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30).read()


DRIVE_ROOT = os.path.expanduser("~/Library/CloudStorage/GoogleDrive-aminisouissi@gmail.com/My Drive")


def drive_dir():
    """Folder inside Google Drive for desktop (override with DRIVE_DIR). None if Drive is not available."""
    base = os.environ.get("DRIVE_DIR") or os.path.join(DRIVE_ROOT, "Creatical Videos")
    try:
        os.makedirs(base, exist_ok=True)
        return base if os.access(base, os.W_OK) else None
    except OSError:
        return None


def slug(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(s).lower().replace("é", "e").replace("è", "e").replace("à", "a").replace("ê", "e")).strip("-")
    return s[:60] or "video"


NETLIFY_API = "https://api.netlify.com/api/v1"


def _netlify(method, path, token, data=None, ctype="application/json"):
    req = urllib.request.Request(NETLIFY_API + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        return json.loads(body) if body else {}


def publish_demo(video_path, job_id):
    """Add /demos/<id>.mp4 to the live Netlify site without touching the other files (file-digest deploy)."""
    token = os.environ["NETLIFY_TOKEN"]
    site = os.environ.get("NETLIFY_SITE", "gleeful-brioche-7763f3.netlify.app")
    if not re.fullmatch(r"d[a-z0-9]{6,24}", str(job_id)):
        raise ValueError("bad job id")
    data = open(video_path, "rb").read()
    sha = hashlib.sha1(data).hexdigest()
    path = f"/demos/{job_id}.mp4"
    files = {f["path"]: f["sha"] for f in _netlify("GET", f"/sites/{site}/files", token) if f.get("path") and f.get("sha")}
    files[path] = sha
    dep = _netlify("POST", f"/sites/{site}/deploys", token, json.dumps({"files": files}).encode())
    if sha in (dep.get("required") or []):
        _netlify("PUT", f"/deploys/{dep['id']}/files{path}", token, data, "application/octet-stream")
    print(f"    Netlify: https://{site}{path}")


def handle(job, send=True):
    item, client = job["item"], job["client"]
    print(f"-> job: {client} / {item.get('title')}")
    work = tempfile.mkdtemp(prefix="creatical-")  # nothing stays on the Mac: everything is built here and removed at the end
    try:
        out, summary = build_video(client, item, work, os.environ.get("GEMINI_API_KEY"),
                                   offline=not os.environ.get("GEMINI_API_KEY"), tag=job.get("id", "job"))
        print("   ", summary)

        saved = None
        dest = None if os.environ.get("NO_DRIVE") else drive_dir()
        keep = os.environ.get("KEEP_DIR")
        if keep:
            os.makedirs(keep, exist_ok=True)
            shutil.copyfile(out, os.path.join(keep, f"{datetime.date.today()}_{slug(client)}_{slug(item.get('title'))}.mp4"))
        if dest:
            name = f"{datetime.date.today()}_{slug(client)}_{slug(item.get('title'))}"
            saved = os.path.join(dest, name + ".mp4")
            shutil.copyfile(out, saved)
            for credits in (f for f in os.listdir(work) if f.startswith("credits-")):
                shutil.copyfile(os.path.join(work, credits), os.path.join(dest, name + "_credits.txt"))
            print("    Google Drive:", saved)
        elif os.environ.get("NO_DRIVE"):
            pass
        else:
            print("    ! Google Drive folder unavailable, keeping a local copy in ./out")
            os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
            saved = shutil.copyfile(out, os.path.join(HERE, "out", os.path.basename(out)))

        if job.get("delivery") == "web" and os.environ.get("NETLIFY_TOKEN"):
            publish_demo(out, job.get("id"))

        if send:
            token = os.environ.get("TELEGRAM_BOT_TOKEN") or sys.exit("TELEGRAM_BOT_TOKEN missing")
            where = ("☁️ Enregistrée dans Google Drive > Creatical Videos" if dest
                     else "☁️ Générée dans le cloud (GitHub Actions)" if os.environ.get("NO_DRIVE") else "(copie locale, Drive indisponible)")
            r = send_telegram(token, os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT), out,
                              f"🎬 {item.get('title')}\n{where}" + ("\n(démo lancée depuis la page publique)" if job.get("delivery") == "web" else ""))
            print("    Telegram:", "sent" if r.get("ok") else r)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="run one job from a local JSON file instead of polling n8n")
    ap.add_argument("--no-send", action="store_true")
    ap.add_argument("--every", type=int, default=15)
    a = ap.parse_args()
    if a.file:
        return handle(json.load(open(a.file)), send=not a.no_send)
    print(f"Agent running: polling {N8N} every {a.every}s. Ctrl+C to stop.")
    while True:
        try:
            job = poll()
            if job:
                try:
                    handle(job)
                except Exception as e:  # tell the user instead of dying silently
                    print("   ! job failed:", e)
                    if os.environ.get("TELEGRAM_BOT_TOKEN"):
                        tg_message(os.environ["TELEGRAM_BOT_TOKEN"], "⚠️ La vidéo n'a pas pu être générée sur le Mac : " + str(e)[:200])
        except Exception as e:
            print("poll error:", str(e)[:150])
        time.sleep(a.every)


if __name__ == "__main__":
    main()
