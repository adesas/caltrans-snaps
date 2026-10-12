#!/usr/bin/env python3
"""Measure end-to-end video latency per camera (scene time -> our arrival).

Method
------
Caltrans still-image endpoints (cwwp2.dot.ca.gov) burn the camera-side NTP
wall clock into each snapshot: that is authoritative *scene time*. The video
stream carries no clock. Both come from the same physical camera, so we:

  1. poll the still endpoint until a NEW image appears -> scene time t_s (OCR)
  2. continuously download the live HLS and extract timestamped frames
  3. find the video frame whose picture best matches the still (MAD on tiny
     grayscale thumbnails; moving vehicles make the dip sharp)
  4. latency L = arrival_time(matched frame) - t_s

Meaning for the archive: a recording file written at wall-clock T shows the
world as of roughly T - L. To export real-world interval [t1, t2], read from
files at [t1 + L, t2 + L].

Usage: python3 scripts/measure_latency.py [--cams id1,id2] [--minutes 2]
Writes data/latency.json (merged) and prints a table.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CAMERAS_JSON = ROOT / "web" / "cameras.json"
OUT = ROOT / "data" / "latency.json"
TZ = ZoneInfo("America/Los_Angeles")
UA = {"User-Agent": "caltrans-snaps-latency/0.1"}
THUMB = (176, 120)
CLOCK_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})\s*(P[SD])T", re.I)


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_cams(which: str | None):
    data = json.loads(CAMERAS_JSON.read_text())
    cams = [c for g in data["groups"] for c in g["cameras"]]
    if which:
        ids = set(which.split(","))
        cams = [c for c in cams if c["id"] in ids]
    return cams


def ocr_clock(img_path: Path) -> float | None:
    """Return epoch seconds of the burned-in clock (today, LA tz) or None."""
    import io
    im = Image.open(img_path)
    w, h = im.size
    band = im.crop((0, int(h * 0.78), w, h)).convert("L")
    band = band.resize((band.width * 4, band.height * 4), Image.LANCZOS)
    # white-on-dark -> dark-on-white hard threshold for tesseract
    thr = band.point(lambda p: 255 if p < 110 else 0)
    band_path = Path("/tmp/lat/_band.png")
    thr.save(band_path)
    out = subprocess.run(
        ["tesseract", str(band_path), "-", "--psm", "6"],
        capture_output=True, text=True).stdout
    m = CLOCK_RE.search(out)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    now = dt.datetime.now(TZ)
    t = now.replace(hour=h, minute=mi, second=s, microsecond=0)
    # handle midnight wrap: if OCR time is >12h ahead of now, subtract a day
    if (t - now).total_seconds() > 12 * 3600:
        t -= dt.timedelta(days=1)
    return t.timestamp()


def tiny_gray(img: Image.Image) -> np.ndarray:
    return np.asarray(
        img.convert("L").resize(THUMB, Image.BILINEAR), dtype=np.float32)


def grab_live_frames(stream_url: str, stop: threading.Event, frames: list):
    """Download latest origin segment repeatedly; append (arrival, ndarray)."""
    base = stream_url.rsplit("/", 1)[0]          # .../NAME.stream
    seen: set = set()
    while not stop.is_set():
        try:
            top = http_get(f"{base}/chunklist.m3u8").decode()
            m = re.search(r"chunklist_w\d+\.m3u8", top)
            if not m:
                time.sleep(2); continue
            listing = http_get(f"{base}/{m.group(0)}").decode()
            segs = [l for l in listing.splitlines() if l.endswith(".ts")]
            if not segs:
                time.sleep(2); continue
            seg = segs[-1]
            if seg in seen:
                time.sleep(1.0); continue
            seen.add(seg)
            raw = http_get(f"{base}/{seg}", timeout=30)
            t_done = time.time()
            # extract frames via ffmpeg pipe -> one PNG per frame in /tmp
            p = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", "pipe:0",
                 "-vf", "fps=4", "-f", "image2pipe", "-vcodec", "bmp", "-"],
                input=raw, capture_output=True)
            if not p.stdout:
                time.sleep(2); continue
            # split BMP stream (each frame is a separate object; parse headers)
            off = 0
            imgs = []
            while True:
                i = p.stdout.find(b"BM", off)
                if i < 0 or i + 54 > len(p.stdout):
                    break
                size = int.from_bytes(p.stdout[i + 2:i + 6], "little")
                blob = p.stdout[i:i + max(size, 54)]
                try:
                    imgs.append(Image.open(__import__("io").BytesIO(blob)))
                except Exception:
                    pass
                off = i + max(size, 3)
            n = len(imgs)
            for k, im in enumerate(imgs):
                # last frame arrived ~now; earlier frames proportionally before
                arr = t_done - (n - 1 - k) * 0.25
                frames.append((arr, tiny_gray(im)))
        except Exception:
            pass
        time.sleep(1.0)


def measure_cam(cam) -> dict | None:
    import io
    img_url = cam.get("image_url") or ""
    stream = cam["stream_url"]
    frames: list = []
    stop = threading.Event()
    th = threading.Thread(target=grab_live_frames, args=(stream, stop, frames))
    th.start()
    try:
        # 1) wait for a NEW still (md5 change) so we know it was just generated
        prev = None
        snap_bytes = t_fetch_snap = t_s = None
        for attempt in range(4):
            deadline = time.time() + 200
            while time.time() < deadline:
                try:
                    b = http_get(img_url, timeout=15)
                except Exception:
                    time.sleep(3); continue
                if prev is not None and b != prev:
                    snap_bytes = b
                    break
                prev = b
                time.sleep(4)
            if snap_bytes is None:
                continue
            t_fetch_snap = time.time()
            Path("/tmp/lat/_snap.jpg").write_bytes(snap_bytes)
            t_s = ocr_clock(Path("/tmp/lat/_snap.jpg"))
            if t_s is not None:
                break
            print(f"  OCR retry {attempt + 1}", flush=True)
        if snap_bytes is None or t_s is None:
            return {"id": cam["id"], "error": "no fresh still / OCR failed"}
        # 2) keep grabbing video for a window after the snapshot moment
        time.sleep(100)
    finally:
        stop.set()
        th.join(timeout=15)
    if not frames:
        return {"id": cam["id"], "error": "no video frames captured"}
    # crop the clock bar off the still (scene area is 4:3 above it)
    im = Image.open(io.BytesIO(snap_bytes))
    w, h = im.size
    scene_h = min(h, int(w * 4 / 3))
    snap = tiny_gray(im.crop((0, 0, w, scene_h)))
    # background = per-pixel median of the video window (static scene)
    stack = np.stack([g for _, g in frames])
    bg = np.median(stack, axis=0)
    s_res = np.abs(snap - bg).flatten()
    s_norm = np.linalg.norm(s_res)
    # 3) normalized correlation of still-residual vs frame-residual (motion)
    best_arr, best_c, curve = None, -2.0, []
    for arr, g in frames:
        if arr < t_fetch_snap - 5:
            continue
        m_res = np.abs(g - bg).flatten()
        nrm = np.linalg.norm(m_res)
        c = float(s_res @ m_res / (s_norm * nrm)) if nrm > 1 else -1.0
        curve.append((arr, c))
        if c > best_c:
            best_c, best_arr = c, arr
    cs = sorted(c for _, c in curve)
    med = cs[len(cs) // 2] if cs else 0.0
    return {
        "id": cam["id"],
        "latency_s": round(best_arr - t_s, 2),
        "match_corr": round(best_c, 3),
        "median_corr": round(med, 3),
        "contrast_ok": bool(best_c > med + 0.15),
        "samples": len(curve),
        "measured_at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", help="comma-separated camera ids (default all)")
    args = ap.parse_args()
    cams = load_cams(args.cams)
    results = []
    for cam in cams:
        print(f"measuring {cam['id']} ...", flush=True)
        r = measure_cam(cam)
        print(json.dumps(r), flush=True)
        results.append(r)
    OUT.parent.mkdir(exist_ok=True)
    merged = json.loads(OUT.read_text()) if OUT.exists() else {}
    for r in results:
        merged[r["id"]] = r
    OUT.write_text(json.dumps(merged, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
