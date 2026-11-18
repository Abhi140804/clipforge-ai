/* ClipForge pages, part 1: shared UI helpers, dashboard, new project, projects, agent workspace. */
"use strict";

/* ---------- shared helpers ---------- */
const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
  kids.forEach((k) => k && el.append(k instanceof Node ? k : document.createTextNode(String(k))));
  return el;
}
const opts = (pairs, val) => pairs.map(([v, l]) => h("option", { value: v, selected: String(v) === String(val) }, l));
const selectEl = (pairs, val) => h("select", {}, opts(pairs, val));
const field = (label, control, hint) => h("label", { class: "f" }, h("span", {}, label), control, hint ? h("span", { class: "hint" }, hint) : null);
function check(label, checked, sub) {
  const input = h("input", { type: "checkbox", checked: !!checked });
  return { input, el: h("label", { class: "chk" }, input, h("span", {}, label, sub ? h("small", {}, sub) : null)) };
}
function segment(pairs, val, onchange) {
  let cur = val;
  const box = h("div", { class: "seg", role: "group" });
  const paint = () => box.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === String(cur)));
  pairs.forEach(([v, l]) => box.append(h("button", { type: "button", "data-v": String(v), onclick: () => { cur = v; paint(); onchange && onchange(v); } }, l)));
  paint();
  return { el: box, get: () => cur, set: (v) => { cur = v; paint(); } };
}
const clipThumb = (c) => `/api/clips/${c.id}/media/thumb?v=${c.updated_at}`;
const clipVideo = (c, kind) => `/api/clips/${c.id}/media/${kind || "video"}?v=${c.updated_at}`;
function progressBar(p) { return h("div", { class: "bar" }, h("i", { style: `width:${Math.round((p || 0) * 100)}%` })); }
function emptyState(title, text, action) { return h("div", { class: "empty" }, h("h3", {}, title), h("p", {}, text), action ? h("div", { style: "margin-top:14px" }, action) : null); }
function xhrUpload(path, fd, onprog) {
  return new Promise((res, rej) => {
    const x = new XMLHttpRequest();
    x.open("POST", path); x.setRequestHeader("X-CF-CSRF", "1");
    x.upload.onprogress = (e) => e.lengthComputable && onprog && onprog(e.loaded / e.total);
    x.onload = () => { let d = null; try { d = JSON.parse(x.responseText); } catch (e) { /* non-JSON */ } x.status < 300 ? res(d) : rej(new ApiError((d && d.error) || x.statusText || "Upload failed", x.status)); };
    x.onerror = () => rej(new ApiError("Upload failed (network error).", 0));
    x.send(fd);
  });
}
const PLATFORM = { youtube: "YouTube", twitch: "Twitch", kick: "Kick", direct: "Direct URL", upload: "Upload", unknown: "Link" };
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

/* ---------- preferences form (used by New Project and Settings) ---------- */
const LANGS = [["auto", "Auto-detect"], ["en", "English"], ["es", "Spanish"], ["fr", "French"], ["de", "German"], ["pt", "Portuguese"], ["it", "Italian"],
  ["hi", "Hindi"], ["ja", "Japanese"], ["ko", "Korean"], ["zh", "Chinese"], ["ru", "Russian"], ["ar", "Arabic"], ["tr", "Turkish"], ["nl", "Dutch"]];

/* mode "project": only fields the user touches are sent, so the preset keeps control of the rest.
   mode "defaults": every field is sent and becomes the account default. */
