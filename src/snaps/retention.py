"""Retention: delete recorded hours older than the configured window."""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


def sweep_once(root: Path, retention_hours: int) -> int:
    """Delete {root}/hls/<cam>/<date>/<HH>/ dirs older than retention_hours.

    Returns number of hour-directories removed. Uses host local time to
    match how sessions are written.
    """
    cutoff = datetime.now() - timedelta(hours=retention_hours)
    removed = 0
    hls_root = root / "hls"
    if not hls_root.exists():
        return 0
    for cam_dir in hls_root.iterdir():
        if not cam_dir.is_dir():
            continue
        for date_dir in cam_dir.iterdir():
            try:
                day = datetime.strptime(date_dir.name, "%Y-%m-%d")
            except ValueError:
                continue
            for hour_dir in list(date_dir.iterdir()):
                try:
                    when = day.replace(hour=int(hour_dir.name))
                except ValueError:
                    continue
                if when < cutoff and (hour_dir / "index.m3u8").exists():
                    shutil.rmtree(hour_dir, ignore_errors=True)
                    removed += 1
            if date_dir.is_dir() and not any(date_dir.iterdir()):
                date_dir.rmdir()
        if not any(cam_dir.iterdir()):
            cam_dir.rmdir()
    return removed


def retention_loop(root: Path, retention_hours: int, stop: threading.Event,
                   interval_s: int = 600) -> None:
    while not stop.wait(timeout=interval_s):
        try:
            n = sweep_once(Path(root), retention_hours)
            if n:
                log.info("retention: removed %d expired hour dirs", n)
        except Exception:
            log.exception("retention sweep failed")
