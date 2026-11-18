/* ClipForge pages, part 2: clip cards, clip library, video editor. */
"use strict";

const clipTitle = (c) => {
  const m = c.metadata || {}, s = m.selected || {};
  return s.custom_title || (m.titles && m.titles[s.title || 0]) || `Moment ${c.idx + 1}${c.variant !== "A" ? " · variant " + c.variant : ""}`;
};

function clipCard(c, onChange) {
  const frame = h("div", { class: "frame" });
  const poster = c.has_thumb ? h("img", { src: clipThumb(c), alt: "", loading: "lazy" }) : h("div", { class: "muted small", style: "padding:14px;text-align:center" }, c.status === "rendering" ? "Rendering…" : c.status === "failed" ? "Render failed" : "No preview");
  frame.append(poster, h("span", { class: "score", title: "Engagement score" }, Math.round(c.score)), h("span", { class: "dur tc" }, fmtTC(c.duration)));
  if (c.has_file || c.has_preview) {
    frame.append(h("button", { class: "play", "aria-label": "Play clip", onclick: (e) => {
      const v = h("video", { src: clipVideo(c, c.has_file ? "video" : "preview"), controls: true, autoplay: true, playsinline: true });
      clear(frame).append(v, h("span", { class: "score" }, Math.round(c.score)));
      e.stopPropagation();
    } }, h("span", {}, icon("play"))));
  }
  return h("div", { class: "clip" }, frame, h("div", { class: "body" },
    h("div", { class: "row sp", style: "align-items:flex-start;flex-wrap:nowrap" }, h("b", { style: "line-height:1.3" }, clipTitle(c)), pill(c.status)),
    h("div", { class: "faint small tc" }, `${fmtTC(c.start)}–${fmtTC(c.end)} · source timecode`, c.stale ? " · edits not rendered" : ""),
    c.error ? h("div", { class: "small", style: "color:var(--bad)" }, c.error) : h("div", { class: "why", title: c.reason }, c.reason),
    h("div", { class: "row", style: "margin-top:auto" },
      h("a", { class: "btn small", href: "#/clips/" + c.id }, "Edit"),
      c.has_file ? h("a", { class: "btn small", href: `/api/clips/${c.id}/media/download`, download: "" }, "Download") : null,
      c.status === "ready" ? h("a", { class: "btn small", href: "#/studio?clip=" + c.id }, "Metadata") : null)));
}

