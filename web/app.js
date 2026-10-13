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
      card.className = "cam";
      card.dataset.id = cam.id;
      card.innerHTML = `${thumbUrl(cam.id, fmtDate(new Date()), new Date().getHours())}
        <span>${cam.name}</span><small>${cam.county}${cam.direction ? " · " + cam.direction : ""}</small>`;
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
  document.querySelectorAll(".hour").forEach((el) =>
    el.classList.toggle("on", +el.dataset.hh === hh));
  const url = hourUrl(currentCam.id, $("#dayPick").value || fmtDate(new Date()), hh);
  playUrl(url);
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

/* ---------------- export ---------------- */

// Parse an HLS playlist into [{url, start, dur}] using EXTINF durations.
function parseSegments(m3u8Text, baseDir) {
  const out = [];
  let t = 0;
  let pendingDur = null;
  for (const raw of m3u8Text.split("\n")) {
    const line = raw.trim();
    if (line.startsWith("#EXTINF:")) {
      pendingDur = parseFloat(line.slice(8));
    } else if (line && !line.startsWith("#")) {
      const dur = Number.isFinite(pendingDur) ? pendingDur : 0;
      out.push({ url: new URL(line, baseDir).href, start: t, dur });
      t += dur;
      pendingDur = null;
    }
  }
  return out;
}

async function runExport() {
  if (!currentCam || currentHh === null) {
    setExportStatus("pick a camera and hour first", true);
    return;
  }
  const btn = $("#exportBtn");
  if (exporting) {  // second click cancels
    exporting = false;
    if (exportAbort) exportAbort.abort();
    return;
  }
  exporting = true;
  exportAbort = new AbortController();
  btn.classList.add("busy");
  btn.textContent = "⏳ Cancel";
  try {
    const dateStr = $("#dayPick").value || fmtDate(new Date());
    const hh = currentHh;
    const playlistUrl = hourUrl(currentCam.id, dateStr, hh);
    const baseDir = playlistUrl.slice(0, playlistUrl.lastIndexOf("/") + 1);
    const res = await fetch(playlistUrl, { signal: exportAbort.signal });
    if (!res.ok) throw new Error(`no recording for this hour (HTTP ${res.status})`);
    let segs = parseSegments(await res.text(), baseDir);
    if (!segs.length) throw new Error("this hour has no segments");

    // clip mode: start at the playhead, default length from the input
    const isClip = $("#exportRange").value === "clip";
    let stamp = "";
    if (isClip) {
      const lenMin = Math.max(1, Math.min(60, +$("#exportLen").value || 5));
      const start = Math.max(0, $("#video").currentTime || 0);
      const end = Math.min(segs[segs.length - 1].start + segs[segs.length - 1].dur,
                           start + lenMin * 60);
      // keep from the segment containing the playhead so the clip opens on a
      // keyframe-aligned chunk (TS segments are independently playable)
      segs = segs.filter((s) => s.start < end && s.start + s.dur > start);
      const d = new Date(`${dateStr}T00:00:00`);
      d.setMinutes(d.getMinutes() + Math.round(start));
      stamp = `-${pad(d.getHours())}${pad(d.getMinutes())}`;
    }

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
    const name = `${currentCam.id}_${dateStr}-${pad(hh)}${stamp}.ts`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
    setExportStatus(`saved ${name} (${Math.round(total / 60)} min, MPEG‑TS — plays in VLC; remux: ffmpeg -i "${name}" -c copy out.mp4)`);
  } catch (e) {
    if (e.name === "AbortError") setExportStatus("export cancelled", true);
    else setExportStatus(String(e.message || e), true);
  } finally {
    exporting = false;
    exportAbort = null;
    btn.classList.remove("busy");
    btn.textContent = "⬇ Export";
  }
}

function setExportStatus(msg, isError) {
  const el = $("#exportStatus");
  el.textContent = msg;
  el.classList.toggle("err", !!isError);
}

function initExport() {
  $("#exportBtn").onclick = runExport;
  const syncLen = () => { $("#lenLabel").style.display =
    $("#exportRange").value === "clip" ? "" : "none"; };
  $("#exportRange").onchange = syncLen;
  syncLen();
}

initExport();
init();
