#!/usr/bin/env python3
"""Generate static SEO landing pages for every camera (web/cams/<id>/index.html).

One crawlable page per camera so long-tail queries ("Hwy 5 at Richards traffic
camera", "US-50 Manlove cam") can land directly, instead of everything fighting
over the single-page index. Also:

  * grabs one real footage frame per camera from the local HLS archive
    (web/img/cams/<id>.jpg) for thumbnails + og:image
  * a 1200x630 mosaic og-cover.jpg for the site itself
  * web/cams/index.html hub page (crawlable internal-link structure)
  * sitemap.xml covering / , /cams/ and every camera page

Run on the recording box (needs the HLS tree), then commit web/:
    python3 scripts/build_seo_pages.py            # full build
    python3 scripts/build_seo_pages.py --no-imgs  # pages only, keep existing images
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

SITE = "https://www.sacvalleycams.com"
MEDIA = "https://media.sacvalleycams.com"
FIRST_RECORDING = "2026-10-12T09:00:00Z"
REPO = "adesas/caltrans-snaps"

# pick four visually distinct cams for the social share mosaic (fall back if absent)
MOSAIC = ["50-sunrise-blvd-oc-jwo-sac50-wb-1", "80-watt-ave-oc-sac80-wb",
          "99-florin-rd-jso-sac99-nb-1", "5-laguna-blvd-jno-sac5-nb"]


def latest_segment(hls_root: Path, cam_id: str) -> Path | None:
    """Newest .ts segment (~30s in, past any corrupt head) scanning newest hours first."""
    camdir = hls_root / cam_id
    if not camdir.is_dir():
        return None
    for d in sorted((p for p in camdir.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)), reverse=True):
        for hh in sorted((p for p in d.iterdir() if p.is_dir()), reverse=True):
            ts = sorted(hh.glob("*.ts"))
            if ts:
                return ts[min(5, len(ts) - 1)]
    return None


def grab_frame(seg: Path, out: Path, width=640) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(seg),
         "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)])
    return r.returncode == 0 and out.exists()


def build_mosaic(imgs: list[Path], out: Path) -> bool:
    if len(imgs) < 4:
        return False
    args = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in imgs:
        args += ["-i", str(p)]
    args += ["-filter_complex",
             "[0][1][2][3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[m];"
             "[m]scale=1200:630:force_original_aspect_ratio=increase,crop=1200:630",
             "-frames:v", "1", "-q:v", "3", str(out)]
    return subprocess.run(args).returncode == 0 and out.exists()


def jsonld_cam(cam: dict) -> str:
    e = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Sac Valley Cams", "item": SITE + "/"},
                {"@type": "ListItem", "position": 2, "name": "All cameras", "item": SITE + "/cams/"},
                {"@type": "ListItem", "position": 3, "name": cam["name"], "item": f"{SITE}/cams/{cam['id']}/"},
            ]},
            {"@type": "Place", "name": f"{cam['name']} traffic camera",
             "geo": {"@type": "GeoCoordinates", "latitude": cam["lat"], "longitude": cam["lon"]},
             "image": f"{SITE}/img/cams/{cam['id']}.jpg"},
        ],
    }
    return json.dumps(e, ensure_ascii=False)


def cam_page(cam: dict, has_img: bool) -> str:
    n = html.escape(cam["name"])
    route = html.escape(cam["route"] or "")
    place = html.escape(cam["place"] or cam["county"] or "Sacramento Valley")
    county = html.escape(cam["county"] or "")
    dirn = f" ({html.escape(cam['direction'])}bound)" if cam.get("direction") else ""
    title = f"{n} Traffic Camera — {route}, {place} | Sac Valley Cams"
    desc = (f"Live feed plus a 30-day recording archive of the {cam['name']} traffic camera "
            f"on {route} near {place}, {county} county{dirn}. Watch any hour of the past month, "
            f"scrub by day and hour, export clips. Free, no signup.")
    img = (f'  <img src="/img/cams/{cam["id"]}.jpg" width="640" height="480" '
           f'alt="Frame from the {n} traffic camera on {route} near {place}">\n' if has_img else "")
    off = "" if not cam.get("offline") else (
        " <em>Caltrans has this feed temporarily offline — recordings made before the outage remain browsable.</em>")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{html.escape(desc)}">
<link rel="canonical" href="{SITE}/cams/{cam['id']}/">
<meta property="og:type" content="website">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc)}">
<meta property="og:url" content="{SITE}/cams/{cam['id']}/">
{"<meta property='og:image' content='" + SITE + "/img/cams/" + cam['id'] + ".jpg'>" if has_img else "<meta property='og:image' content='" + SITE + "/img/og-cover.jpg'>"}
<meta name="twitter:card" content="summary_large_image">
<link rel="stylesheet" href="/style.css">
<script type="application/ld+json">{jsonld_cam(cam)}</script>
</head>
<body>
<header><div>
  <h1>{n} traffic camera{dirn}</h1>
  <p class="sub">{route} · {place}, {county} county — live feed &amp; 30-day archive. Footage courtesy of <a href="https://cwwp2.dot.ca.gov/vm/streamlist.htm" rel="noreferrer">Caltrans CCTV</a>.</p>
</div><a class="btn" href="/?cam={cam['id']}">▶ Watch {n}</a></header>
<main style="display:block;max-width:900px;margin:1rem auto;padding:0 1.2rem">
{img}{off}
<p>Use the player to pick any day and hour of the past {30} days of recordings, or <a href="/#map">browse all cameras on the map</a>.</p>
<p><a href="/cams/">All cameras</a> · <a href="/cams/">Camera index</a> · <a href="/">Home</a></p>
</main>
<footer>caltrans-snaps — independent 30-day Caltrans camera archive. <a href="https://github.com/{REPO}">Source on GitHub</a>.</footer>
</body>
</html>
"""