function prefsForm(mode, init) {
  const meta = state.meta, D = meta.defaults, I = init || {};
  const proj = mode === "project";
  const music = meta.assets.filter((a) => a.kind === "music");
  const dur = segment([[15, "15 s"], [30, "30 s"], [45, "45 s"], [60, "60 s"], ["custom", "Custom"]], [15, 30, 45, 60].includes(I.duration ?? D.duration) ? (I.duration ?? D.duration) : "custom", () => syncCustom());
  const custom = h("input", { type: "number", min: 10, max: 180, value: I.duration ?? D.duration, style: "width:90px" });
  const syncCustom = () => { custom.style.display = dur.get() === "custom" ? "" : "none"; if (custom.__unit) custom.__unit.style.display = custom.style.display; }; syncCustom();
  const preset = selectEl([["auto", "Auto (agent picks from content)"], ...meta.presets.map((p) => [p, p[0].toUpperCase() + p.slice(1)])], I.preset ?? "auto");
  const ctype = selectEl(meta.content_types.map((c) => [c, c === "auto" ? "Auto-detect" : c[0].toUpperCase() + c.slice(1)]), I.content_type ?? "auto");
  const maxc = h("input", { type: "number", min: 1, max: 12, value: I.max_clips ?? D.max_clips });
  const minscore = h("input", { type: "range", min: 0, max: 100, step: 5, value: I.min_score ?? D.min_score });
  minscore.style.flex = "1";
  const minlbl = h("span", { class: "num muted" }, String(minscore.value));
  minscore.addEventListener("input", () => (minlbl.textContent = minscore.value));
  const variants = check("Also render an alternative edit of each moment", (I.variants ?? D.variants) >= 2, "A second treatment (different layout, captions, zoom) to compare.");
  const lang = selectEl(LANGS, I.language ?? "auto");
  const caplang = h("input", { type: "text", placeholder: "e.g. Spanish (leave empty to keep original)", value: I.caption_language ?? "" });
  const depth = selectEl([["standard", "Standard: LLM reads the transcript"], ["off", "Off: audio/visual/text heuristics only"]], I.llm_depth ?? "standard");
  const Cp = (I.captions || {}), Au = (I.audio || {}), Ed = (I.editing || {});
  const tri = (cur, pairs) => selectEl(proj ? [["", "Preset default"], ...pairs] : pairs, proj ? (cur === undefined ? "" : String(cur)) : String(cur ?? ""));
  const capOn = tri(Cp.enabled, [["true", "On"], ["false", "Off"]]);
  const capStyle = tri(Cp.style, [["pop", "Pop (word highlight)"], ["classic", "Classic"], ["minimal", "Minimal"]]);
  const capPos = tri(Cp.position, [["top", "Top"], ["middle", "Middle"], ["lower", "Lower third"], ["bottom", "Bottom"]]);
  const fonts = meta.fonts.length ? meta.fonts : ["Poppins"];
  const capFont = tri(Cp.font, fonts.map((f) => [f, f]));
  const musicSel = tri(Au.music, [["auto", "Auto (by content type)"], ["none", "None"], ...music.map((a) => [String(a.id), a.name + (a.builtin ? "" : " (yours)")])]);
  const sfxSel = tri(Au.sfx, [["auto", "Auto"], ["none", "None"]]);
  const layout = tri(Ed.layout, [["auto", "Auto"], ["face", "Follow the speaker"], ["blur", "Blurred background"], ["center", "Center crop"]]);
  const zoom = tri(Ed.zoom, [["auto", "Auto"], ["off", "Off"], ["subtle", "Subtle"], ["punchy", "Punchy"]]);
  const cb = (label, key, group, sub) => check(label, ((group || {})[key] ?? ({ remove_pauses: D.editing.remove_pauses, jump_cut_zoom: D.editing.jump_cut_zoom, color: D.editing.color, denoise: D.audio.denoise, voice_enhance: D.audio.voice_enhance, normalize: D.audio.normalize, ducking: D.audio.ducking })[key]), sub);
  const rp = cb("Remove silences", "remove_pauses", Ed), jc = cb("Zoom on jump cuts", "jump_cut_zoom", Ed), col = cb("Basic color correction", "color", Ed);
  const dn = cb("Noise reduction", "denoise", Au), ve = cb("Voice enhancement", "voice_enhance", Au), nm = cb("Loudness normalization", "normalize", Au), du = cb("Duck music under speech", "ducking", Au);
  const touched = new Set();
  const track = (name, ...els) => els.forEach((e) => e.addEventListener("change", () => touched.add(name)));
  track("captions", capOn); track("cstyle", capStyle); track("cpos", capPos); track("cfont", capFont); track("music", musicSel); track("sfx", sfxSel);
  track("layout", layout); track("zoom", zoom); track("rp", rp.input); track("jc", jc.input); track("col", col.input);
  track("dn", dn.input); track("ve", ve.input); track("nm", nm.input); track("du", du.input);
  const advanced = h("details", { class: "stack", style: "margin-top:4px" }, h("summary", { style: "cursor:pointer;color:var(--muted);margin-bottom:12px" }, proj ? "Style overrides (optional)" : "Captions, audio and editing defaults"),
    h("div", { class: "field-grid", style: "margin-top:12px" },
      field("Captions", capOn), field("Caption style", capStyle), field("Caption font", capFont), field("Caption position", capPos),
      field("Background music", musicSel), field("Sound effects", sfxSel), field("Framing", layout), field("Zoom", zoom),
      h("div", { class: "wide stack" }, rp.el, jc.el, col.el, dn.el, ve.el, nm.el, du.el)));
  const el = h("div", { class: "stack" },
    h("div", { class: "field-grid" }, field("Editing preset", preset), field("Content type", ctype)),
    field("Maximum clip length", h("div", { class: "row" }, dur.el, custom, custom.__unit = h("span", { class: "hint" }, "seconds"))),
    h("div", { class: "field-grid" }, field("Clips to create (max)", maxc), field("Minimum engagement score", h("div", { class: "row" }, minscore, minlbl))),
    variants.el,
    h("div", { class: "field-grid" }, field("Spoken language", lang), field("Translate captions", caplang)),
    field("AI analysis", depth),
    advanced);
  const bool = (v) => v === "true";
  function get() {
    const out = { preset: preset.value, content_type: ctype.value, max_clips: Number(maxc.value) || 5, min_score: Number(minscore.value),
      variants: variants.input.checked ? 2 : 1, language: lang.value, caption_language: caplang.value.trim(), llm_depth: depth.value };
    out.duration = dur.get() === "custom" ? Math.max(10, Math.min(180, Number(custom.value) || 45)) : Number(dur.get());
    const want = (name) => !proj || touched.has(name);
    const grp = (o, k, v) => { if (v !== "" && v !== undefined) (o[k] = v); };
    const captions = {}, audio = {}, editing = {};
    if (want("captions") && capOn.value !== "") captions.enabled = bool(capOn.value);
    if (want("cstyle") && capStyle.value) captions.style = capStyle.value;
    if (want("cpos") && capPos.value) captions.position = capPos.value;
    if (want("cfont") && capFont.value) captions.font = capFont.value;
    if (want("music") && musicSel.value) audio.music = musicSel.value;
    if (want("sfx") && sfxSel.value) audio.sfx = sfxSel.value;
    if (want("layout") && layout.value) editing.layout = layout.value;
    if (want("zoom") && zoom.value) editing.zoom = zoom.value;
    if (want("rp")) editing.remove_pauses = rp.input.checked;
    if (want("jc")) editing.jump_cut_zoom = jc.input.checked;
    if (want("col")) editing.color = col.input.checked;
    if (want("dn")) audio.denoise = dn.input.checked;
    if (want("ve")) audio.voice_enhance = ve.input.checked;
    if (want("nm")) audio.normalize = nm.input.checked;
    if (want("du")) audio.ducking = du.input.checked;
    if (Object.keys(captions).length) out.captions = captions;
    if (Object.keys(audio).length) out.audio = audio;
    if (Object.keys(editing).length) out.editing = editing;
    if (proj && out.content_type === "auto") delete out.content_type;
    return out;
  }
  return { el, get };
}

