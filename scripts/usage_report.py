#!/usr/bin/env python3
"""Build the localhost-only usage report from Caddy's JSON access log.

Answers: which IPs are using the cameras, what they watched (camera + hour),
and which of them exported footage. An hour is flagged as an export when a
client pulls >= EXPORT_SEGS segments of one hour within EXPORT_WINDOW_S
(playback trickles in at ~1 segment per 6 s) or fetches a .vtt sidecar
(the web export button's clock file).

Writes {root}/admin/usage.html (served by Caddy to localhost only) and
{root}/admin/usage.json. Run from a systemd timer (see deploy/).

Usage: python3 scripts/usage_report.py [STORAGE_ROOT]
       STORAGE_ROOT default: /mnt/8tb/caltrans-snaps
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

EXPORT_SEGS = 15          # segments of one hour-dir within…
EXPORT_WINDOW_S = 60.0    # …this many seconds => export burst
DAY_KEEP = 7              # days of history rendered into the report

HLS_RE = re.compile(r"^/hls/(?P<cam>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})/(?P<hh>\d{2})/(?P<file>[^/?]+)")


def load_entries(log_path: Path):
    """Yield parsed access-log entries (newest file only; rotation keeps 14)."""
    if not log_path.exists():
        return
    for line in log_path.read_text(errors="replace").splitlines():
        if '"request"' not in line:
            continue
        try:
            e = json.loads(line)
            req = e["request"]
            hdrs = req.get("headers", {}) or {}
            # Caddy 2.6 logs the socket IP; behind cloudflared the real
            # visitor is in Cf-Connecting-Ip / X-Forwarded-For (logged as lists)
            def hval(name):
                v = hdrs.get(name) or []
                return (v[-1] if isinstance(v, list) else v).split(",")[-1].strip()
            ip = hval("Cf-Connecting-Ip") or hval("X-Forwarded-For") or req.get("remote_ip", "?")
            yield {
                "ts": float(e.get("ts", 0)),
                "ip": ip,
                "method": req.get("method", ""),
                "path": req.get("uri", "").split("?", 1)[0],
                "status": e.get("status", 0),
                "bytes": int(e.get("size", e.get("bytes_written", 0)) or 0),
                "ua": hval("User-Agent"),
            }
        except Exception:
            continue


def analyze(entries):
    # hour-bucket aggregation: (ip, cam, date, hh) -> stats
    hours = defaultdict(lambda: {"seg_ts": [], "bytes": 0, "playlist": 0,
                                 "vtt": 0, "head": 0})
    ip_meta = defaultdict(lambda: {"uas": set(), "first": None, "last": None,
                                   "bytes": 0})
    for e in entries:
        m = HLS_RE.match(e["path"])
        if not m or e["status"] not in (200, 206):
            continue
        cam, date, hh, f = m.group("cam"), m.group("date"), m.group("hh"), m.group("file")
        key = (e["ip"], cam, date, hh)
        h = hours[key]
        meta = ip_meta[e["ip"]]
        meta["first"] = e["ts"] if meta["first"] is None else min(meta["first"], e["ts"])
        meta["last"] = e["ts"] if meta["last"] is None else max(meta["last"], e["ts"])
        meta["bytes"] += e["bytes"]
        if e["ua"]:
            meta["uas"].add(e["ua"][:120])
        if e["method"] == "HEAD":
            h["head"] += 1
        elif f.endswith(".m3u8"):
            h["playlist"] += 1
        elif f.endswith(".vtt"):
            h["vtt"] += 1
            h["bytes"] += e["bytes"]
        elif f.endswith(".ts"):
            h["seg_ts"].append(e["ts"])
            h["bytes"] += e["bytes"]
    return hours, ip_meta


def export_burst(ts_list) -> bool:
    ts = sorted(ts_list)
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > EXPORT_WINDOW_S:
            j += 1
        if i - j + 1 >= EXPORT_SEGS:
            return True
    return False


def fmt_bytes(n):
    for unit, div in (("MB", 1e6), ("kB", 1e3)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/8tb/caltrans-snaps")
    entries = list(load_entries(root / "logs" / "access.log"))
    hours, ip_meta = analyze(entries)

    now = time.time()
    per_ip = defaultdict(lambda: {"watch": [], "export": []})
    for (ip, cam, date, hh), h in hours.items():
        row = {"cam": cam, "date": date, "hour": hh, "segs": len(h["seg_ts"]),
               "bytes": h["bytes"], "playlist": h["playlist"], "vtt": h["vtt"],
               "probes": h["head"]}
        if not (row["segs"] or row["playlist"] or row["vtt"]):
            continue  # hour-bar HEAD probes only
        is_export = row["vtt"] > 0 or export_burst(h["seg_ts"])
        per_ip[ip]["export" if is_export else "watch"].append(row)

    def last_seen(ip):
        return ip_meta[ip]["last"] or 0

    ips = sorted(per_ip, key=last_seen, reverse=True)
    day_24 = [ip for ip in ips if now - last_seen(ip) < 86400]
    total_bytes = sum(m["bytes"] for m in ip_meta.values())
    n_exports = sum(len(v["export"]) for v in per_ip.values())

    # ---------------- HTML ----------------
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    rows_html = []
    for ip in ips:
        meta, data = ip_meta[ip], per_ip[ip]
        seen = datetime.fromtimestamp(last_seen(ip)).strftime("%Y-%m-%d %H:%M")
        ua = "; ".join(sorted(meta["uas"]))[:90] or "—"
        cells = []
        for r in sorted(data["export"], key=lambda r: (r["date"], r["hour"]), reverse=True):
            cells.append(
                f"<tr class='exp'><td>⬇ EXPORT</td><td>{esc(r['cam'])}</td>"
                f"<td>{r['date']} {r['hour']}:00</td><td>{r['segs']} segs</td>"
                f"<td>{fmt_bytes(r['bytes'])}</td></tr>")
        for r in sorted(data["watch"], key=lambda r: (r["date"], r["hour"]), reverse=True):
            cells.append(
                f"<tr><td>watched</td><td>{esc(r['cam'])}</td>"
                f"<td>{r['date']} {r['hour']}:00</td><td>{r['segs']} segs</td>"
                f"<td>{fmt_bytes(r['bytes'])}</td></tr>")
        rows_html.append(
            f"""<tr class='iprow'><td rowspan='{max(len(cells),1)}' class='ipcell'>
            <b>{esc(ip)}</b><br><small>last seen {seen}<br>{fmt_bytes(meta['bytes'])} served<br>
            {len(data['export'])} export(s)</small><br><small class='ua'>{esc(ua)}</small></td>
            {'</tr><tr>'.join(cells) if cells else '<td colspan=4>—</td>'}</tr>""")

    html = f"""<!DOCTYPE html>
