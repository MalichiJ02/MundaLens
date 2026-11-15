"use strict";
const el = id => document.getElementById(id);
const val = id => el(id).value;
const put = (id, text) => { el(id).textContent = text; };
const clamp = (n, low = 0, high = 100) => Math.max(low, Math.min(high, n));
const coordinates = () => [el("lat").valueAsNumber, el("lon").valueAsNumber];
const pointQuery = () => new URLSearchParams({lat: val("lat"), lon: val("lon")});
const validPoint = () => ["lat", "lon"].every(id => el(id).value !== "" && el(id).checkValidity());
const isoDay = offset => new Date(Date.now() + offset * 86400000).toISOString().slice(0, 10);
const utc = stamp => stamp ? stamp.replace("T", " ").slice(0, 16) + " UTC" : "";
const daysSince = iso => Math.floor((Date.now() - new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso).getTime()) / 86400000);

/* Small element builder. Text always goes in as text, never as HTML. */
function h(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids.flat().filter(kid => kid != null && kid !== false));
  return node;
}
const make = (tag, text) => h(tag, {text});
const chip = (text, kind = "") => h("span", {class: "chip " + kind, text});
const setChip = (id, text, kind) => { const node = el(id); node.textContent = text; node.className = "chip " + kind; };
function sourceLink(title, url) {
  return h("a", {text: title, href: url, target: "_blank", rel: "noopener"});
}
function nodeList(target, lines, title, url) {
  const node = el(target);
  node.replaceChildren(...lines.map(line => make("p", line)));
  if (url) node.append(sourceLink(title, url));
}
const bar = (label, value, kind = "") => h("div", {class: "bar-row"},
  h("span", {class: "bar-label", text: label}),
  h("div", {class: "bar " + kind, role: "img", "aria-label": label + " " + value + " out of 100"}, h("span", {style: "width:" + clamp(value) + "%"})),
  h("strong", {text: String(value)}));
const confidenceKind = level => ({high: "ok", medium: "warn", low: "bad"}[level] || "muted");

const state = {result: null, evidence: {}, config: null, market: {}, rechecked: new Set()};
let marker, googleMap, googleMarker;
let analysisId = 0, evidenceId = 0, profileReady = false, rainId = 0, chartIndex = null;
const STAGE_NAMES = ["Initial", "Development", "Mid-season", "Late"];
const labels = {
  power: "NASA POWER", imerg: "GPM IMERG", "imerg-day": "daily GPM IMERG", smap: "SMAP soil moisture",
  hls: "Landsat vegetation", srtm: "SRTM terrain", soil: "SoilGrids soil"
};

/* ---------- dates and season presets ---------- */
el("start").max = el("end").max = isoDay(0);
function setDates(start, end, preset) {
  el("start").value = start;
  el("end").value = end;
  el("preset-season").setAttribute("aria-pressed", String(preset === "season"));
  el("preset-recent").setAttribute("aria-pressed", String(preset === "recent"));
}
function lastRainySeason() {
  const now = new Date();
  const year = now.getUTCFullYear();
  const lat = validPoint() ? coordinates()[0] : -15;
  if (lat >= 8) {  // northern rains: May to October
    const done = now > new Date(Date.UTC(year, 9, 31));
    const y = done ? year : year - 1;
    return [y + "-05-01", y + "-10-31"];
  }
  const done = now > new Date(Date.UTC(year, 3, 30));  // southern rains: November to April
  const y = done ? year : year - 1;
  return [(y - 1) + "-11-01", y + "-04-30"];
}
const recentWindow = () => setDates(isoDay(-182), isoDay(-4), "recent");
el("preset-season").onclick = () => { setDates(...lastRainySeason(), "season"); markStale(); };
el("preset-recent").onclick = () => { recentWindow(); markStale(); };
for (const id of ["start", "end"]) el(id).addEventListener("input", () => setDates(val("start"), val("end"), ""));
recentWindow();

