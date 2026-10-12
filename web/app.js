/* global Hls, CFG */
"use strict";

const $ = (s) => document.querySelector(s);
// local preview: `make site-serve` + caddy on :8090 both work on localhost
if (location.hostname === "localhost") CFG.MEDIA_BASE = "http://localhost:8090";
let catalog = null;
let currentCam = null;
let hls = null;

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

init();
