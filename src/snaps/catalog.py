"""Caltrans CCTV catalog client (ArcGIS REST) + camera selection.

Queries the statewide Caltrans CCTV feature service and lets you pick a
camera set by county, with route preference / demotion rules, writing both
the recorder config (config/cameras.yaml) and the website data
(web/cameras.json).

Example:
    python -m snaps.catalog \
        --counties Butte Yuba Sutter \
        --near 39.5124,-121.5417 \
        --prefer-route SR-70 \
        --demote Chico Skyway Southgate Eaton Garner East_Ave East_20th "Hwy 32" \
        --limit 10
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

API_URL = (
    "https://caltrans-gis.dot.ca.gov/arcgis/rest/services/"
    "CHhighway/CCTV/FeatureServer/0/query"
)
USER_AGENT = "caltrans-snaps/0.1 (personal archive project)"


def fetch_cameras(counties: List[str], timeout: int = 30) -> List[Dict[str, Any]]:
    """Fetch all in-service cameras with a streaming video URL for given counties."""
    where = " OR ".join(f"county='{c}'" for c in counties)
    params = urllib.parse.urlencode(
        {"where": where, "outFields": "*", "f": "json"}
    )
    req = urllib.request.Request(f"{API_URL}?{params}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if "error" in data:
        raise RuntimeError(f"ArcGIS API error: {data['error']}")
    cams = []
    for feat in data.get("features", []):
        a = feat["attributes"]
        if not (a.get("streamingVideoURL") or "").strip():
            continue
        if a.get("inService") is False:
            continue
        cams.append(a)
    return cams


def slugify_stream(stream_url: str) -> str:
    """Stable camera id derived from the wzmedia stream name."""
    stem = stream_url.rstrip("/").split("/")[-2]  # "<name>.stream"
    stem = re.sub(r"\.stream$", "", stem)
    return re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()


def haversineish(lat: float, lon: float, near: tuple) -> float:
    dlat = (lat - near[0]) * 111.0
    dlon = (lon - near[1]) * 88.0
    return math.hypot(dlat, dlon)


def select_cameras(
    cams: List[Dict[str, Any]],
    near: tuple,
    prefer_routes: List[str],
    demote_patterns: List[str],
    limit: int,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Rank: preferred routes first, then non-demoted, then distance to anchor.

    include_patterns force cameras in (matched on locationName); exclude_patterns
    drop them outright. Fill-up beyond forced picks only uses non-demoted
    candidates so 'deprioritize Chico' isn't silently violated by padding.
    """
    include_patterns = include_patterns or []
    exclude_patterns = exclude_patterns or []
    cams = [a for a in cams
            if not any(re.search(p, a.get("locationName") or "", re.I) for p in exclude_patterns)]

    def rank(a: Dict[str, Any]):
        name = a.get("locationName") or ""
        route = a.get("route") or ""
        # tolerate underscores in API names when user passes spaced patterns
        pats = [p.replace(" ", "_").replace("_", ".") if "_" not in p and " " in p else p
                for p in demote_patterns]
        demoted = any(re.search(p, name, re.IGNORECASE) for p in pats)
        dist = haversineish(a["latitude"], a["longitude"], near)
        return (route not in prefer_routes, demoted, dist, name)

    def norm(name: str) -> str:
        return re.sub(r"\s+", " ", name.replace("_", " ").strip())

    forced = [a for a in cams
              if any(re.search(p, a.get("locationName") or "", re.I) for p in include_patterns)]
    selected, seen_demoted = list(forced), set()
    for a in forced:
        nm = norm(a.get("locationName") or "")
        seen_demoted.add(re.sub(r"\s*\d*$", "", nm))

    ranked = sorted(cams, key=rank)
    for a in ranked:
        if len(selected) >= limit or a in forced:
            continue
        name = a.get("locationName") or ""
        is_demoted = any(re.search(p, name, re.IGNORECASE) for p in demote_patterns)
        if is_demoted:
            base = re.sub(r"\s*\d*$", "", norm(name))
            if base in seen_demoted:
                continue
            seen_demoted.add(base)
        selected.append(a)
    # keep output grouped by route for readability
    selected.sort(key=lambda a: (a["route"], a["latitude"]))
    return selected


def to_records(cams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    recs = []
    for a in cams:
        stream = a["streamingVideoURL"].strip()
        recs.append(
            {
                "id": slugify_stream(stream),
                "objectid": a["OBJECTID"],
                "name": (a.get("locationName") or "").strip(),
                "route": a.get("route"),
                "county": a.get("county"),
                "place": a.get("nearbyPlace"),
                "direction": a.get("direction"),
                "lat": a["latitude"],
                "lon": a["longitude"],
                "stream_url": stream,
            }
        )
    return recs


def write_yaml(path: str, records: List[Dict[str, Any]], storage_root: str, retention_hours: int) -> None:
    import yaml  # noqa: deferred import

    header = (
        "# caltrans-snaps recorder configuration.\n"
        f"# Generated by python -m snaps.catalog at {time.strftime('%Y-%m-%dT%H:%M%z')}.\n"
        "# Edit cameras by hand or regenerate; the recorder reads this file.\n"
    )
    doc = {
        "storage_root": storage_root,
        "recording": {
            "segment_seconds": 6,       # HLS part length; small = fast seeking in history
            "session_hours": 1,         # one ffmpeg run per clock hour (restart at boundary)
            "retention_hours": retention_hours,
            "url_refresh_hours": 24,    # re-query ArcGIS for stream URLs daily (see Makefile)
        },
        "cameras": records,
    }
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)


def write_site_json(path: str, records: List[Dict[str, Any]], region_name: str) -> None:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in sorted(records, key=lambda x: (x["route"] or "", -(x["lat"] or 0))):
        groups.setdefault(r["route"], []).append(r)
    site = {
        "region": region_name,
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "timezone": "America/Los_Angeles",  # recording host local time = storage layout
        "groups": [{"route": k, "cameras": v} for k, v in groups.items()],
    }
    with open(path, "w") as f:
        json.dump(site, f, indent=1)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--counties", nargs="+", required=True)
    p.add_argument("--near", required=True, help="anchor lat,lon e.g. 39.5124,-121.5417")
    p.add_argument("--prefer-route", nargs="*", default=[])
    p.add_argument("--demote", nargs="*", default=[], help="regexes matched against locationName; ranked last")
    p.add_argument("--include", nargs="*", default=[], help="locationName regexes to force into the set")
    p.add_argument("--exclude", nargs="*", default=[], help="locationName regexes to drop outright")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--region", default="Oroville / SR-70 Corridor")
    p.add_argument("--storage-root", default="/mnt/8tb/caltrans-snaps")
    p.add_argument("--retention-hours", type=int, default=2160)
    p.add_argument("--config-out", default="config/cameras.yaml")
    p.add_argument("--site-out", default="web/cameras.json")
    args = p.parse_args(argv)

    near = tuple(float(x) for x in args.near.split(","))
    cams = fetch_cameras(args.counties)
    sel = select_cameras(cams, near, args.prefer_route, args.demote, args.limit,
                         include_patterns=args.include, exclude_patterns=args.exclude)
    recs = to_records(sel)

    write_yaml(args.config_out, recs, args.storage_root, args.retention_hours)
    write_site_json(args.site_out, recs, args.region)

    print(f"fetched {len(cams)} stream cameras in {args.counties}; selected {len(recs)}:")
    for r in recs:
        print(f"  [{r['route']}] {r['county']:8} {r['name']:35} -> {r['id']}")
    print(f"wrote {args.config_out} and {args.site_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