/* ---------- map ---------- */
const map = L.map("farm-map", {worldCopyJump: true}).setView([12, 12], 2);
const satellite = L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 19, attribution: "Imagery © Esri and contributing providers"
});
const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
});
satellite.addTo(map);
const markerAt = (lat, lon, zoom) => {
  if (!marker) {
    marker = L.marker([lat, lon], {draggable: true, keyboard: true, title: "Drag to move your farm pin"}).addTo(map);
    marker.on("dragend", () => { const p = marker.getLatLng(); choosePoint(p.lat, p.lng); });
  }
  marker.setLatLng([lat, lon]);
  if (zoom) map.setView([lat, lon], zoom);
  if (googleMap) {
    const position = {lat, lng: lon};
    googleMarker.setPosition(position);
    if (zoom) {
      googleMap.setCenter(position);
      googleMap.setZoom(zoom);
    }
  }
};
function choosePoint(lat, lon, zoom) {
  el("lat").value = clamp(lat, -85, 85).toFixed(5);
  el("lon").value = L.Util.wrapNum(lon, [-180, 180], true).toFixed(5);
  markerAt(...coordinates(), zoom);
  put("location-status", "Farm pin · " + val("lat") + ", " + val("lon"));
  if (profileReady) el("farm").requestSubmit();
  refresh();
}
map.on("click", e => choosePoint(e.latlng.lat, e.latlng.lng));
el("basemap").onchange = () => {
  const selected = val("basemap");
  if (selected === "google" && googleMap) {
    el("google-map").hidden = false;
    el("farm-map").hidden = true;
    if (validPoint()) markerAt(...coordinates(), 13);
    return;
  }
  if (el("google-map")) el("google-map").hidden = true;
  el("farm-map").hidden = false;
  map.removeLayer(satellite).removeLayer(streets);
  (selected === "satellite" ? satellite : streets).addTo(map);
  map.invalidateSize();
};
el("locate").onclick = () => {
  if (!navigator.geolocation) {
    put("location-status", "Location is unavailable. Search for a place or pin your farm on the map.");
    return;
  }
  put("location-status", "Waiting for your device location…");
  navigator.geolocation.getCurrentPosition(
    position => choosePoint(position.coords.latitude, position.coords.longitude, 14),
    () => put("location-status", "Device location is unavailable. Search for a place, pin your farm or enter coordinates."),
    {enableHighAccuracy: true, timeout: 12000}
  );
};
el("demo").onclick = () => {
  setDates("2024-11-01", "2025-04-30", "");
  el("irrigation").value = "rainfed";
  choosePoint(-15.4, 28.3, 13);
};
for (const id of ["lat", "lon"]) {
  el(id).onchange = () => {
    if (validPoint()) choosePoint(...coordinates(), 13);
  };
}

/* ---------- place search ---------- */
el("place-form").onsubmit = async event => {
  event.preventDefault();
  const query = val("place").trim();
  const list = el("place-results");
  if (query.length < 3) { put("location-status", "Type at least three letters to search for a place."); return; }
  put("location-status", "Searching for “" + query + "”…");
  try {
    const response = await fetch("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q=" + encodeURIComponent(query),
      {headers: {"Accept": "application/json"}});
    if (!response.ok) throw Error("search");
    const found = await response.json();
    if (!found.length) { list.hidden = true; put("location-status", "No place found. Try a nearby town or pin the map."); return; }
    list.replaceChildren(...found.map(place => h("li", {}, h("button", {type: "button", text: place.display_name,
      onclick: () => { list.hidden = true; choosePoint(Number(place.lat), Number(place.lon), 13); }}))));
    list.hidden = false;
    put("location-status", "Choose the matching place below.");
  } catch {
    put("location-status", "Place search is unavailable. Pin your farm on the map instead.");
  }
};

/* ---------- requests ---------- */
async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Check your dates and crop selections.");
  return data;
}

/* ---------- crop selectors ---------- */
const cropIds = ["previous", "alternative-a", "alternative-b"];
function syncCrops() {
  const chosen = cropIds.map(val);
  for (const id of cropIds) {
    for (const option of el(id).options) {
      option.disabled = chosen.some((crop, i) => crop === option.value && cropIds[i] !== id);
    }
  }
}
for (const id of cropIds) el(id).addEventListener("change", () => { syncCrops(); renderMarketRows(); });

function markStale() {
  if (!state.result) return;
  el("stale").hidden = false;
  for (const id of ["run", "run-float"]) el(id).classList.add("attention");
}
for (const id of ["previous", "alternative-a", "alternative-b", "irrigation", "soil", "priority", "soil-ph", "rain-source", "past-seasons", "start", "end"]) {
  el(id).addEventListener("change", markStale);
}
function clearStale() {
  el("stale").hidden = true;
  for (const id of ["run", "run-float"]) el(id).classList.remove("attention");
}