/* ---------- library ---------- */
route(/^\/clips$/, async (main, _, q) => {
  let status = q.status || "", project = q.project || "";
  const bar = h("div", { class: "row", style: "margin-bottom:16px" }), grid = h("div");
  const { projects } = await GET("/api/projects");
  main.append(page("Clips", "Everything the agent has produced. Open a clip to edit captions, style and audio."), bar, grid);
  const draw = async () => {
    const r = await GET(`/api/clips?${project ? "project=" + project + "&" : ""}${status ? "status=" + status : ""}`);
    clear(bar).append(h("div", { class: "seg" }, [["", "All"], ["ready", "Ready"], ["rendering", "Rendering"], ["failed", "Failed"]].map(([v, l]) => h("button", { class: status === v ? "on" : "", onclick: () => { status = v; draw(); } }, l))),
      (() => { const s = selectEl([["", "All projects"], ...projects.map((p) => [String(p.id), p.title || "Untitled"])], project); s.style.maxWidth = "280px"; s.onchange = () => { project = s.value; draw(); }; return s; })());
    clear(grid);
    if (!r.clips.length) return grid.append(emptyState("No clips here", status || project ? "Nothing matches this filter." : "Start a project and the clips will appear here as soon as they are rendered.", h("a", { class: "btn primary", href: "#/new" }, "New project")));
    grid.append(h("div", { class: "clips" }, r.clips.map((c) => h("div", {}, h("div", { class: "faint small", style: "margin:0 0 6px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, c.project_title), clipCard(c)))));
    return r.clips.some((c) => c.status === "rendering");
  };
  await draw();
  every(4000, () => draw().catch(() => {}));
}, "/clips");

/* ---------- editor ---------- */
route(/^\/clips\/(\d+)$/, async (main, [id]) => {
  let clip = await GET("/api/clips/" + id);
  let edit = JSON.parse(JSON.stringify(clip.edit || {}));
  const pending = {}, capEdits = new Map();
  let tab = "captions", viewing = clip.has_file ? "video" : "preview", busy = null;
  const meta = state.meta;
  edit.captions = edit.captions || {}; edit.audio = edit.audio || {}; edit.trim = edit.trim || { start_delta: 0, end_delta: 0 };
  const dirty = () => Object.keys(pending).length > 0 || capEdits.size > 0;

  const localSet = (key, val) => {
    if (key === "music") { edit.music_asset = val === "none" || val === "" || val == null ? null : val; return; }
    if (key === "trim_start") { edit.trim.start_delta = val; return; }
    if (key === "trim_end") { edit.trim.end_delta = val; return; }
    const parts = key.split("."); let cur = edit;
    for (const p of parts.slice(0, -1)) cur = cur[p] || (cur[p] = {});
    cur[parts[parts.length - 1]] = val;
  };
  const setKey = (key, val) => { pending[key] = val; localSet(key, val); refreshBar(); };

  /* controls bound to the edit-decision document */
  const bindSelect = (key, pairs, cur) => { const s = selectEl(pairs, cur); s.onchange = () => setKey(key, s.value); return s; };
  const bindCheck = (key, label, cur, sub) => { const c = check(label, cur, sub); c.input.onchange = () => setKey(key, c.input.checked); return c.el; };
  const bindRange = (key, lo, hi, step, cur, fmt) => {
    const r = h("input", { type: "range", min: lo, max: hi, step, value: cur ?? lo }), out = h("span", { class: "num muted", style: "min-width:44px;text-align:right" }, (fmt || String)(Number(r.value)));
    r.oninput = () => { out.textContent = (fmt || String)(Number(r.value)); setKey(key, Number(r.value)); };
    return h("div", { class: "row", style: "flex-wrap:nowrap" }, r, out);
  };
  const bindColor = (key, cur) => { const i = h("input", { type: "color", value: cur || "#ffffff" }); i.oninput = () => setKey(key, i.value.toUpperCase()); return i; };

  const phone = h("div", { class: "phone" }), side = h("div", { class: "stack" }), panel = h("div", { class: "panel" }), bar = h("div", { class: "row sp", style: "margin-bottom:14px" });
  const tabsEl = h("div", { class: "tabs" });
  const status = h("div");

  function refreshBar() {
    clear(bar).append(h("div", { class: "row" }, dirty() ? h("span", { class: "pill warn" }, "Unsaved changes") : clip.stale ? h("span", { class: "pill warn" }, "Edits not rendered yet") : h("span", { class: "pill ok" }, "Up to date"),
      busy ? h("span", { class: "muted small" }, busy) : null),
      h("div", { class: "row" },
        h("button", { disabled: !dirty() || !!busy, onclick: () => save() }, "Save"),
        h("button", { disabled: !!busy || clip.status === "rendering", onclick: () => renderNow("preview") }, "Render preview"),
        h("button", { class: "primary", disabled: !!busy || clip.status === "rendering", onclick: () => renderNow("final") }, "Render final")));
  }

  async function save() {
    if (capEdits.size) {
      const lines = [...capEdits.values()];
      await api("PUT", `/api/clips/${id}/captions`, { lines });
      capEdits.clear();
    }
    if (Object.keys(pending).length) {
      clip = await PATCH("/api/clips/" + id, { edit: { ...pending } });
      for (const k of Object.keys(pending)) delete pending[k];
    }
    clip = await GET("/api/clips/" + id);
    edit = JSON.parse(JSON.stringify(clip.edit || {}));
    edit.captions = edit.captions || {}; edit.audio = edit.audio || {}; edit.trim = edit.trim || { start_delta: 0, end_delta: 0 };
  }

  async function renderNow(mode) {
    try {
      if (dirty()) await save();
      busy = mode === "preview" ? "Rendering preview…" : "Rendering final (with quality checks)…"; refreshBar();
      const before = clip.updated_at;
      const evs = await GET(`/api/projects/${clip.project_id}/events?after=0`);
      const lastEv = evs.events.length ? evs.events[evs.events.length - 1].id : 0;
      await POST(`/api/clips/${id}/render`, { mode });
      const t0 = Date.now();
      while (Date.now() - t0 < 600000) {
        await new Promise((r) => setTimeout(r, 1500));
        const c = await GET("/api/clips/" + id);
        const done = mode === "final" ? c.status !== "rendering" : c.updated_at !== before;
        if (done) { clip = c; edit = JSON.parse(JSON.stringify(c.edit || {})); edit.captions = edit.captions || {}; edit.audio = edit.audio || {}; edit.trim = edit.trim || { start_delta: 0, end_delta: 0 };
          viewing = mode === "final" && c.has_file ? "video" : "preview"; busy = null;
          if (mode === "final" && c.status === "failed") toast("Render did not pass quality control: " + (c.error || "see the Quality tab"), true); else toast(mode === "final" ? "Final render complete." : "Preview ready.");
          drawAll(); return; }
        if (mode === "preview") {
          const e2 = await GET(`/api/projects/${clip.project_id}/events?after=${lastEv}`);
          const bad = e2.events.find((e) => e.level === "error" && /Render|failed/.test(e.message));
          if (bad) throw new ApiError(bad.message);
        }
      }
      throw new ApiError("Rendering is taking unusually long. Check the project's agent log.");
    } catch (e) { busy = null; toast(e.message, true); try { clip = await GET("/api/clips/" + id); } catch (x) { /* ignore */ } drawAll(); }
  }

  /* ---------- preview ---------- */
  function drawPhone() {
    clear(phone);
    const hasF = clip.has_file, hasP = clip.has_preview;
    const src = viewing === "video" && hasF ? clipVideo(clip, "video") : hasP ? clipVideo(clip, "preview") : hasF ? clipVideo(clip, "video") : null;
    phone.append(h("div", { class: "frame" }, src ? h("video", { src, controls: true, playsinline: true, poster: clip.has_thumb ? clipThumb(clip) : null }) :
      h("div", { class: "muted small", style: "padding:18px;text-align:center" }, clip.status === "rendering" ? "Rendering…" : clip.status === "failed" ? "This render failed quality control. Adjust the settings and render again." : "Render a preview to see your edit.")));
  }

  /* ---------- tabs ---------- */
  const TABS = [["captions", "Captions"], ["style", "Style & presets"], ["audio", "Music & audio"], ["frame", "Framing & trim"], ["quality", "Quality & why"]];
  const tabBody = {
    captions() {
      const C = edit.captions;
      const lines = h("div", {});
      (clip.caption_lines || []).forEach((l) => {
        const inp = h("input", { type: "text", value: l.text, maxlength: 200, "aria-label": `Caption at ${fmtTC(l.start)}` });
        inp.oninput = () => { if (inp.value.trim() !== l.text) capEdits.set(l.i0, { i0: l.i0, i1: l.i1, text: inp.value.trim() }); else capEdits.delete(l.i0); refreshBar(); };
        lines.append(h("div", { class: "cap-line" }, h("span", { class: "tc faint small" }, l.start.toFixed(1) + "s"), inp));
      });
      return h("div", { class: "stack" },
        bindCheck("captions.enabled", "Show captions", C.enabled !== false),
        h("div", { class: "field-grid" },
          field("Style", bindSelect("captions.style", [["pop", "Pop (word highlight)"], ["classic", "Classic"], ["minimal", "Minimal"]], C.style)),
          field("Font", bindSelect("captions.font", (meta.fonts.length ? meta.fonts : ["Poppins"]).map((f) => [f, f]), C.font)),
          field("Position", bindSelect("captions.position", [["top", "Top"], ["middle", "Middle"], ["lower", "Lower third"], ["bottom", "Bottom"]], C.position)),
          field("Size", bindRange("captions.size", 40, 160, 4, C.size)),
          field("Words per line", bindRange("captions.words_per_line", 1, 6, 1, C.words_per_line)),
          field("Nudge up / down", bindRange("caption_offset", -1, 1, 0.05, edit.caption_offset || 0, (v) => v.toFixed(2)))),
        bindCheck("captions.uppercase", "UPPERCASE", C.uppercase),
        h("div", { class: "row" }, field("Text", bindColor("captions.color", C.color)), field("Highlight", bindColor("captions.highlight", C.highlight)), field("Key words", bindColor("captions.keyword_color", C.keyword_color)), field("Outline", bindColor("captions.outline", C.outline))),
        h("div", {}, h("h3", { style: "margin:6px 0 8px" }, "Caption text"),
          (clip.caption_lines || []).length ? [h("div", { class: "hint", style: "margin-bottom:8px" }, "Fix words the speech recognizer got wrong. Timing is kept. Save before regrouping lines with the controls above."), lines] :
            h("div", { class: "muted" }, "This clip has no transcript, so there is nothing to edit.")));
    },
    style() {
      return h("div", { class: "stack" },
        h("div", {}, h("h3", { style: "margin-bottom:8px" }, "Editing presets"), h("div", { class: "chips" }, meta.presets.map((p) => h("button", { class: "chip", title: "Apply this preset to the clip", onclick: () => applyPreset(p) }, p[0].toUpperCase() + p.slice(1)))),
          h("div", { class: "hint", style: "margin-top:6px" }, "A preset changes the settings below in one go; you can still fine-tune afterwards.")),
        h("div", { class: "field-grid" },
          field("Framing", bindSelect("layout", [["auto", "Auto"], ["face", "Follow the speaker"], ["blur", "Blurred background"], ["center", "Center crop"]], edit.layout)),
          field("Zoom effects", bindSelect("zoom", [["auto", "Auto"], ["off", "Off"], ["subtle", "Subtle"], ["punchy", "Punchy"]], edit.zoom))),
        bindCheck("jump_cut_zoom", "Zoom on jump cuts", edit.jump_cut_zoom, "Alternating punch-ins hide cuts and keep the pace up."),
        bindCheck("color", "Basic color correction", edit.color),
        bindCheck("remove_pauses", "Remove silences", edit.remove_pauses, "Dead air longer than the threshold is trimmed."),
        h("div", { class: "field-grid" },
          field("Silence threshold", bindRange("pause_threshold", 0.3, 2, 0.1, edit.pause_threshold ?? 0.7, (v) => v.toFixed(1) + " s")),
          field("Pause to keep", bindRange("pause_keep", 0.05, 0.6, 0.05, edit.pause_keep ?? 0.22, (v) => v.toFixed(2) + " s"))),
        h("div", { class: "row" }, h("button", { onclick: async () => { const r = await guard(() => POST(`/api/clips/${id}/variant`)); if (r) { toast("Rendering an alternative edit…"); location.hash = "#/clips/" + r.clip_id; } } }, "Create alternative variant"),
          h("button", { class: "danger", onclick: () => confirmBox("Delete this clip?", "The rendered file is removed and any queued upload is cancelled.", "Delete clip", async () => { if (await guard(() => DEL("/api/clips/" + id), "Clip deleted.") !== undefined) location.hash = "#/projects/" + clip.project_id; }) }, "Delete clip")));
    },
    audio() {
      const A = edit.audio, music = meta.assets.filter((a) => a.kind === "music"), sfx = meta.assets.filter((a) => a.kind === "sfx");
      const cur = edit.music_asset == null ? "none" : String(edit.music_asset);
      const mus = bindSelect("music", [["none", "No music"], ...music.map((a) => [a.builtin ? a.name : String(a.id), a.name + (a.builtin ? " (built-in)" : " (yours)")])], cur);
      const up = h("input", { type: "file", accept: ".mp3,.wav,.m4a,.aac,.ogg,.flac" }), lic = check("I have the rights to use this audio", false);
      const kind = selectEl([["music", "Music"], ["sfx", "Sound effect"]], "music");
      return h("div", { class: "stack" },
        h("div", { class: "field-grid" }, field("Background music", mus), field("Music level", bindRange("audio.music_db", -40, -6, 1, A.music_db ?? -18, (v) => v + " dB"))),
        field("Sound effects", bindSelect("sfx", [["auto", "Auto (whoosh/pop on cuts and emphasis)"], ["none", "None"]], edit.sfx || "auto"), sfx.length ? "Library: " + sfx.map((a) => a.name).join(", ") : null),
        bindCheck("audio.ducking", "Duck music under speech", A.ducking),
        bindCheck("audio.denoise", "Noise reduction", A.denoise),
        bindCheck("audio.voice_enhance", "Voice enhancement", A.voice_enhance),
        bindCheck("audio.normalize", "Loudness normalization", A.normalize),
        field("Target loudness", bindRange("audio.target_lufs", -24, -9, 1, A.target_lufs ?? -14, (v) => v + " LUFS")),
        h("div", { class: "panel", style: "background:var(--bg)" }, h("h3", { style: "margin-bottom:8px" }, "Add your own audio"),
          h("div", { class: "hint", style: "margin-bottom:10px" }, "Built-in sounds are synthesized by ClipForge. Upload music you are licensed to use; ClipForge never adds commercial music on its own."),
          h("div", { class: "stack" }, h("div", { class: "field-grid" }, field("Type", kind), field("File", up)), lic.el,
            h("div", {}, h("button", { onclick: async (e) => {
              if (!up.files[0]) return toast("Choose an audio file.", true);
              if (!lic.input.checked) return toast("Confirm that you have the rights to use this audio.", true);
              e.target.disabled = true;
              try { const fd = new FormData(); fd.append("file", up.files[0]); fd.append("kind", kind.value); fd.append("license_confirmed", "true");
                await xhrUpload("/api/assets", fd); state.meta = await GET("/api/meta"); toast("Audio added."); drawAll(); }
              catch (ex) { toast(ex.message, true); e.target.disabled = false; }
            } }, "Upload audio")))));
    },
    frame() {
      return h("div", { class: "stack" },
        h("div", { class: "field-grid" },
          field("Horizontal position", bindRange("crop_offset", -1, 1, 0.05, edit.crop_offset || 0, (v) => v.toFixed(2)), "Slide the vertical crop left/right when framing is not on auto-tracking."),
          field("Crop zoom", bindRange("crop_zoom", 1, 2, 0.05, edit.crop_zoom || 1, (v) => v.toFixed(2) + "×"))),
        h("div", { class: "field-grid" },
          field("Start (seconds earlier / later)", bindRange("trim_start", -3, 3, 0.1, edit.trim.start_delta || 0, (v) => (v > 0 ? "+" : "") + v.toFixed(1) + " s"), "Negative starts the clip earlier."),
          field("End (seconds earlier / later)", bindRange("trim_end", -3, 3, 0.1, edit.trim.end_delta || 0, (v) => (v > 0 ? "+" : "") + v.toFixed(1) + " s"))),
        h("div", { class: "hint" }, `Source range: ${fmtTC(clip.start)}–${fmtTC(clip.end)} (${clip.duration.toFixed(1)} s). Trim changes apply when you render.`));
    },
    quality() {
      const q = clip.qc || {}, sd = clip.score_detail || {};
      const dims = Object.entries(sd.dims || sd).filter(([, v]) => typeof v === "number");
      return h("div", { class: "stack" },
        h("div", {}, h("h3", { style: "margin-bottom:6px" }, "Why the agent chose this moment"), h("p", { class: "muted" }, clip.reason), clip.kind ? h("div", { class: "hint", style: "margin-top:4px" }, "Moment type: " + clip.kind) : null),
        dims.length ? h("div", { class: "dims" }, dims.map(([k, v]) => [h("span", { class: "muted" }, k.replace(/_/g, " ")), h("div", { class: "row", style: "flex-wrap:nowrap;gap:8px" }, h("div", { class: "bar", style: "flex:1" }, h("i", { style: `width:${Math.round((v <= 1 ? v * 100 : v))}%` })), h("span", { class: "num small muted" }, Math.round(v <= 1 ? v * 100 : v)))])) : null,
        h("div", {}, h("h3", { style: "margin-bottom:8px" }, "Quality control"),
          (q.checks || []).length ? h("div", { class: "checks" }, q.checks.map((c) => h("div", {}, h("span", { class: "s " + (c.status === "pass" ? "" : c.status) }), h("span", {}, h("b", {}, c.name.replace(/_/g, " ")), " · ", h("span", { class: "muted" }, c.detail))))) : h("div", { class: "muted" }, "Render the final clip to run the quality checks."),
          q.attempts && q.attempts.length > 1 ? h("div", { class: "hint", style: "margin-top:8px" }, `The agent needed ${q.attempts.length} render attempts and corrected problems automatically.`) : null),
        (q.decisions || []).length ? h("div", {}, h("h3", { style: "margin-bottom:6px" }, "Editing decisions"), h("ul", { class: "muted small", style: "margin:0;padding-left:18px" }, q.decisions.map((d) => h("li", {}, d)))) : null,
        clip.error ? h("div", { class: "notice bad" }, clip.error) : null);
    },
  };

  function applyPreset(name) {
    const d = meta.preset_defs[name] || {};
    for (const [k, v] of Object.entries(d.editing || {})) setKey(k, v);
    for (const [k, v] of Object.entries(d.captions || {})) setKey("captions." + k, v);
    for (const [k, v] of Object.entries(d.audio || {})) {
      if (k === "music") setKey("music", v === "none" ? "none" : (edit.music_asset || "Calm Pad"));
      else if (k === "sfx") setKey("sfx", v);
      else setKey("audio." + k, v);
    }
    toast(`${name[0].toUpperCase() + name.slice(1)} preset applied. Render to see it.`);
    drawAll();
  }

  function drawPanel() {
    clear(tabsEl).append(TABS.map(([k, l]) => h("button", { class: tab === k ? "on" : "", onclick: () => { tab = k; drawPanel(); } }, l)));
    clear(panel).append(tabsEl, tabBody[tab]());
  }
  function drawAll() {
    clear(status);
    if (clip.status === "failed") status.append(h("div", { class: "notice bad", style: "margin-bottom:14px" }, h("b", {}, "Quality control failed. "), clip.error || "Adjust the settings and render again."));
    if (clip.status === "rendering" && !busy) status.append(h("div", { class: "notice", style: "margin-bottom:14px" }, "This clip is rendering. The page will update when it finishes."));
    drawPhone(); drawPanel(); refreshBar();
    const seg = clip.has_file && clip.has_preview ? h("div", { class: "seg", style: "margin:12px auto 0;display:flex;width:max-content" }, [["video", "Final"], ["preview", "Preview"]].map(([k, l]) => h("button", { class: viewing === k ? "on" : "", onclick: () => { viewing = k; drawAll(); } }, l))) : null;
    clear(side).append(phone, seg, h("div", { class: "stack small", style: "margin-top:14px" },
      h("div", { class: "row sp" }, h("span", { class: "muted" }, "Engagement score"), h("b", { class: "num" }, Math.round(clip.score))),
      h("div", { class: "row sp" }, h("span", { class: "muted" }, "Duration"), h("b", { class: "num" }, clip.duration.toFixed(1) + " s")),
      h("div", { class: "row sp" }, h("span", { class: "muted" }, "Status"), pill(clip.status))),
      h("div", { class: "row", style: "margin-top:8px" }, clip.has_file ? h("a", { class: "btn", href: `/api/clips/${id}/media/download`, download: "" }, "Download MP4") : null,
        clip.status === "ready" ? h("a", { class: "btn", href: "#/studio?clip=" + id }, "Titles & publish") : null));
  }

  main.append(h("div", { class: "head" }, h("div", {}, h("div", { class: "small" }, h("a", { href: "#/projects/" + clip.project_id }, "← " + (clip.project.title || "Project"))), h("h1", { style: "margin-top:6px" }, clipTitle(clip)),
    h("p", {}, `${fmtTC(clip.start)}–${fmtTC(clip.end)} from the source · variant ${clip.variant}`))), status, h("div", { class: "grid editor" }, side, h("div", {}, bar, panel)));
  drawAll();
  every(3000, async () => {
    if (busy || dirty()) return;
    try { const c = await GET("/api/clips/" + id); if (c.status !== clip.status || c.updated_at !== clip.updated_at) { clip = c; edit = JSON.parse(JSON.stringify(c.edit || {})); edit.captions = edit.captions || {}; edit.audio = edit.audio || {}; edit.trim = edit.trim || { start_delta: 0, end_delta: 0 }; if (c.status === "ready" && c.has_file) viewing = "video"; drawAll(); } } catch (e) { /* transient */ }
  });
}, "/clips");