/* ---------- dashboard ---------- */
function activityChart(rows) {
  const W = 720, H = 150, pad = 22, n = rows.length, max = Math.max(1, ...rows.map((r) => Math.max(r.clips, r.projects)));
  const bw = (W - pad) / n, svg = sv("svg", { viewBox: `0 0 ${W} ${H + 18}`, role: "img", "aria-label": "Projects started and clips rendered per day over the last two weeks", preserveAspectRatio: "none" });
  svg.style.cssText = "width:100%;height:170px;display:block";
  [0.5, 1].forEach((f) => svg.append(sv("line", { x1: pad, x2: W, y1: H - f * (H - 10), y2: H - f * (H - 10), stroke: "var(--line)", "stroke-width": 1 })));
  svg.append(sv("text", { x: 0, y: 14, fill: "var(--faint)", "font-size": 11 }, String(max)));
  rows.forEach((r, i) => {
    const x = pad + i * bw, ch = (r.clips / max) * (H - 10), ph = (r.projects / max) * (H - 10);
    const t = sv("title", {}, `${r.day}: ${r.clips} clips, ${r.projects} projects`);
    const g = sv("g", {}, t, sv("rect", { x: x + bw * 0.12, y: H - ch, width: bw * 0.42, height: ch, rx: 2, fill: "var(--tungsten)" }),
      sv("rect", { x: x + bw * 0.56, y: H - ph, width: bw * 0.32, height: ph, rx: 2, fill: "var(--info)" }));
    svg.append(g);
    if (i % 2 === 0) svg.append(sv("text", { x: x + bw * 0.1, y: H + 14, fill: "var(--faint)", "font-size": 10 }, r.day));
  });
  return svg;
}

function projectRow(p) {
  const clips = p.clip_counts || {};
  const n = (clips.ready || 0);
  return h("tr", { class: "link-row", onclick: () => (location.hash = "#/projects/" + p.id) },
    h("td", {}, h("div", { style: "font-weight:500" }, p.title || "Untitled video"), h("div", { class: "faint small" }, PLATFORM[p.platform] || p.platform, " · ", ago(p.created_at))),
    h("td", {}, pill(p.status), p.status === "running" ? h("div", { style: "margin-top:6px;width:120px" }, progressBar(p.progress)) : null),
    h("td", { class: "num" }, n ? `${n} clip${n > 1 ? "s" : ""}` : "–"));
}

