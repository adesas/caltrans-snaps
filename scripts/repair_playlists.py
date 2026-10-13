#!/usr/bin/env python3
"""Rebuild hour-directory HLS playlists from the .ts files actually on disk.

A service restart mid-hour used to reset ffmpeg's per-attempt segment prefix,
which could overwrite earlier segments and leave index.m3u8 listing a mix of
stale/missing entries. This tool detects such hours (playlist names != files
on disk, or duplicates) and regenerates the playlist:

  * segments ordered by file mtime (recording order),
  * exact durations via ffprobe,
  * #EXT-X-PROGRAM-DATE-TIME per segment derived from file mtime minus
    duration — so even legacy/repaired hours get exact wall-clock times in
    the web player and exports,
  * #EXT-X-DISCONTINUITY where there is a recording gap.

Dry-run by default; --write to apply. Skips hour dirs written in the last
120 s (a live session may be mid-append).

Usage:
  python3 scripts/repair_playlists.py [ROOT]           # report
  python3 scripts/repair_playlists.py [ROOT] --write   # fix
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = "/mnt/8tb/caltrans-snaps/hls"
LIVE_GUARD_S = 120


def probe_duration(seg: Path) -> float | None:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(seg)],
        capture_output=True, text=True)
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def playlist_names(pl_file: Path) -> list[str]:
    if not pl_file.exists():
        return []
    return [ln.strip() for ln in pl_file.read_text().splitlines()
            if ln.strip().endswith(".ts")]


def rebuild(hdir: Path) -> str:
    segs = sorted((f for f in hdir.glob("a*_*.ts")),
                  key=lambda f: (f.stat().st_mtime, f.name))
    pl = hdir / "index.m3u8"
    have, want = playlist_names(pl), [f.name for f in segs]
    if not want:
        return "empty"
    if sorted(have) == sorted(want) and len(set(have)) == len(have):
        return "ok"

    entries, max_dur = [], 0.0
    for f in segs:
        dur = probe_duration(f) or 6.0
        start_wall = f.stat().st_mtime - dur
        entries.append((f.name, dur, start_wall))
        max_dur = max(max_dur, dur)

    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-ALLOW-CACHE:YES",
             f"#EXT-X-TARGETDURATION:{int(max_dur) + 1}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-INDEPENDENT-SEGMENTS"]
    prev_end = None
    for name, dur, start_wall in entries:
        if prev_end is not None and start_wall - prev_end > max(15.0, dur * 3):
            lines.append("#EXT-X-DISCONTINUITY")
        pdt = datetime.fromtimestamp(start_wall).isoformat(timespec="milliseconds")
        lines += [f"#EXTINF:{dur:.6f},", f"#EXT-X-PROGRAM-DATE-TIME:{pdt}", name]
        prev_end = start_wall + dur

    tmp = pl.with_suffix(".m3u8.tmp")
    tmp.write_text("\n".join(lines) + "\n#EXT-X-ENDLIST\n")
    tmp.replace(pl)
    return f"rebuilt ({len(entries)} segments)"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    write = "--write" in sys.argv
    root = Path(args[0] if args else DEFAULT_ROOT)
    changed = 0
    for pl in sorted(root.glob("*/*/*/index.m3u8")):
        hdir = pl.parent
        newest = max((f.stat().st_mtime for f in hdir.glob("a*_*.ts")), default=0)
        if time.time() - newest < LIVE_GUARD_S:
            continue  # live session may be appending right now
        r = rebuild(hdir) if write else (
            "would rebuild" if sorted(playlist_names(pl)) != sorted(
                f.name for f in hdir.glob("a*_*.ts"))
            or len(set(playlist_names(pl))) != len(playlist_names(pl)) else "ok")
        if r not in ("ok", "empty"):
            changed += 1
            print(f"{hdir}: {r}")
    print(f"{'repaired' if write else 'affected'} hours: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