def hub_page(cams: list[dict]) -> str:
    groups: dict[str, list] = {}
    for c in cams:
        groups.setdefault(c["route"] or "?", []).append(c)
    lis = []
    for route, cs in sorted(groups.items()):
        links = " · ".join(
            f'<a href="/cams/{c["id"]}/">{html.escape(c["name"])}</a>'
            + (" <em>(offline)</em>" if c.get("offline") else "")
            for c in sorted(cs, key=lambda x: -(x["lat"] or 0)))
        lis.append(f"<h3>{html.escape(route)}</h3><p>{links}</p>")
    nrec = sum(1 for c in cams if not c.get("offline"))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Traffic Camera Index — {len(cams)} cameras, I-5 · I-80 · US-50 · SR-51 · SR-70 · SR-99 | Sac Valley Cams</title>
<meta name="description" content="Every Caltrans traffic camera archived by sacvalleycams.com: {nrec} live feeds with a free 30-day recording archive across I-5, I-80, US-50, SR-51, SR-70 and SR-99 in the Sacramento Valley.">
<link rel="canonical" href="{SITE}/cams/">
<meta property="og:title" content="Traffic Camera Index — Sac Valley Cams">
<meta property="og:image" content="{SITE}/img/og-cover.jpg">
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header><div><h1>Traffic camera index</h1>
<p class="sub">{len(cams)} cameras ({nrec} recording). Each links to its own page with live feed, 30-day archive and export.</p></div>
<a class="btn" href="/#map">🗺 Map view</a></header>
<main style="display:block;max-width:980px;margin:1rem auto;padding:0 1.2rem;color:#9aa5b5">
{''.join(lis)}
</main>
<footer>caltrans-snaps — independent 30-day Caltrans camera archive. <a href="https://github.com/{REPO}">Source on GitHub</a>.</footer>
</body>
</html>
"""


def write_sitemap(cams: list[dict], path: Path) -> None:
    today = time.strftime("%Y-%m-%d")
    urls = [(SITE + "/", "daily", "0.9"), (SITE + "/cams/", "weekly", "0.7")]
    urls += [(f"{SITE}/cams/{c['id']}/", "weekly", "0.6") for c in cams]
    body = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{today}</lastmod><changefreq>{cf}</changefreq><priority>{p}</priority></url>"
        for u, cf, p in urls)
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "\n</urlset>\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--web", default="web")
    ap.add_argument("--hls", default="/mnt/8tb/caltrans-snaps/hls")
    ap.add_argument("--no-imgs", action="store_true", help="skip ffmpeg frame grabs (reuse existing)")
    a = ap.parse_args()

    web, hls = Path(a.web), Path(a.hls)
    site = json.loads((web / "cameras.json").read_text())
    cams = [c for g in site["groups"] for c in g["cameras"]]

    img_dir = web / "img" / "cams"
    img_dir.mkdir(parents=True, exist_ok=True)
    got = skipped = 0
    frames: dict[str, Path] = {}
    for cam in cams:
        out = img_dir / f"{cam['id']}.jpg"
        if a.no_imgs and out.exists():
            frames[cam["id"]] = out
            continue
        seg = latest_segment(hls, cam["id"])
        if seg and grab_frame(seg, out):
            frames[cam["id"]] = out
            got += 1
        else:
            skipped += 1

    mosaic_src = [frames[m] for m in MOSAIC if m in frames]
    ok_mosaic = build_mosaic(mosaic_src, web / "img" / "og-cover.jpg") if len(mosaic_src) == 4 else False

    pages = web / "cams"
    for cam in cams:
        d = pages / cam["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(cam_page(cam, cam["id"] in frames))
    (pages / "index.html").write_text(hub_page(cams))
    write_sitemap(cams, web / "sitemap.xml")

    print(f"cameras: {len(cams)}; frames grabbed: {got}, missing/skipped: {skipped}; og-cover: {'ok' if ok_mosaic else 'NOT BUILT'}")
    print(f"wrote {pages}/ (index + {len(cams)} pages) and {web/'sitemap.xml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