route(/^\/$/, async (main) => {
  const body = h("div", { class: "stack" });
  main.append(page("Dashboard", "What the agent has produced and what it is working on right now.", h("a", { class: "btn primary", href: "#/new" }, icon("plus"), "New project")), body);
  const load = async () => {
    const d = await GET("/api/dashboard");
    clear(body);
    const pub = d.publishing || {};
    body.append(
      h("div", { class: "grid g4" },
        h("div", { class: "panel stat" }, h("b", {}, d.videos_processed), h("span", {}, "Videos processed")),
        h("div", { class: "panel stat" }, h("b", {}, d.clips_ready), h("span", {}, "Clips ready", d.clips_failed ? ` · ${d.clips_failed} failed QC` : "")),
        h("div", { class: "panel stat" }, h("b", {}, d.avg_score ?? "–"), h("span", {}, "Average engagement score")),
        h("div", { class: "panel stat" }, h("b", {}, (pub.published || 0) + (pub.scheduled || 0)), h("span", {}, "Published or scheduled", pub.pending_approval ? ` · ${pub.pending_approval} awaiting approval` : ""))),
      d.active.length ? h("div", { class: "panel" }, h("h2", {}, "Agent is working"), h("div", { class: "stack" }, d.active.map((p) =>
        h("div", { class: "stack", style: "gap:6px;cursor:pointer", onclick: () => (location.hash = "#/projects/" + p.id) },
          h("div", { class: "row sp" }, h("b", {}, p.title || "Untitled video"), h("span", { class: "muted small" }, p.status === "queued" ? "Queued" : ({ ingest: "Fetching video", transcribe: "Transcribing", analyze: "Analyzing signals", detect: "Finding moments", render: "Editing clips", metadata: "Writing titles" }[p.stage] || p.stage))),
          progressBar(p.progress))))) : null,
      h("div", { class: "grid split" },
        h("div", { class: "panel" }, h("div", { class: "row sp" }, h("h2", {}, "Processing activity"), h("span", { class: "small muted" }, h("i", { style: "display:inline-block;width:9px;height:9px;background:var(--tungsten);border-radius:2px;margin-right:5px" }), "clips rendered ", h("i", { style: "display:inline-block;width:9px;height:9px;background:var(--info);border-radius:2px;margin:0 5px 0 12px" }), "projects started")), activityChart(d.activity)),
        h("div", { class: "panel" }, h("h2", {}, "Publishing"),
          h("div", { class: "stack", style: "gap:8px" },
            [["pending_approval", "Awaiting approval"], ["queued", "Queued"], ["scheduled", "Scheduled on YouTube"], ["published", "Published"], ["failed", "Failed"]].map(([k, l]) =>
              h("div", { class: "row sp" }, h("span", { class: "muted" }, l), h("b", { class: "num" }, pub[k] || 0))),
            d.quota ? h("div", { class: "hint", style: "margin-top:6px" }, `YouTube quota today: ${d.quota.used} / ${d.quota.limit} units (${d.quota.uploads_left} uploads left)`) : h("a", { href: "#/publishing" }, "Connect YouTube →")))),
      h("div", { class: "panel" }, h("div", { class: "row sp" }, h("h2", {}, "Recent projects"), h("a", { href: "#/projects" }, "All projects →")),
        d.recent.length ? h("table", {}, h("tbody", {}, d.recent.map(projectRow))) : emptyState("No projects yet", "Paste a video link or upload a file and the agent will find the best moments.", h("a", { class: "btn primary", href: "#/new" }, "Start your first project"))));
  };
  await load();
  every(4000, () => load().catch(() => {}));
}, "/");

