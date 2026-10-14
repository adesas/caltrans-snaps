/* global Hls, CFG */
"use strict";

const $ = (s) => document.querySelector(s);
// local preview: `make site-serve` + caddy on :8090 both work on localhost
if (location.hostname === "localhost") CFG.MEDIA_BASE = "http://localhost:8090";
let catalog = null;
let currentCam = null;
let currentHh = null;
let hls = null;
let exporting = false;
let exportAbort = null;

const pad = (n) => String(n).padStart(2, "0");
const fmtDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function hourUrl(camId, dateStr, hh) {
  return `${CFG.MEDIA_BASE}/hls/${camId}/${dateStr}/${pad(hh)}/index.m3u8`;
}
function thumbUrl(camId, dateStr, hh) {
  // thumbnails are written when an hour completes; if the current hour has
  // none yet, fall back once to the previous hour's thumbnail
  const d = new Date(`${dateStr}T00:00:00`);
  d.setHours(hh - 1);
  const prev = `${CFG.MEDIA_BASE}/hls/${camId}/${fmtDate(d)}/${pad(d.getHours())}/thumb.jpg`;
  return `<img loading="lazy" alt="" src="${CFG.MEDIA_BASE}/hls/${camId}/${dateStr}/${pad(hh)}/thumb.jpg"
    onerror="if(!this.dataset.p){this.dataset.p=1;this.src='${prev}'}else this.style.display='none'">`;
}

async function init() {
  $("#requestBtn").href =
    `https://github.com/${CFG.REPO}/issues/new?labels=coverage-request&title=Add+county:+%3CCounty%3E`;
  $("#retentionDays").textContent = Math.round(CFG.RETENTION_HOURS / 24);

  catalog = await (await fetch("cameras.json")).json();
  renderSidebar();

  const now = new Date();
  $("#dayPick").value = fmtDate(now);
  $("#dayPick").max = fmtDate(now);
  const min = new Date(now.getTime() - CFG.RETENTION_HOURS * 3600e3);
  $("#dayPick").min = fmtDate(min);
  $("#dayPick").onchange = renderHourBar;
  $("#nowBtn").onclick = () => { if (currentCam) selectHour(new Date().getHours()); };
  const video = $("#video");
  video.addEventListener("timeupdate", tickClock);
  video.addEventListener("loadedmetadata", tickClock);
  renderHourBar();
}

function renderSidebar() {
  const side = $("#sidebar");
  side.innerHTML = "";
  for (const g of catalog.groups) {
    const h = document.createElement("h2");
    h.textContent = g.route;
    side.appendChild(h);
    for (const cam of g.cameras) {
      const card = document.createElement("button");
      card.className = "cam" + (cam.offline ? " off" : "");
      card.dataset.id = cam.id;
      card.innerHTML = `${thumbUrl(cam.id, fmtDate(new Date()), new Date().getHours())}
        <span>${cam.name}${cam.offline ? ' <em class="off-tag">offline</em>' : ""}</span><small>${cam.county}${cam.direction ? " · " + cam.direction : ""}</small>`;
      card.onclick = () => selectCam(cam);
      side.appendChild(card);
    }
  }
}

function renderHourBar() {
  const bar = $("#hourBar");
  bar.innerHTML = "";
  if (!currentCam) return;
  const dateStr = $("#dayPick").value || fmtDate(new Date());
  for (let hh = 0; hh < 24; hh++) {
    const b = document.createElement("button");
    b.className = "hour";
    b.textContent = pad(hh);
    b.dataset.hh = hh;
    b.onclick = () => selectHour(hh);
    // probe the playlist itself (segments exist even before thumb.jpg is written)
    fetch(hourUrl(currentCam.id, dateStr, hh), { method: "HEAD" })
      .then((r) => { if (r.ok) b.classList.add("has"); else b.classList.add("empty"); })
      .catch(() => b.classList.add("empty"));
    bar.appendChild(b);
  }
}

function selectCam(cam) {
  currentCam = cam;
  document.querySelectorAll(".cam").forEach((el) =>
    el.classList.toggle("sel", el.dataset.id === cam.id));
  $("#camMeta").innerHTML =
    `<b>${cam.name}</b> — ${cam.route} at ${cam.place}, ${cam.county} county` +
    ` <a href="https://www.google.com/maps?q=${cam.lat},${cam.lon}" target="_blank" rel="noreferrer">map</a>`;
  renderHourBar();
  selectHour(new Date().getHours());
}

function selectHour(hh) {
  if (!currentCam) return;
  currentHh = hh;
  segTable = null;   // refetch the playlist for the wall-clock table
  document.querySelectorAll(".hour").forEach((el) =>
    el.classList.toggle("on", +el.dataset.hh === hh));
  const url = hourUrl(currentCam.id, $("#dayPick").value || fmtDate(new Date()), hh);
  playUrl(url);
  ensureSegs().catch(() => { segTable = null; }).finally(tickClock);
}

