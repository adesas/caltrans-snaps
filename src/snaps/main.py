"""Entry point: load config, start per-camera recorder threads + retention."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

import yaml

from .recorder import CameraRecorder
from .retention import retention_loop

log = logging.getLogger("snaps")


def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not cfg or not cfg.get("cameras"):
        raise SystemExit(f"config {path}: 'cameras' list is required")
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/cameras.yaml")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(Path(args.config))
    root = Path(cfg["storage_root"])
    rec = cfg.get("recording", {})
    segment_seconds = int(rec.get("segment_seconds", 6))
    retention_hours = int(rec.get("retention_hours", 2160))
    root.mkdir(parents=True, exist_ok=True)

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("signal %s -> shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threads = []
    for cam in cfg["cameras"]:
        r = CameraRecorder(cam, root, segment_seconds=segment_seconds, stop=stop)
        t = threading.Thread(target=r.run, name=f"rec-{cam['id']}", daemon=True)
        t.start()
        threads.append(t)
        log.info("started recorder for %s (%s)", cam["id"], cam.get("name"))

    tr = threading.Thread(
        target=retention_loop, args=(root, retention_hours, stop),
        name="retention", daemon=True,
    )
    tr.start()

    print(f"recording {len(cfg['cameras'])} cameras to {root} "
          f"(retention {retention_hours}h); Ctrl-C to stop", flush=True)
    try:
        while not stop.is_set():
            stop.wait(timeout=5)
    except KeyboardInterrupt:
        stop.set()

    for t in threads + [tr]:
        t.join(timeout=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