/* ---------- new project ---------- */
route(/^\/new$/, async (main) => {
  const caps = state.meta.caps;
  let mode = "url", file = null, sub = null;
  const rights = check("I own this video or am authorised to use it", false, "ClipForge only processes content you have the rights to. Videos protected by DRM or access restrictions are not processed.");
  const prefs = prefsForm("project");
  const tabs = h("div", { class: "tabs" });
  const pane = h("div", { class: "stack" });
  const url = h("input", { type: "url", placeholder: "https://www.youtube.com/watch?v=…  ·  Twitch VOD  ·  Kick video  ·  direct .mp4 link", autocomplete: "off" });
  const probeOut = h("div");
  const batch = h("textarea", { placeholder: "One video URL per line (up to 25)", rows: 6 });
  const titleIn = h("input", { type: "text", placeholder: "Optional title", maxlength: 200 });
  const fileInfo = h("div", { class: "muted small" });
  const prog = h("div", { style: "display:none" }, progressBar(0));
  const go = h("button", { class: "primary", type: "button" }, icon("play"), "Start AI processing");

  const probe = async () => {
    clear(probeOut);
    if (!url.value.trim()) return;
    probeOut.append(h("div", { class: "muted small" }, "Checking the link…"));
    const r = await guard(() => POST("/api/projects/probe", { url: url.value.trim() }));
    clear(probeOut);
    if (!r) return;
    if (r.ok) {
      probeOut.append(h("div", { class: "notice ok" }, h("b", {}, r.title || "Video found"), h("div", { class: "muted small" }, [PLATFORM[r.platform] || r.platform, r.uploader, r.duration ? fmtTC(r.duration) : null].filter(Boolean).join(" · "),
        r.downloadable === false ? " · " + (r.reason || "not downloadable") : "")));
      if (r.downloadable === false) probeOut.append(h("div", { class: "notice warn", style: "margin-top:8px" }, (r.reason || "This video cannot be fetched automatically.") + " You can still create the project and upload the file when asked."));
    } else {
      probeOut.append(h("div", { class: "notice " + (r.needs_upload ? "warn" : "bad") }, r.error, r.needs_upload ? h("div", { class: "small", style: "margin-top:4px" }, "Switch to the Upload tab and add the file instead.") : null));
    }
  };
  const drop = h("div", { class: "drop", tabindex: 0, role: "button", "aria-label": "Choose a video file" }, h("b", {}, "Drop a video here or click to choose"), h("div", { class: "small", style: "margin-top:6px" }, `MP4, MOV, MKV or WebM · up to ${caps.max_upload_mb} MB`));
  const fileIn = h("input", { type: "file", accept: ".mp4,.mov,.mkv,.webm,video/*", style: "display:none" });
  const subIn = h("input", { type: "file", accept: ".srt,.vtt" });
  const setFile = (f) => { file = f; fileInfo.textContent = f ? `${f.name} · ${fmtBytes(f.size)}` : ""; if (f && !titleIn.value) titleIn.value = f.name.replace(/\.[^.]+$/, ""); };
  drop.onclick = () => fileIn.click();
  drop.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileIn.click(); } };
  fileIn.onchange = () => setFile(fileIn.files[0] || null);
  subIn.onchange = () => (sub = subIn.files[0] || null);
  ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });

  const panes = {
    url: () => [field("Video link", h("div", { class: "row", style: "flex-wrap:nowrap" }, url, h("button", { type: "button", onclick: probe }, "Check link"))),
      probeOut, h("div", { class: "hint" }, caps.yt_dlp ? "YouTube, Twitch VOD and Kick links are fetched with yt-dlp when the platform allows it." : "yt-dlp is not installed on this server, so YouTube/Twitch/Kick links cannot be downloaded automatically; direct video URLs and uploads work.")],
    batch: () => [field("Video links", batch, "Each link becomes its own project and they are processed one after another in the background. Every project uses the settings below."), h("div", { class: "hint" }, "Batch mode uses the same rights confirmation for all links.")],
    upload: () => [drop, fileIn, fileInfo, field("Title", titleIn), field("Subtitles / captions file (optional)", subIn, "A .srt or .vtt file is used instead of speech recognition.")],
  };
  const draw = () => {
    clear(tabs).append([["url", "Video link"], ["batch", "Batch of links"], ["upload", "Upload a file"]].map(([k, l]) => h("button", { type: "button", class: mode === k ? "on" : "", onclick: () => { mode = k; draw(); } }, l)));
    clear(pane).append(panes[mode]());
  };
  draw();

  go.onclick = async () => {
    if (!rights.input.checked) return toast("Please confirm that you own this video or are authorised to use it.", true);
    go.disabled = true;
    try {
      const p = prefs.get();
      if (mode === "url") {
        if (!url.value.trim()) throw new ApiError("Paste a video link first.");
        const r = await POST("/api/projects", { url: url.value.trim(), prefs: p, rights_confirmed: true });
        location.hash = "#/projects/" + r.id;
      } else if (mode === "batch") {
        const urls = batch.value.split(/\s*\n\s*/).map((s) => s.trim()).filter(Boolean);
        if (!urls.length) throw new ApiError("Add at least one link.");
        const r = await POST("/api/projects/batch", { urls, prefs: p, rights_confirmed: true });
        const ok = r.results.filter((x) => x.ok), bad = r.results.filter((x) => !x.ok);
        if (bad.length) { toast(`${ok.length} queued, ${bad.length} rejected: ` + bad.map((b) => b.error).slice(0, 2).join(" "), true); if (!ok.length) { go.disabled = false; return; } }
        else toast(`${ok.length} projects queued.`);
        location.hash = ok.length === 1 ? "#/projects/" + ok[0].project_id : "#/projects";
      } else {
        if (!file) throw new ApiError("Choose a video file first.");
        const fd = new FormData();
        fd.append("file", file); if (sub) fd.append("subtitles", sub);
        fd.append("title", titleIn.value || file.name); fd.append("rights_confirmed", "true"); fd.append("prefs", JSON.stringify(p));
        prog.style.display = "block";
        const r = await xhrUpload("/api/projects/upload", fd, (f) => { prog.firstChild.firstChild.style.width = Math.round(f * 100) + "%"; });
        location.hash = "#/projects/" + r.id;
      }
    } catch (e) { toast(e.message, true); go.disabled = false; prog.style.display = "none"; }
  };

  main.append(page("New project", "Paste a link or upload a video. The agent does the rest: transcribe, find moments, edit, caption and write titles."),
    h("div", { class: "grid split" },
      h("div", { class: "stack" }, h("div", { class: "panel" }, tabs, pane), h("div", { class: "panel" }, rights.el), prog,
        h("div", { class: "row" }, go, h("span", { class: "hint" }, "Nothing is published automatically. You review every clip first."))),
      h("div", { class: "panel" }, h("h2", {}, "Editing preferences"), prefs.el)));
  if (!caps.llm) main.prepend(h("div", { class: "notice warn", style: "margin-bottom:16px" }, "No Anthropic API key is configured, so the agent runs in heuristic mode (no LLM reasoning, simpler titles). ", h("a", { href: "#/settings" }, "Add a key in Settings")));
}, "/new");