function playUrl(url) {
  const video = $("#video");
  $("#noSel").style.display = "none";
  if (hls) { hls.destroy(); hls = null; }
  const Lib = window.Hls || window.HLS;   // hls.js UMD global is `Hls`
  if (Lib && Lib.isSupported()) {
    hls = new Lib({ lowLatencyMode: false });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Lib.Events.ERROR, (_e, d) => {
      if (d.fatal && d.details === "manifestLoadError") {
        $("#camMeta").innerHTML += ` <em class="miss">no recording for this hour</em>`;
      }
    });
  } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url; // Safari native HLS
  }
  video.play().catch(() => {});
}

/* ---------------- wall-clock segment table ---------------- */

// Parse an HLS playlist into [{url, start, dur, wall, pdt}] using EXTINF
// durations and #EXT-X-PROGRAM-DATE-TIME tags (written by the recorder's
// program_date_time flag). Segments without an explicit PDT continue from the
// previous segment's wall time; before the first PDT we fall back to the
// hour-directory timestamp (approximate across gaps).
function parseSegments(m3u8Text, baseDir, hourStartMs) {
  const out = [];
  let t = 0, pendingDur = null, pendingPdt = null, prevWallEnd = hourStartMs;
  for (const raw of m3u8Text.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("#EXTINF:")) {
      pendingDur = parseFloat(line.slice(8));
    } else if (line.startsWith("#EXT-X-PROGRAM-DATE-TIME:")) {
      pendingPdt = Date.parse(line.slice(25));  // tag is 25 chars incl. ':'
    } else if (line && !line.startsWith("#")) {
      const dur = Number.isFinite(pendingDur) ? pendingDur : 0;
      const pdt = Number.isFinite(pendingPdt);
      const wall = pdt ? pendingPdt : prevWallEnd;
      out.push({ url: new URL(line, baseDir).href, start: t, dur, wall, pdt });
      t += dur;
      prevWallEnd = wall + dur * 1000;
      pendingDur = null;
      pendingPdt = null;
    }
  }
  return out;
}

let segTable = null;   // {key, segs, exact} — parsed playlist of the loaded hour

async function ensureSegs(force) {
  if (!currentCam || currentHh === null) throw new Error("pick a camera and hour first");
  const dateStr = $("#dayPick").value || fmtDate(new Date());
  const key = `${currentCam.id}|${dateStr}|${currentHh}`;
  if (segTable && segTable.key === key && !force) return segTable;
  const playlistUrl = hourUrl(currentCam.id, dateStr, currentHh);
  const res = await fetch(playlistUrl);
  if (!res.ok) throw new Error(`no recording for this hour (HTTP ${res.status})`);
  const hourStartMs = new Date(`${dateStr}T00:00:00`).getTime() + currentHh * 3600e3;
  const baseDir = playlistUrl.slice(0, playlistUrl.lastIndexOf("/") + 1);
  const segs = parseSegments(await res.text(), baseDir, hourStartMs);
  if (!segs.length) throw new Error("this hour has no segments");
  segTable = { key, segs, exact: segs.some((s) => s.pdt) };
  return segTable;
}

// wall-clock ms for position t (seconds into the joined timeline)
function wallAt(segs, t) {
  const s = segs.find((x) => t < x.start + x.dur) || segs[segs.length - 1];
  return s.wall + Math.max(0, t - s.start) * 1000;
}