/* ---------- answer ---------- */
function verdictView(ranking, history) {
  const best = ranking.recommended[0];
  const current = ranking.current;
  const previous = val("previous");
  if (!best) return h("div");
  const better = best.score - current.score;
  const reason = best.reasons.find(text => /fixes nitrogen|can use the nitrogen|helps break/.test(text)) || best.reasons[0];
  const concern = best.warnings.find(text => /shortfall|above|well outside|swing/.test(text));
  const basis = ranking.method.ranked_on === "history"
    ? "Based on " + ranking.method.seasons_used + " seasons of weather for the same dates."
    : "Based on the selected season only.";
  return h("div", {class: "verdict"},
    h("div", {class: "eyebrow", text: "Best next crop for this farm"}),
    h("div", {class: "headline", text: best.crop + " after " + previous + " · " + best.score + "/100"}),
    h("p", {text: better > 0
      ? "That is " + better + " points ahead of planting " + previous + " again (" + current.score + "/100)."
      : "Planting " + previous + " again scores about the same (" + current.score + "/100), so rotation gives you little extra here."}),
    reason ? h("p", {text: reason}) : null,
    concern ? h("p", {text: "Watch out: " + concern}) : null,
    h("p", {}, chip(best.confidence + " confidence"), " ", chip(basis)));
}
function assumptionChips(inputs, ranking) {
  const m = ranking.method;
  const items = [];
  items.push(inputs.soil ? chip("Soil: " + inputs.soil.texture + ", " + m.awc_mm_per_m + " mm/m", "ok") : chip("Soil: assumed loam", "warn"));
  if (m.ph != null) items.push(chip("pH " + m.ph + (m.ph_source === "estimate" ? " (estimate)" : ""), "muted"));
  items.push(chip("Evaporation: " + ({"penman-monteith": "Penman-Monteith", mixed: "Penman-Monteith + Hargreaves", hargreaves: "Hargreaves (temperature only)"}[m.et0]), m.et0 === "penman-monteith" ? "ok" : "warn"));
  items.push(chip("Rain: " + (inputs.rain_source === "chirps" ? "CHIRPS" : "NASA POWER"), "muted"));
  if (m.slope != null) items.push(chip("Slope " + Number(m.slope).toFixed(1) + "°", "muted"));
  items.push(chip(m.seasons_used + (m.seasons_used === 1 ? " season" : " seasons") + " compared", m.ranked_on === "history" ? "ok" : "warn"));
  if (m.calibrated) items.push(chip("Locally calibrated", "ok"));
  return items;
}
function rankingView(ranking) {
  const chosen = new Set(cropIds.map(val));
  const rows = ranking.ranked.map(item => {
    const isCurrent = item.crop === val("previous");
    const picked = chosen.has(item.crop);
    return h("div", {class: "rank-row" + (isCurrent ? " current" : "")},
      h("span", {class: "num", text: "#" + item.rank}),
      h("span", {text: item.crop + (isCurrent ? " (current)" : "")}),
      h("div", {class: "bar", role: "img", "aria-label": item.crop + " score " + item.score + " out of 100"}, h("span", {style: "width:" + clamp(item.score) + "%"})),
      h("strong", {text: String(item.score)}),
      isCurrent || picked ? h("span", {class: "chip muted", text: isCurrent ? "Baseline" : "Comparing"})
        : h("button", {type: "button", class: "secondary", text: "Compare", "aria-label": "Compare " + item.crop,
          onclick: () => useAlternative(item.crop)}));
  });
  return h("div", {class: "rank-list"}, h("h3", {text: "All crops ranked for this farm"}), ...rows);
}
function useAlternative(crop) {
  const scores = Object.fromEntries(state.result.rotation.ranking.ranked.map(item => [item.crop, item.score]));
  const [a, b] = ["alternative-a", "alternative-b"];
  const target = scores[val(a)] <= scores[val(b)] ? a : b;
  el(target).value = crop;
  syncCrops();
  renderMarketRows();
  el("farm").requestSubmit();
}
function optionCard(option, index, best) {
  const a = option.assessment;
  const card = h("article", {class: "card" + (best ? " best" : "")});
  card.append(h("div", {class: "card-head"}, h("h3", {text: option.sequence.join(" → ")}), chip(index ? "Alternative " + index : "Current crop", "muted")));
  if (!a) return card;
  card.append(
    h("p", {class: "big"}, String(a.score), h("small", {text: " / 100"})),
    h("p", {}, chip(a.confidence + " confidence", confidenceKind(a.confidence))),
    bar("Water and heat", a.water_score, a.water_score < 40 ? "clay" : a.water_score < 60 ? "gold" : ""),
    bar("Rotation", a.rotation_score, a.rotation_score < 40 ? "clay" : ""));
  if (a.seasons_used > 1) card.append(h("p", {text: "Typical year " + a.score_median + " · bad year " + a.score_bad_year + " (" + a.seasons_used + " seasons)."}));
  card.append(h("p", {text: "Modelled yield about " + a.rel_yield_pct + "% of potential this season. Water shortfall " + a.water_deficit_mm + " mm of " + a.water_demand_mm + " mm needed."}));
  if (a.warnings.length) card.append(h("ul", {class: "warn-line"}, ...a.warnings.slice(0, 3).map(text => make("li", text))));
  const extras = [...a.reasons, ...a.warnings.slice(3)];
  const water = [
    "Typical crop water need · " + option.water_mm.join("–") + " mm over the growing period.",
    "Sensitivity to drought · " + option.sensitivity + ". Water matters most during " + option.stage + ".",
    val("priority") === "water" ? option.water_context : null,
    val("priority") === "cover" ? (option.legume ? "Check whether the local variety and planting calendar can close a gap in soil cover." : "Check how long this rotation leaves the ground uncovered after harvest.") : null,
    val("priority") === "market" ? "Check local buyers, prices and costs for " + option.crop.toLowerCase() + "." : null
  ].filter(Boolean);
  card.append(h("details", {}, h("summary", {text: "Why this score"}), h("ul", {}, ...[...extras, ...water].map(text => make("li", text)))));
  return card;
}
function answerView() {
  if (!state.result) return;
  const {history, rotation, inputs} = state.result;
  const ranking = rotation.ranking;
  const drought = history.longest;
  const recent = state.evidence["imerg-day"];
  put("rotation-context", (drought
    ? "In the dates you selected, the longest run below 1 mm of rain per day lasted " + drought.days + " days."
    : "Rainfall coverage for this period determines which historical patterns can be compared.")
    + (recent?.value != null ? " GPM estimated " + recent.value + " mm for the latest available full day (" + recent.observed + ")." : ""));
  el("verdict").replaceChildren(verdictView(ranking, history));
  el("assumptions").replaceChildren(...assumptionChips(inputs, ranking));
  el("checks").replaceChildren(...[...(ranking.checks || []), ...inputs.notes.map(text => ({level: "", text}))]
    .map(item => h("div", {class: "check " + (item.level === "warn" ? "warn" : ""), text: item.text})));
  el("ranking").replaceChildren(rankingView(ranking));
  const bestCrop = ranking.recommended[0]?.crop;
  el("rotations").replaceChildren(...rotation.scenarios.map((option, index) => optionCard(option, index, option.crop === bestCrop)));
  let context = "Historical rainfall is a screening measure. Confirm the growing period, soil drainage and variety locally.";
  if (val("irrigation") === "irrigated") context += " Include available irrigation water when assessing each option.";
  if (val("irrigation") === "rainfed") context += " For rainfed crops, check how the historical dry spell overlaps each crop’s growth stages.";
  if (val("soil") !== "unknown") context += " Your field observation recorded " + val("soil") + " soil.";
  if (val("notes").trim()) context += " Your notes: " + val("notes").trim();
  el("rotation-notes").replaceChildren(h("p", {text: context}), ...ranking.notes.map(text => make("p", text)),
    sourceLink("FAO crop water needs", rotation.water_source), " · ", sourceLink("FAO water-sensitive stages", rotation.stage_source));
  for (const id of ["share", "print"]) el(id).disabled = false;
  updateShare();
}
function summaryText() {
  const {history, rotation} = state.result;
  const r = rotation.ranking;
  const best = r.recommended[0];
  const lines = ["MundaLens · " + val("lat") + ", " + val("lon") + " · " + val("start") + " to " + val("end"),
    "Best next crop: " + best.crop + " after " + val("previous") + " (" + best.score + "/100, " + best.confidence + " confidence)",
    "Current crop " + val("previous") + ": " + r.current.score + "/100",
    ...rotation.scenarios.slice(1).map(option => option.crop + ": " + option.assessment.score + "/100"),
    "Rainfall " + (history.total ?? "n/a") + " mm, longest dry spell " + (history.longest ? history.longest.days + " days" : "none"),
    "Screening aid only. Confirm varieties, planting dates and soil locally."];
  if (val("notes").trim()) lines.push("Notes: " + val("notes").trim());
  return lines.join("\n");
}
function updateShare() {
  const link = el("whatsapp");
  link.href = "https://wa.me/?text=" + encodeURIComponent(summaryText());
  link.hidden = false;
}
el("share").onclick = async () => {
  try {
    await navigator.clipboard.writeText(summaryText());
    put("status", "Summary copied. Paste it into a message or note.");
  } catch {
    put("status", "Copy is blocked in this browser. Use Print or save PDF instead.");
  }
};
el("print").onclick = () => window.print();