/* ---------- projects ---------- */
route(/^\/projects$/, async (main) => {
  const holder = h("div");
  main.append(page("Projects", "Every video you have given the agent.", h("a", { class: "btn primary", href: "#/new" }, icon("plus"), "New project")), holder);
  const load = async () => {
    const { projects } = await GET("/api/projects");
    clear(holder);
    if (!projects.length) return holder.append(emptyState("No projects yet", "Start one and it will show up here.", h("a", { class: "btn primary", href: "#/new" }, "New project")));
    holder.append(h("div", { class: "panel" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Video"), h("th", {}, "Status"), h("th", {}, "Clips"), h("th", {}, "Length"), h("th", {}))),
      h("tbody", {}, projects.map((p) => h("tr", { class: "link-row", onclick: () => (location.hash = "#/projects/" + p.id) },
        h("td", {}, h("div", { style: "font-weight:500" }, p.title || "Untitled video"), h("div", { class: "faint small" }, PLATFORM[p.platform] || p.platform, " · ", fmtDate(p.created_at))),
        h("td", {}, pill(p.status), p.status === "running" ? h("div", { style: "margin-top:6px;width:120px" }, progressBar(p.progress)) : null),
        h("td", { class: "num" }, (p.clip_counts && p.clip_counts.ready) || "–"),
        h("td", { class: "num muted" }, p.duration ? fmtTC(p.duration) : "–"),
        h("td", { style: "text-align:right" }, h("button", { class: "ghost small danger", title: "Delete project", disabled: ["queued", "running"].includes(p.status),
          onclick: (e) => { e.stopPropagation(); confirmBox("Delete this project?", "The source video, all rendered clips and their queue items are removed.", "Delete", async () => { if (await guard(() => DEL("/api/projects/" + p.id), "Project deleted.") !== undefined) load(); }); } }, "Delete"))))))));
  };
  await load();
  every(5000, () => load().catch(() => {}));
}, "/projects");

/* ---------- agent workspace ---------- */
const STAGES = [["ingest", "Fetch video"], ["transcribe", "Transcribe"], ["analyze", "Analyze signals"], ["detect", "Find moments"], ["render", "Edit & quality-check"], ["metadata", "Write metadata"]];

function timeline(an, onPick) {
  const sg = an.signals;
  if (!sg || !sg.duration) return h("div", { class: "muted" }, "Signal analysis is not available yet.");
  const W = 1000, H = 150, dur = sg.duration, X = (t) => (t / dur) * W;
  const norm = (arr) => { const lo = Math.min(...arr), hi = Math.max(...arr), r = hi - lo || 1; return arr.map((v) => (v - lo) / r); };
  const line = (arr, color, fill) => {
    const a = norm(arr), step = sg.step, pts = a.map((v, i) => `${X(i * step).toFixed(1)},${(H - 6 - v * (H - 16)).toFixed(1)}`);
    return sv("path", { d: "M" + pts.join("L") + (fill ? `L${X(a.length * step)},${H}L0,${H}Z` : ""), fill: fill ? color : "none", "fill-opacity": fill ? 0.18 : null, stroke: color, "stroke-width": 1.4, "vector-effect": "non-scaling-stroke" });
  };
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Audio energy, motion and the moments the agent selected over the video" });
  (sg.silences || []).forEach(([a, b]) => svg.append(sv("rect", { x: X(a), y: 0, width: Math.max(1, X(b - a)), height: H, fill: "var(--line)", "fill-opacity": 0.5 })));
  (an.candidates || []).forEach((c) => svg.append(sv("rect", { x: X(c.start), y: H - 22, width: Math.max(2, X(c.end - c.start)), height: 8, fill: "var(--info)", "fill-opacity": 0.35 }, sv("title", {}, `Candidate ${fmtTC(c.start)}–${fmtTC(c.end)} · score ${c.score}`))));
  if (sg.energy.length) svg.append(line(sg.energy, "#f2a73b", true));
  if (sg.motion.length) svg.append(line(sg.motion, "#62c6d4", false));
  (sg.cuts || []).forEach((t) => svg.append(sv("line", { x1: X(t), x2: X(t), y1: 0, y2: 8, stroke: "var(--faint)", "stroke-width": 1, "vector-effect": "non-scaling-stroke" })));
  (an.selected || []).filter((s) => s.variant === "A").forEach((s) => {
    const g = sv("g", { class: "tl-clip", onclick: null }, sv("title", {}, `Clip ${s.idx + 1}: ${fmtTC(s.start)}–${fmtTC(s.end)} · score ${Math.round(s.score)}`),
      sv("rect", { x: X(s.start), y: H - 12, width: Math.max(4, X(s.end - s.start)), height: 12, rx: 2, fill: "#f2a73b" }));
    g.addEventListener("click", () => onPick && onPick(s));
    svg.append(g);
  });
  return h("div", { class: "timeline" }, svg,
    h("div", { class: "row sp small muted", style: "margin-top:4px" }, h("span", {}, "0:00"), h("span", {}, fmtTC(dur / 2)), h("span", {}, fmtTC(dur))),
    h("div", { class: "legend" }, h("span", {}, h("i", { style: "background:#f2a73b" }), "Audio energy"), h("span", {}, h("i", { style: "background:#62c6d4" }), "Visual motion"),
      h("span", {}, h("i", { style: "background:var(--info);opacity:.4" }), "Candidate moments"), h("span", {}, h("i", { style: "background:#f2a73b" }), "Selected clips (click)"), h("span", {}, h("i", { style: "background:var(--line)" }), "Silence")));
}

