"""Per-camera HLS recorder: one ffmpeg copy-mode session per clock hour.

Storage layout (all times are the recording host's local time, which for
this deployment is America/Los_Angeles):

    {storage_root}/hls/{camera_id}/{YYYY-MM-DD}/{HH}/index.m3u8
    {storage_root}/hls/{camera_id}/{YYYY-MM-DD}/{HH}/seg_000.ts ...
    {storage_root}/hls/{camera_id}/{YYYY-MM-DD}/{HH}/thumb.jpg   (first frame)
    {storage_root}/status/{camera_id}.json                        (heartbeat)

Caltrans HLS feeds 404 periodically (~every 20-30 min, normal); sessions
that die early are restarted with exponential backoff + jitter (NOT a fixed
cap: wzmedia rate-limits per IP and a synchronized restart storm across all
camers gets our IP penalty-boxed — see 2026-10-14 incident where all new TCP
connects to wzmedia were SYN-dropped after hours of ~30 s retry hammering).
A stalled session (no new segments for STALL_SECONDS even though ffmpeg is
"alive") is killed and restarted like a dead one.
No transcoding: CPU per camera is ~1-2%, disk write ~50 KB/s per camera.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import subprocess
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

# kill a session if no new .ts segment appears for this long
STALL_SECONDS = 90


def hour_dir(root: Path, cam_id: str, when: datetime) -> Path:
    return root / "hls" / cam_id / when.strftime("%Y-%m-%d") / when.strftime("%H")


class CameraRecorder:
    """Records one camera forever (until stop() is set). Runs in its own thread."""

    def __init__(self, camera: Dict, storage_root: Path, segment_seconds: int = 6,
                 stop: Optional[threading.Event] = None):
        self.cam = camera
        self.cam_id = camera["id"]
        self.root = storage_root
        self.segment_seconds = segment_seconds
        self.stop = stop or threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.session_hour_start: Optional[datetime] = None
        self.attempt = 0  # restarts within one hour dir; each gets its own segment prefix
        self.consecutive_failures = 0
        self.last_heartbeat = 0.0
        self.session_started_ts = 0.0
        # Stable per-camera offset (0-45 s) applied before starting a session:
        # keeps 50+ cameras from reconnecting in lockstep on the clock hour.
        self.phase = zlib.crc32(self.cam_id.encode()) % 46

    # ------------------------------------------------------------------ ffmpeg

    def _ffmpeg_cmd(self, out_dir: Path) -> list:
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "8",
            "-i", self.cam["stream_url"],
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(self.segment_seconds),
            "-hls_list_size", "0",          # full playlist for the hour (seekable history)
            "-hls_allow_cache", "1",
            # append_list + per-attempt prefix: if Caltrans 404s the feed mid-hour,
            # the restart appends new segments instead of clobbering earlier ones.
            # (flag names are lowercase on ffmpeg >= 6; uppercase broke parsing on 7.1)
            # program_date_time: each attempt's segments carry
            # #EXT-X-PROGRAM-DATE-TIME so players/exports can map video time
            # to wall-clock exactly, even across mid-hour reconnect gaps.
            "-hls_flags", "append_list+independent_segments+program_date_time",
            "-hls_segment_filename", str(out_dir / f"a{self.attempt}_%03d.ts"),
            str(out_dir / "index.m3u8"),
        ]

    def _stalled(self, out_dir: Path) -> bool:
        """True if the running session has produced no new segment recently."""
        newest = self.session_started_ts
        for f in out_dir.glob("a*_*.ts"):
            m = f.stat().st_mtime
            if m > newest:
                newest = m
        return time.time() - newest > STALL_SECONDS

    def _backoff_wait(self) -> None:
        """Exponential backoff with jitter between restarts (interruptible).

        wzmedia rate-limits per IP; after N consecutive failures we must get
        progressively more polite instead of retrying on a short fixed cycle.
        Sequence ~5,10,20,40,80,160,300,300...s (±25% jitter).
        """
        n = max(self.consecutive_failures, 1)
        base = min(300.0, 5.0 * (2 ** (n - 1)))
        delay = base * (0.75 + random.random() * 0.5)
        log.info("[%s] backing off %.0f s (failure #%d)", self.cam_id, delay, n)
        self.stop.wait(timeout=delay)

    def _start_session(self, hour_start: datetime) -> None:
        out_dir = hour_dir(self.root, self.cam_id, hour_start)
        out_dir.mkdir(parents=True, exist_ok=True)
        # Prefix numbering is per hour-dir, NOT per process: a service restart
        # mid-hour must never reuse an a<N>_ prefix that already exists (it
        # would overwrite earlier segments). Scan for the next free prefix.
        used = [int(m.group(1)) for f in out_dir.glob("a*_*.ts")
                if (m := re.match(r"a(\d+)_", f.name))]
        self.attempt = max(used, default=-1) + 1
        log.info("[%s] starting session %s (attempt a%d)", self.cam_id, out_dir, self.attempt)
        self.proc = subprocess.Popen(
            self._ffmpeg_cmd(out_dir),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.session_hour_start = hour_start
        self.session_started_ts = time.time()

    def _kill_session(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)  # finish playlist cleanly
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self.proc = None

    def _finalize_hour(self, hour_start: datetime) -> None:
        """After an hour session ends: make thumbnail + heartbeat."""
        out_dir = hour_dir(self.root, self.cam_id, hour_start)
        segs = sorted(out_dir.glob("a*_*.ts"))
        first_seg = segs[0] if segs else out_dir / "a0_000.ts"
        if segs:
            self.consecutive_failures = 0
        if not first_seg.exists():
            log.warning("[%s] no segments for %s", self.cam_id, out_dir.name)
            return
        thumb = out_dir / "thumb.jpg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-i", str(first_seg), "-frames:v", "1", str(thumb)],
                timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:  # thumbnail is best-effort
            log.warning("[%s] thumbnail failed: %s", self.cam_id, e)
        self._heartbeat(hour_start, ok=True)

    def _heartbeat(self, hour_start: datetime, ok: bool) -> None:
        self.last_heartbeat = time.time()
        st_dir = self.root / "status"
        st_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "camera_id": self.cam_id,
            "last_hour_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_session_local": hour_start.strftime("%Y-%m-%d %H:00"),
            "ok": ok,
            "consecutive_failures": self.consecutive_failures,
        }
        tmp = st_dir / f"{self.cam_id}.json.tmp"
        tmp.write_text(json.dumps(payload))
        tmp.replace(st_dir / f"{self.cam_id}.json")

    # ------------------------------------------------------------------- loop

    def run(self) -> None:
        """Hour-aligned record loop, polling every ~2 s. Exits when stop is set."""
        while not self.stop.is_set():
            hour_start = datetime.now().replace(minute=0, second=0, microsecond=0)

            if self.proc is not None:
                alive = self.proc.poll() is None
                # watchdog: an ffmpeg stuck on a black-holed TCP connect looks
                # "alive" forever but writes nothing — kill and treat as dead.
                if alive and self._stalled(hour_dir(self.root, self.cam_id, self.session_hour_start)):
                    log.warning("[%s] session stalled (no segments for %ss), killing",
                                self.cam_id, STALL_SECONDS)
                    self._kill_session()
                    alive = False
                if alive and hour_start == self.session_hour_start:
                    if time.time() - self.last_heartbeat > 60:
                        self._heartbeat(hour_start, ok=True)
                    time.sleep(2)
                    continue
                # died mid-hour, or we crossed the clock boundary -> close out hour
                self._kill_session()
                if self.session_hour_start is not None:
                    self._finalize_hour(self.session_hour_start)
                if self.stop.is_set():
                    break
                if hour_start == self.session_hour_start and alive is False:
                    # mid-hour stream drop: back off; _start_session picks the
                    # next free segment prefix from what is on disk
                    self.consecutive_failures += 1
                    self._backoff_wait()
            if self.stop.is_set():
                break
            if self.proc is None:
                # per-camera phase: don't start in lockstep with every other
                # camera on the clock hour (reconnect storm invites rate limits)
                due = hour_start + timedelta(seconds=self.phase)
                wait = (due - datetime.now()).total_seconds()
                if 0 < wait:
                    self.stop.wait(timeout=wait)
                if self.stop.is_set():
                    break
                self._start_session(hour_start)
        # shutdown: close out the partial hour so its playlist/thumbnail/status survive
        self._kill_session()
        if self.session_hour_start is not None:
            self._finalize_hour(self.session_hour_start)
        log.info("[%s] recorder stopped", self.cam_id)