/* ---------- rainfall chart ---------- */
function drawChart(data) {
  const metrics = [
    [data.total === null ? "Unavailable" : data.total + " mm", "Rainfall · " + data.valid + "/" + data.days.length + " days"],
    [data.longest ? data.longest.days + " days" : data.valid ? "0 days" : "Unavailable", "Longest spell below 1 mm/day"],
    [data.peak === null ? "Unavailable" : data.peak + " °C", "Peak daily air temperature"]
  ];
  el("metrics").replaceChildren(...metrics.map(([value, caption]) => h("div", {class: "metric"}, h("strong", {text: value}), h("small", {text: caption}))));

  const svgNS = "http://www.w3.org/2000/svg";
  const node = (tag, attrs = {}, text) => {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  };
  const W = 720, H = 230, left = 38, right = 12, top = 16, bottom = 28;
  const plotW = W - left - right, plotH = H - top - bottom;
  const n = data.days.length;
  const step = plotW / n;
  const max = Math.max(5, ...data.days.map(day => day.rain ?? 0));
  const yOf = value => top + plotH - value / max * plotH;
  const svg = node("svg", {viewBox: "0 0 " + W + " " + H, class: "chart", role: "group", tabindex: "0",
    "aria-label": "Daily rainfall in millimetres with a cumulative line. Gold marks the longest dry spell, grey marks missing days. Use the arrow keys to read each day."});

  // growth stages of the chosen crop from the assumed planting day
  const crop = val("stage-crop");
  const stages = state.config?.stages?.[crop];
  const planting = state.result?.rotation.ranking.planting_date;
  const plantIndex = planting ? data.days.findIndex(day => day.date === planting) : -1;
  if (stages && plantIndex >= 0) {
    let from = plantIndex;
    stages.forEach((length, i) => {
      const to = Math.min(n, from + length);
      if (from < n) {
        svg.append(node("rect", {x: left + from * step, y: top, width: Math.max(0, (to - from) * step), height: plotH, fill: ["#eef4ef", "#e0ece4", "#d3e5da", "#e6efe8"][i]}));
        if ((to - from) * step > 46) svg.append(node("text", {x: left + from * step + 3, y: top + 10, "font-size": 9, fill: "#52675d"}, STAGE_NAMES[i]));
      }
      from = to;
    });
  }
  // gridlines and y labels
  for (const fraction of [0, .5, 1]) {
    const y = yOf(max * fraction);
    svg.append(node("line", {x1: left, x2: W - right, y1: y, y2: y, stroke: "#d4e1d8", "stroke-width": .6}));
    svg.append(node("text", {x: left - 4, y: y + 3, "text-anchor": "end", "font-size": 9, fill: "#52675d"}, Math.round(max * fraction)));
  }
  svg.append(node("text", {x: 2, y: 10, "font-size": 9, fill: "#52675d"}, "mm/day"));
  // bars
  const cumulative = [];
  let running = 0;
  data.days.forEach((day, i) => {
    running += day.rain ?? 0;
    cumulative.push(running);
    const dry = data.longest && day.date >= data.longest.start && day.date <= data.longest.end;
    const height = Math.max(dry || day.rain === null ? 5 : 1.5, (day.rain ?? 0) / max * plotH);
    svg.append(node("rect", {x: left + i * step, y: top + plotH - height, height, width: Math.max(.5, step - .6),
      fill: dry ? "#ad7125" : day.rain === null ? "#9ba5a0" : "#287260"}));
  });
  // month ticks
  data.days.forEach((day, i) => {
    if (day.date.endsWith("-01") || i === 0) {
      const label = new Date(day.date + "T00:00:00Z").toLocaleString("en", {month: "short", timeZone: "UTC"});
      svg.append(node("line", {x1: left + i * step, x2: left + i * step, y1: top + plotH, y2: top + plotH + 4, stroke: "#52675d", "stroke-width": .8}));
      svg.append(node("text", {x: left + i * step + 2, y: H - 12, "font-size": 9, fill: "#52675d"}, label));
    }
  });
  // cumulative line (scaled to its own total)
  const total = cumulative[n - 1] || 1;
  svg.append(node("polyline", {fill: "none", stroke: "#0f3d33", "stroke-width": 1.6, "stroke-linejoin": "round",
    points: cumulative.map((c, i) => (left + (i + .5) * step).toFixed(1) + "," + (top + plotH - c / total * plotH).toFixed(1)).join(" ")}));
  svg.append(node("text", {x: W - right - 2, y: top + 10, "text-anchor": "end", "font-size": 9, fill: "#0f3d33", "font-weight": 700}, "Cumulative " + Math.round(total) + " mm"));
  // planting marker and dry-spell label
  if (plantIndex >= 0) {
    const x = left + plantIndex * step;
    svg.append(node("line", {x1: x, x2: x, y1: top, y2: top + plotH, stroke: "#12392f", "stroke-width": 1.2, "stroke-dasharray": "4 3"}));
    svg.append(node("text", {x: x + 3, y: top + plotH - 4, "font-size": 9, fill: "#12392f", "font-weight": 700}, "Assumed planting"));
  }
  if (data.longest) {
    const i = data.days.findIndex(day => day.date === data.longest.start);
    svg.append(node("text", {x: Math.min(W - 150, left + i * step), y: top + plotH - 22, "font-size": 9, fill: "#7a4c10", "font-weight": 700}, "Longest dry spell · " + data.longest.days + " days"));
  }
  // reading cursor
  const cursor = node("line", {y1: top, y2: top + plotH, stroke: "#12392f", "stroke-width": 1, visibility: "hidden"});
  svg.append(cursor);
  const readout = el("chart-readout");
  const show = i => {
    i = clamp(i, 0, n - 1, 0);
    chartIndex = i;
    cursor.setAttribute("x1", left + (i + .5) * step);
    cursor.setAttribute("x2", left + (i + .5) * step);
    cursor.setAttribute("visibility", "visible");
    const day = data.days[i];
    readout.textContent = day.date + " · " + (day.rain === null ? "rain unavailable" : day.rain + " mm") + " · " + Math.round(cumulative[i]) + " mm so far"
      + (day.heat != null ? " · max " + day.heat + " °C" : "");
  };
  const fromEvent = event => {
    const box = svg.getBoundingClientRect();
    show(Math.floor(((event.clientX - box.left) / box.width * W - left) / step));
  };
  svg.addEventListener("pointermove", fromEvent);
  svg.addEventListener("pointerdown", fromEvent);
  svg.addEventListener("keydown", event => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      show((chartIndex ?? (event.key === "ArrowRight" ? -1 : n)) + (event.key === "ArrowRight" ? 1 : -1));
    }
  });
  el("chart").replaceChildren(svg, make("p", "Green bars are daily rain, gold is the longest dry spell and grey marks missing days. The dark line adds the rain up."));
  el("source").replaceChildren(sourceLink(data.source, data.url));
  if (data.longest) el("source").append(" · " + data.longest.start + " to " + data.longest.end);
}
el("stage-crop").onchange = () => { if (state.result) drawChart(state.result.history); };