route(/^\/projects\/(\d+)$/, async (main, [id]) => {
  const head = h("div"), progressBox = h("div"), logBox = h("div", { class: "log", "aria-live": "off" }), analysisBox = h("div", { class: "stack" }), sideBox = h("div", { class: "stack" }), clipsBox = h("div");
  let last = 0, debug = false, lastStatus = null, atBottom = true, analysisLoaded = false, p = null;
  logBox.addEventListener("scroll", () => { atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40; });
  const dbg = check("Show debug detail", false);
  dbg.input.addEventListener("change", () => { debug = dbg.input.checked; last = 0; clear(logBox); pollLog(); });

  const addEvents = (evs) => evs.forEach((e) => {
    logBox.append(h("div", { class: "ev " + e.level }, h("time", { class: "tc" }, clock(e.ts)), h("span", { class: "tool" }, e.tool || (e.level === "thought" ? "agent" : "")), h("span", { class: "msg" }, e.message)));
    last = Math.max(last, e.id);
  });
  const pollLog = async () => {
    const r = await GET(`/api/projects/${id}/events?after=${last}${debug ? "&debug=1" : ""}`);
    if (r.events.length) { addEvents(r.events); if (atBottom) logBox.scrollTop = logBox.scrollHeight; }
  };

  const drawAnalysis = async () => {
    const an = await GET(`/api/projects/${id}/analysis`);
    if (!an.signals && !an.candidates.length) return false;
    clear(analysisBox);
    const sel = (an.selected || []).filter((s) => s.variant === "A");
    const covered = (c) => sel.find((s) => Math.abs(c.start - s.start) < 1.5 && Math.abs(c.end - s.end) < 1.5);
    const overlaps = (c) => sel.find((s) => c.start < s.end && c.end > s.start);
    analysisBox.append(h("div", { class: "panel" }, h("h2", {}, "What the agent saw"),
      h("div", { class: "muted small", style: "margin-bottom:10px" }, an.content_type ? `Detected content type: ${an.content_type}. ` : "", "Orange bars are the clips it chose; blue ticks are other candidates it scored."),
      timeline(an, (s) => (location.hash = "#/clips/" + s.id))),
      h("div", { class: "panel" }, h("h2", {}, "Ranked candidate moments"),
        an.candidates.length ? h("div", { style: "overflow:auto" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Score"), h("th", {}, "Why it scored"), h("th", {}, "Transcript"), h("th", {}, ""))),
          h("tbody", {}, an.candidates.slice(0, 15).map((c) => { const s = covered(c); return h("tr", {}, h("td", { class: "num", style: "white-space:nowrap" }, `${fmtTC(c.start)}–${fmtTC(c.end)}`), h("td", { class: "num" }, Math.round(c.score)),
            h("td", { class: "small muted" }, Object.entries(c.dims || {}).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, v]) => `${k.replace(/_/g, " ")} ${Math.round(v * 100)}`).join(" · ")),
            h("td", { class: "small", style: "max-width:340px" }, c.text || h("span", { class: "faint" }, "no speech")),
            h("td", {}, s ? h("a", { href: "#/clips/" + s.id }, "Selected →") : overlaps(c) ? h("span", { class: "faint small" }, "overlaps a chosen clip") : h("span", { class: "faint small" }, "not selected"))); })))) : h("div", { class: "muted" }, "No candidates were scored.")));
    return true;
  };

  const draw = async () => {
    p = await GET("/api/projects/" + id);
    const idx = p.status === "completed" ? STAGES.length : Math.max(0, STAGES.findIndex(([k]) => k === p.stage));
    clear(head).append(page(p.title || "Untitled video", [PLATFORM[p.platform] || p.platform, p.duration ? fmtTC(p.duration) : null, p.width ? `${p.width}×${p.height}` : null, "added " + ago(p.created_at)].filter(Boolean).join(" · "),
      [pill(p.status),
        ["queued", "running"].includes(p.status) ? h("button", { onclick: () => guard(() => POST(`/api/projects/${id}/cancel`), "Cancelling…") }, "Cancel") : null,
        ["failed", "cancelled"].includes(p.status) ? h("button", { class: "primary", onclick: async () => { await guard(() => POST(`/api/projects/${id}/retry`), "Restarted."); draw(); } }, "Retry") : null,
        h("button", { class: "danger", disabled: ["queued", "running"].includes(p.status), onclick: () => confirmBox("Delete this project?", "The source video, all rendered clips and their queue items are removed.", "Delete", async () => { if (await guard(() => DEL("/api/projects/" + id), "Project deleted.") !== undefined) location.hash = "#/projects"; }) }, "Delete")]));
    clear(progressBox);
    if (p.status === "needs_upload") {
      const f = h("input", { type: "file", accept: ".mp4,.mov,.mkv,.webm,video/*" }), s = h("input", { type: "file", accept: ".srt,.vtt" }), bar = h("div", { style: "display:none" }, progressBar(0));
      progressBox.append(h("div", { class: "panel" }, h("div", { class: "notice warn", style: "margin-bottom:14px" }, h("b", {}, "The agent could not fetch this video. "), p.error || ""),
        h("p", { class: "muted", style: "margin-bottom:12px" }, "Upload the file yourself (only if you own it or are authorised to use it) and the agent carries on from here."),
        h("div", { class: "stack" }, field("Video file", f), field("Subtitles (optional .srt/.vtt)", s), bar,
          h("div", {}, h("button", { class: "primary", onclick: async (e) => {
            if (!f.files[0]) return toast("Choose a file first.", true);
            e.target.disabled = true; bar.style.display = "block";
            const fd = new FormData(); fd.append("file", f.files[0]); if (s.files[0]) fd.append("subtitles", s.files[0]);
            try { await xhrUpload(`/api/projects/${id}/upload`, fd, (x) => (bar.firstChild.firstChild.style.width = Math.round(x * 100) + "%")); toast("Uploaded. Processing started."); draw(); }
            catch (ex) { toast(ex.message, true); e.target.disabled = false; bar.style.display = "none"; }
          } }, "Upload and continue")))));
    } else {
      progressBox.append(h("div", { class: "panel" },
        p.status === "failed" ? h("div", { class: "notice bad", style: "margin-bottom:14px" }, h("b", {}, "Processing failed. "), p.error || "") : null,
        h("div", { class: "steps", style: "margin-bottom:12px" }, STAGES.map(([k, l], i) => h("span", { class: "step" + (i < idx ? " done" : i === idx && p.status === "running" ? " now" : "") }, (i < idx ? "✓ " : "") + l))),
        h("div", { class: "row sp small muted", style: "margin-bottom:6px" }, h("span", {}, p.status === "running" ? "Working…" : p.status === "queued" ? "Waiting for a free worker" : p.status === "completed" ? "Finished" : ""), h("span", { class: "num" }, Math.round((p.progress || 0) * 100) + "%")), progressBar(p.progress)));
    }
    const m = p.meta || {};
    clear(sideBox).append(h("div", { class: "panel" }, h("h3", {}, "Summary"),
      h("div", { class: "stack small", style: "margin-top:10px" },
        m.content_type ? h("div", {}, h("span", { class: "muted" }, "Content type "), h("b", {}, m.content_type)) : null,
        m.agent_summary ? h("div", { class: "muted" }, m.agent_summary) : null,
        h("div", {}, h("span", { class: "muted" }, "Clips "), h("b", {}, Object.entries(p.clip_counts || {}).map(([k, v]) => `${v} ${k}`).join(", ") || "none yet")),
        m.llm_usage ? h("div", { class: "faint" }, `LLM usage: ${m.llm_usage.calls || 0} calls · ${((m.llm_usage.input_tokens || 0) + (m.llm_usage.output_tokens || 0)).toLocaleString()} tokens`) : null,
        m.source_removed ? h("div", { class: "faint" }, "The source file was removed by storage cleanup; rendered clips are kept.") : null,
        p.source_url ? h("div", { class: "faint", style: "word-break:break-all" }, p.source_url) : null)));
    clear(clipsBox);
    const clips = p.clips || [];
    if (clips.length) clipsBox.append(h("div", { class: "panel flat", style: "padding:0" }, h("div", { class: "row sp", style: "margin-bottom:12px" }, h("h2", {}, "Generated clips"), h("a", { href: "#/clips?project=" + id }, "Open in clip library →")), h("div", { class: "clips" }, clips.map((c) => clipCard(c)))));
    await pollLog();
    if (!analysisLoaded || (p.status === "running" && ["detect", "render", "metadata"].includes(p.stage))) analysisLoaded = await drawAnalysis().catch(() => false);
    lastStatus = p.status;
  };

  main.append(head, progressBox, h("div", { class: "grid split" }, h("div", { class: "stack" }, h("div", { class: "panel" }, h("div", { class: "row sp", style: "margin-bottom:10px" }, h("h2", {}, "Agent activity"), dbg.el), logBox), analysisBox), sideBox), h("div", { style: "margin-top:16px" }, clipsBox));
  await draw();
  every(2000, async () => {
    try {
      if (["queued", "running"].includes(lastStatus) || lastStatus === null || (p && (p.clips || []).some((c) => c.status === "rendering"))) await draw();
      else await pollLog();
    } catch (e) { /* transient */ }
  });
}, "/projects");