<html lang=en><head><meta charset=utf-8><meta http-equiv=refresh content=60>
<title>caltrans-snaps · usage</title><style>
:root {{ color-scheme: dark; }} body {{ font-family: system-ui, sans-serif; background:#101318; color:#e6e9ee; margin:1.2rem; }}
h1 {{ font-size:1.1rem; margin:0 0 .3rem; }} .sub {{ color:#9aa5b5; font-size:.85rem; margin-bottom:1rem; }}
table {{ border-collapse:collapse; width:100%; font-size:.88rem; }}
td,th {{ padding:.35rem .55rem; border-bottom:1px solid #2a3140; text-align:left; vertical-align:top; }}
th {{ color:#8fa2bd; font-weight:600; position:sticky; top:0; background:#101318; }}
.ipcell {{ background:#161b24; white-space:nowrap; }} .ua {{ color:#5c6a80; word-break:break-all; }}
tr.exp td {{ background:#3a2f1e; }} tr.exp td:first-child {{ color:#ffd479; font-weight:600; }}
code {{ background:#1a1f28; padding:.1rem .3rem; border-radius:4px; }}
</style></head><body>
<h1>📷 caltrans-snaps — who's using the cameras</h1>
<div class=sub>generated {datetime.now():%Y-%m-%d %H:%M:%S} · refreshes every 60 s ·
<b>{len(day_24)}</b> distinct IP(s) in last 24 h · <b>{n_exports}</b> export(s) detected ·
{fmt_bytes(total_bytes)} served (since log start, rotated at 20 MiB ×14)<br>
export rule: ≥{EXPORT_SEGS} segments of one hour within {int(EXPORT_WINDOW_S)} s, or a .vtt sidecar fetch.
localhost-only page — not reachable through the tunnel.</div>
<table><tr><th>client</th><th>action</th><th>camera</th><th>hour</th><th>segments</th><th>bytes</th></tr>
{''.join(rows_html) or '<tr><td colspan=6>no /hls traffic logged yet</td></tr>'}
</table></body>"""

    out_dir = root / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "usage.json").write_text(json.dumps(
        {"generated": now,
         "ips": {ip: {"meta": {"last_seen": ip_meta[ip]["last"], "bytes": ip_meta[ip]["bytes"]},
                      "watch": per_ip[ip]["watch"], "export": per_ip[ip]["export"]}
                 for ip in ips}}, indent=1))
    tmp = out_dir / "usage.html.tmp"
    tmp.write_text(html)
    tmp.replace(out_dir / "usage.html")
    print(f"{len(ips)} IP(s), {n_exports} export(s) -> {out_dir/'usage.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