/* ---------- analysis ---------- */
function loading() {
  el("verdict").replaceChildren(h("div", {class: "skeleton"}));
  el("assumptions").replaceChildren();
  el("checks").replaceChildren();
  el("ranking").replaceChildren(h("div", {class: "skeleton line"}), h("div", {class: "skeleton line"}), h("div", {class: "skeleton line"}));
  el("rotations").replaceChildren(h("div", {class: "skeleton"}), h("div", {class: "skeleton"}), h("div", {class: "skeleton"}));
  el("rotation-notes").replaceChildren();
  for (const id of ["metrics", "chart", "source"]) el(id).replaceChildren();
}
function evidenceBody() {
  const out = {};
  const smap = state.evidence.smap, hls = state.evidence.hls;
  if (smap?.value != null && smap.observed) out.smap = {value: smap.value, observed: smap.observed};
  if (hls?.ndvi != null && hls.observed) out.hls = {ndvi: hls.ndvi, observed: hls.observed};
  return out;
}
el("farm").onsubmit = async event => {
  event.preventDefault();
  const id = ++analysisId;
  if (!profileReady) return;
  if (!validPoint()) {
    put("status", "Choose your farm first: search for a place, use the map or enter coordinates.");
    return;
  }
  if (new Set(cropIds.map(val)).size !== 3) {
    put("status", "Choose three different crops.");
    return;
  }
  if (!val("start") || !val("end") || val("start") > val("end")) {
    put("status", "Choose a start date before the end date.");
    return;
  }
  markerAt(...coordinates());
  const [lat, lon] = coordinates();
  const body = {lat, lon, start: val("start"), end: val("end"),
    rain_source: val("rain-source"), past_seasons: Number(val("past-seasons")), evidence: evidenceBody(),
    rotation: {previous: val("previous"), alternatives: [val("alternative-a"), val("alternative-b")],
      irrigation: val("irrigation"), priority: val("priority"), soil: val("soil"), local_notes: val("notes"),
      soil_ph: val("soil-ph") === "" ? null : Number(val("soil-ph"))}};
  state.result = null;
  clearStale();
  loading();
  for (const button of ["share", "print"]) el(button).disabled = true;
  el("whatsapp").hidden = true;
  put("rotation-context", "Loading the comparison for this farm…");
  put("status", "Fetching rainfall and soil data. Comparing past seasons can take up to a minute.");
  try {
    const result = await request("/api/farm/analysis", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (id !== analysisId) return;
    state.result = result;
    el("stage-crop").value = val("previous");
    drawChart(result.history);
    answerView();
    renderMarketRows();
    put("status", "Rainfall history and rotation comparison loaded.");
    runRainCheck(id);
    updateSoilCard(result.inputs.soil);
  } catch (error) {
    if (id !== analysisId) return;
    for (const target of ["verdict", "ranking", "rotations"]) el(target).replaceChildren();
    put("status", error.message);
    put("rotation-context", "The comparison is unavailable. Check the inputs and try again.");
  }
};
el("run-float").onclick = () => el("farm").requestSubmit();

/* ---------- soil and rainfall cross-check cards ---------- */
function updateSoilCard(soil) {
  if (!soil) {
    setChip("soil-chip", "Assumed", "warn");
    nodeList("soil-status", ["Soil texture could not be estimated here, so a loam water capacity is assumed. Enter your soil pH if you know it."]);
    return;
  }
  setChip("soil-chip", "Estimate", "ok");
  nodeList("soil-status", [
    soil.texture + " · " + soil.sand_pct + "% sand, " + soil.silt_pct + "% silt, " + soil.clay_pct + "% clay",
    "Holds about " + soil.awc_mm_per_m + " mm of usable water per metre" + (soil.ph != null ? " · pH " + soil.ph : ""),
    soil.status], "SoilGrids", soil.source);
}
async function runRainCheck(analysis) {
  const id = ++rainId;
  setChip("rain-chip", "Checking", "muted");
  el("rain-action").replaceChildren();
  put("rain-status", "Comparing NASA POWER with CHIRPS rainfall…");
  const query = new URLSearchParams({lat: val("lat"), lon: val("lon"), start: val("start"), end: val("end")});
  try {
    const data = await request("/api/farm/rain-check?" + query);
    if (id !== rainId || analysis !== analysisId) return;
    if (data.days == null) {
      setChip("rain-chip", "Unavailable", "muted");
      nodeList("rain-status", [data.status]);
      return;
    }
    setChip("rain-chip", {close: "Agree", moderate: "Differ", weak: "Disagree"}[data.agreement], {close: "ok", moderate: "warn", weak: "bad"}[data.agreement]);
    nodeList("rain-status", [
      "NASA POWER " + data.power_mm + " mm · CHIRPS " + data.chirps_mm + " mm over " + data.days + " days (" + (data.difference_pct > 0 ? "+" : "") + data.difference_pct + "%).",
      data.status], "CHIRPS", data.source);
    if (data.agreement !== "close" && val("rain-source") !== "chirps") {
      el("rain-action").replaceChildren(h("button", {type: "button", class: "secondary", text: "Use CHIRPS rainfall",
        onclick: () => { el("rain-source").value = "chirps"; el("farm").requestSubmit(); }}));
    }
  } catch (error) {
    if (id === rainId) { setChip("rain-chip", "Unavailable", "muted"); put("rain-status", "The rainfall cross-check is unavailable right now."); }
  }
}

/* ---------- evidence cards ---------- */
function ageChip(chipId, iso, freshDays, freshLabel) {
  const days = daysSince(iso);
  if (days <= freshDays) setChip(chipId, freshLabel, "ok");
  else setChip(chipId, days + " days old", days > 30 ? "warn" : "ok");
}
const needsServer = status => /Earthdata|server-side|needs NASA/i.test(status || "");
function refresh() {
  if (!validPoint()) return;
  el("refresh").disabled = false;
  const id = ++evidenceId;
  const query = pointQuery().toString();
  const endpoints = {power: "/api/live/conditions", imerg: "/api/live/imerg-map",
    "imerg-day": "/api/farm/satellite/imerg", smap: "/api/farm/satellite/smap", hls: "/api/farm/satellite/hls",
    srtm: "/api/farm/satellite/srtm", soil: "/api/farm/soil"};
  state.evidence = {};
  el("imerg-map").replaceChildren();
  el("hls-image").replaceChildren();
  for (const chipId of ["power", "imerg", "smap", "hls", "srtm", "soil"]) setChip(chipId + "-chip", "Checking", "muted");
  for (const [name, endpoint] of Object.entries(endpoints)) {
    put(name + "-status", "Checking " + labels[name] + "…");
    request(endpoint + "?" + query).then(data => {
      if (id !== evidenceId) return;
      state.evidence[name] = data;
      if (name === "power") {
        const lines = [];
        if (data.rain) lines.push(data.rain.rain + " mm rain · " + data.rain.date);
        if (data.heat) lines.push(data.heat.heat + " °C maximum · " + data.heat.date);
        if (data.week) lines.push(data.week.total + " mm across " + data.week.days + " of 7 days through " + data.week.end);
        nodeList("power-status", lines, "NASA POWER request", data.url);
        const latest = data.rain?.date || data.heat?.date;
        if (latest) ageChip("power-chip", latest, 3, "Up to date"); else setChip("power-chip", "No data", "warn");
      } else if (name === "imerg") {
        nodeList("imerg-status", [utc(data.published_time) + " · Area precipitation rate."], "NASA GPM IMERG", "https://gpm.nasa.gov/data/imerg");
        const hours = (Date.now() - new Date(data.published_time).getTime()) / 3600000;
        setChip("imerg-chip", hours <= 4 ? "Live" : hours <= 48 ? Math.round(hours) + " h old" : Math.round(hours / 24) + " days old", hours <= 4 ? "ok" : "warn");
        const image = Object.assign(new Image(), {src: data.image_url, alt: "NASA map of precipitation around the farm pin"});
        image.onerror = () => { if (id === evidenceId) el("imerg-map").replaceChildren(make("p", "NASA's map image was unavailable.")); };
        el("imerg-map").append(image, sourceLink("Rainfall map legend", data.legend_url));
      } else if (name === "imerg-day") {
        const lines = [];
        if (data.value != null) lines.push(data.value + " mm total · " + data.observed);
        lines.push(needsServer(data.status) ? "Numerical daily rainfall is not enabled on this server. The map above still works." : data.status);
        nodeList("imerg-day-status", lines);
      } else if (name === "soil") {
        updateSoilCard(data.texture ? data : null);
        if (!data.texture) nodeList("soil-status", [data.status || "Soil texture is unavailable here."]);
      } else {
        const lines = [];
        const unavailable = name === "smap" && data.value == null && needsServer(data.status);
        if (data.value != null) lines.push(data.value.toFixed(3) + " m³/m³ root-zone moisture");
        if (data.ndvi != null) lines.push("NDVI " + data.ndvi.toFixed(3) + (data.pixels_used > 1 ? " (median of " + data.pixels_used + " pixels around the pin)" : ""));
        if (data.slope != null) lines.push(data.elevation + " m elevation · " + data.slope.toFixed(1) + "° slope");
        if (data.observed && !unavailable) lines.push((name === "smap" && data.value == null ? "Latest granule · " : "Observed · ") + utc(data.observed));
        lines.push(unavailable ? "Soil moisture is not enabled on this server." : data.status);
        nodeList(name + "-status", lines, labels[name] + " source", data.source);
        if (name === "smap") {
          if (unavailable) setChip("smap-chip", "Not enabled", "muted");
          else if (data.value != null) ageChip("smap-chip", data.observed, 5, "Latest");
          else setChip("smap-chip", "No data", "warn");
        }
        if (name === "srtm") setChip("srtm-chip", data.slope != null ? "Static" : "Unavailable", data.slope != null ? "ok" : "muted");
        if (name === "hls") {
          if (data.ndvi != null) ageChip("hls-chip", data.observed, 10, "Recent");
          else if (data.archive) setChip("hls-chip", "Archive only", "warn");
          else setChip("hls-chip", "No clear view", "warn");
        }
        if (name === "hls" && data.archive) {
          const archive = data.archive;
          el("hls-image").append(make("p", "Archived Landsat 9 scene · " + archive.observed +
            " · Median NDVI " + archive.ndvi_median.toFixed(3) + " across " + archive.clear_pixels +
            " valid pixels out of " + archive.pixels + " around the pin."),
            sourceLink("Landsat 9 reference", archive.source));
        }
        if (name === "hls" && data.image_url) {
          const image = Object.assign(new Image(), {src: data.image_url, alt: "Landsat scene around the farm pin"});
          image.onerror = () => image.remove();
          el("hls-image").append(image);
        }
        maybeRecheck(name, data, id);
      }
      if (state.result) answerView();
    }).catch(error => {
      if (id !== evidenceId) return;
      put(name + "-status", error.message);
      const chipId = name === "imerg-day" ? null : name + "-chip";
      if (chipId) setChip(chipId, "Unavailable", "muted");
    });
  }
}
/* The server compares SMAP and Landsat with the water balance. Send them once they arrive and are relevant. */
function maybeRecheck(name, data, id) {
  if (!state.result || state.rechecked.has(id + name)) return;
  const end = new Date(val("end") + "T00:00:00Z"), start = new Date(val("start") + "T00:00:00Z");
  let relevant = false;
  if (name === "smap" && data.value != null && data.observed) relevant = Math.abs(new Date(data.observed) - end) <= 10 * 86400000;
  if (name === "hls" && data.ndvi != null && data.observed) { const seen = new Date(data.observed); relevant = seen >= start && seen <= end; }
  if (!relevant) return;
  state.rechecked.add(id + name);
  setTimeout(() => { if (id === evidenceId) el("farm").requestSubmit(); }, 800);
}
el("refresh").onclick = refresh;
setInterval(() => { if (!document.hidden) refresh(); }, 15 * 60 * 1000);

/* ---------- market scenario ---------- */
function renderMarketRows() {
  const crops = cropIds.map(val).filter(Boolean);
  const body = el("market-rows");
  if (!crops.length) return;
  body.replaceChildren(...crops.map(crop => {
    const saved = state.market[crop] || {};
    const row = h("tr", {"data-crop": crop}, h("td", {text: crop}));
    for (const field of ["price", "quantity", "cost"]) {
      row.append(h("td", {}, h("input", {type: "number", min: "0", step: "any", "aria-label": crop + " " + field, value: saved[field] ?? "",
        oninput: event => { (state.market[crop] ||= {})[field] = event.target.value; updateMargins(); }})));
    }
    row.append(h("td", {class: "margin", text: "–"}));
    return row;
  }));
  updateMargins();
}
function updateMargins() {
  const rows = [...el("market-rows").children];
  const margins = rows.map(row => {
    const saved = state.market[row.dataset.crop] || {};
    const [price, quantity, cost] = ["price", "quantity", "cost"].map(field => Number(saved[field]));
    const ready = ["price", "quantity", "cost"].every(field => saved[field] !== undefined && saved[field] !== "") && [price, quantity, cost].every(v => Number.isFinite(v) && v >= 0);
    return ready ? price * quantity - cost : null;
  });
  const best = Math.max(...margins.filter(v => v !== null), -Infinity);
  rows.forEach((row, i) => {
    row.lastChild.textContent = margins[i] === null ? "–" : val("currency") + " " + margins[i].toFixed(2);
    row.classList.toggle("top", margins[i] !== null && margins[i] === best && margins.filter(v => v !== null).length > 1);
  });
  const ready = margins.filter(v => v !== null).length;
  put("margin", ready ? "Gross margin is price × quantity − direct costs. This is your own scenario, not a forecast." : "Enter a price, quantity and direct costs for at least one crop.");
}
el("currency").onchange = updateMargins;

/* ---------- Google basemap (optional) ---------- */
function googleOption(key) {
  const surface = h("div", {id: "google-map", hidden: true});
  el("farm-map").after(surface);
  window.mundaGoogleReady = () => {
    googleMap = new google.maps.Map(surface, {center: {lat: 12, lng: 12}, zoom: 2, mapTypeId: "satellite"});
    googleMarker = new google.maps.Marker({map: googleMap, draggable: true});
    googleMap.addListener("click", event => choosePoint(event.latLng.lat(), event.latLng.lng()));
    googleMarker.addListener("dragend", event => choosePoint(event.latLng.lat(), event.latLng.lng()));
    el("basemap").add(new Option("Google Satellite", "google"));
  };
  const query = new URLSearchParams({key, loading: "async", callback: "mundaGoogleReady"});
  document.head.append(Object.assign(document.createElement("script"), {
    src: "https://maps.googleapis.com/maps/api/js?" + query, async: true
  }));
}

/* ---------- start ---------- */
request("/api/config").then(data => {
  state.config = data;
  cropIds.forEach((id, i) => {
    data.crops.forEach(crop => el(id).add(new Option(crop, crop)));
    el(id).selectedIndex = i;
  });
  data.crops.forEach(crop => el("stage-crop").add(new Option(crop, crop)));
  syncCrops();
  renderMarketRows();
  profileReady = true;
  for (const id of ["run", "run-float"]) el(id).disabled = false;
  if (validPoint()) el("farm").requestSubmit();
  if (data.google_maps_key) googleOption(data.google_maps_key);
}).catch(error => put("status", error.message));