const hhmmss = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const stampSecs = (d) => `${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;

function tickClock() {
  const el = $("#clock");
  const video = $("#video");
  if (!segTable || !currentCam || !isFinite(video.currentTime)) {
    el.classList.add("hide");
    return;
  }
  const d = new Date(wallAt(segTable.segs, video.currentTime));
  el.textContent = (segTable.exact ? "" : "≈") + `${fmtDate(d)} ${hhmmss(d)}`;
  el.title = segTable.exact
    ? "recording wall clock"
    : "approximate — this hour predates program-date-time tags; exact for new recordings";
  el.classList.remove("hide");
  updateRangeHint();
}

function updateRangeHint() {
  const hint = $("#exportRangeHint");
  if (!segTable || $("#exportRange").value !== "clip") { hint.textContent = ""; return; }
  const segs = segTable.segs;
  const start = Math.max(0, $("#video").currentTime || 0);
  if (start >= segs[segs.length - 1].start + segs[segs.length - 1].dur) {
    hint.textContent = ""; return;
  }
  const lenMin = Math.max(0.5, Math.min(60, +$("#exportLen").value || 5));
  const w0 = new Date(wallAt(segs, start));
  const w1 = new Date(wallAt(segs, Math.min(start + lenMin * 60,
    segs[segs.length - 1].start + segs[segs.length - 1].dur)));
  hint.textContent = `${segTable.exact ? "" : "≈"}${hhmmss(w0)} → ${hhmmss(w1)}`;
}

/* ---------------- export ---------------- */

function downloadBlob(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
}

const vts = (sec) => {
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60;
  const s = sec - Math.floor(sec / 60) * 60;
  return `${pad(h)}:${pad(m)}:${s.toFixed(3).padStart(6, "0")}`;
};

async function runExport() {
  const btn = $("#exportBtn");
  if (exporting) {  // second click cancels
    exporting = false;
    if (exportAbort) exportAbort.abort();
    return;
  }
  if (!currentCam || currentHh === null) {
    setExportStatus("pick a camera and hour first", true);
    return;
  }
  exporting = true;
  exportAbort = new AbortController();
  btn.classList.add("busy");
  btn.textContent = "⏳ Cancel";
  try {
    const st = await ensureSegs(true);   // fresh playlist: the current hour is still growing
    let segs = st.segs;
    const dateStr = $("#dayPick").value || fmtDate(new Date());
    const hh = currentHh;

    // clip mode: whole segments around the playhead (TS segments are
    // independently playable, so we snap to segment boundaries)
    const isClip = $("#exportRange").value === "clip";
    if (isClip) {
      const lenMin = Math.max(0.5, Math.min(60, +$("#exportLen").value || 5));
      const start = Math.max(0, $("#video").currentTime || 0);
      const end = Math.min(segs[segs.length - 1].start + segs[segs.length - 1].dur,
                           start + lenMin * 60);
      segs = segs.filter((s) => s.start < end && s.start + s.dur > start);
    }

    // exact wall-clock span of what is actually being written (segment-aligned)
    const w0 = new Date(segs[0].wall);
    const w1 = new Date(segs[segs.length - 1].wall + segs[segs.length - 1].dur * 1000);
    const base = isClip
      ? `${currentCam.id}_${fmtDate(w0)}-${pad(hh)}_${stampSecs(w0)}-${stampSecs(w1)}`
      : `${currentCam.id}_${dateStr}-${pad(hh)}`;

    const total = segs.reduce((a, s) => a + s.dur, 0);
    setExportStatus(`downloading 0/${segs.length} segments…`);
    const parts = [];
    let loaded = 0;
    for (let i = 0; i < segs.length; i++) {
      const r = await fetch(segs[i].url, { signal: exportAbort.signal });
      if (!r.ok) throw new Error(`segment ${segs[i].url} failed (HTTP ${r.status})`);
      parts.push(new Uint8Array(await r.arrayBuffer()));
      loaded += segs[i].dur;
      setExportStatus(
        `downloading ${i + 1}/${segs.length} segments (${Math.round(loaded)}s of ${Math.round(total)}s)…`);
    }
    // MPEG-TS concatenation: joining raw .ts chunks yields a valid MPEG-TS file
    const blob = new Blob(parts, { type: "video/mp2t" });
    parts.length = 0;
    downloadBlob(blob, `${base}.ts`);

    if ($("#exportVtt").checked) {
      // WebVTT sidecar with one cue per segment labeled with its wall clock.
      // Named x.clock.vtt on purpose: NOT auto-paired by players (some choke
      // when they auto-load a same-named .vtt next to the .ts) — open it from
      // the player's subtitle menu to get an on-screen recording clock.
      const t0 = segs[0].start;
      const vtt = ["WEBVTT", ""];
      for (const s of segs) {
        const a = s.start - t0, b = a + s.dur;
        const d = new Date(s.wall);
        vtt.push(`${vts(a)} --> ${vts(b)}`, `${fmtDate(d)} ${hhmmss(d)}`, "");
      }
      downloadBlob(new Blob([vtt.join("\n")], { type: "text/vtt" }), `${base}.clock.vtt`);
    }

    setExportStatus(
      `saved ${base}.ts — ${segTable.exact ? "" : "≈"}${hhmmss(w0)} → ${hhmmss(w1)}` +
      ` (${(total / 60).toFixed(1)} min MPEG‑TS, plays in VLC; remux: ffmpeg -i "${base}.ts" -c copy out.mp4)`);
  } catch (e) {
    if (e.name === "AbortError") setExportStatus("export cancelled", true);
    else setExportStatus(String(e.message || e), true);
  } finally {
    exporting = false;
    exportAbort = null;
    btn.classList.remove("busy");
    btn.textContent = "⬇ Export";
    updateRangeHint();
  }
}

function setExportStatus(msg, isError) {
  const el = $("#exportStatus");
  el.textContent = msg;
  el.classList.toggle("err", !!isError);
}

function initExport() {
  $("#exportBtn").onclick = runExport;
  const syncLen = () => {
    const clip = $("#exportRange").value === "clip";
    $("#lenLabel").style.display = clip ? "" : "none";
    updateRangeHint();
  };
  $("#exportRange").onchange = syncLen;
  $("#exportLen").oninput = updateRangeHint;
  syncLen();
}

initExport();
init();
