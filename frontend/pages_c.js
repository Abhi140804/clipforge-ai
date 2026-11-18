/* ClipForge pages, part 3: content studio, publishing, settings. */
"use strict";

const PRIVACY = [["private", "Private"], ["unlisted", "Unlisted"], ["public", "Public"]];
const tagsToText = (t) => (t || []).map((x) => "#" + x).join(" ");
const textToTags = (s) => s.split(/[\s,]+/).map((x) => x.replace(/^#/, "").replace(/[^A-Za-z0-9_]/g, "")).filter(Boolean).slice(0, 15);
const toLocalInput = (t) => (t ? new Date(t * 1000 - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "");
const fromLocalInput = (v) => (v ? Math.floor(new Date(v).getTime() / 1000) : null);

/* ---------- content studio ---------- */
route(/^\/studio$/, async (main, _, q) => {
  const { clips } = await GET("/api/clips?status=ready");
  main.append(page("Content studio", "Titles, hooks, hashtags, descriptions and thumbnail text for each finished clip. Pick what you like or write your own."));
  if (!clips.length) return main.append(emptyState("No finished clips yet", "Metadata is generated for each clip once it passes quality control.", h("a", { class: "btn primary", href: "#/new" }, "New project")));
  let cur = clips.find((c) => String(c.id) === q.clip) || clips[0];
  const list = h("div", { class: "stack", style: "gap:8px" }), work = h("div", { class: "stack" });
  main.append(h("div", { class: "grid", style: "grid-template-columns:260px minmax(0,1fr);align-items:start" }, h("div", { class: "panel", style: "padding:10px" }, list), work));
  const drawList = () => clear(list).append(clips.map((c) => h("button", { class: "ghost", style: `justify-content:flex-start;text-align:left;width:100%;padding:8px;gap:10px;${c.id === cur.id ? "background:var(--panel-2);border-color:var(--line-2);color:var(--text)" : ""}`, onclick: () => { cur = c; drawList(); drawWork(); } },
    c.has_thumb ? h("img", { src: clipThumb(c), alt: "", style: "width:36px;height:64px;object-fit:cover;border-radius:4px;flex:none" }) : null,
    h("span", { style: "min-width:0" }, h("span", { style: "display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, clipTitle(c)), h("span", { class: "faint small" }, `${c.project_title} · ${fmtTC(c.duration)}`)))));

  async function drawWork() {
    clear(work);
    const c = await GET("/api/clips/" + cur.id);
    let m = c.metadata || {};
    if (!m.titles) return work.append(h("div", { class: "notice warn" }, "No metadata has been generated for this clip yet."), h("button", { onclick: async () => { if (await guard(() => POST(`/api/clips/${c.id}/metadata/regenerate`), "Metadata generated.")) drawWork(); } }, "Generate now"));
    const sel = () => m.selected || {};
    const save = async (patch, msg) => { const r = await guard(() => PATCH(`/api/clips/${c.id}/metadata`, patch), msg); if (r) { m = r; } return r; };

    // titles
    const customT = h("input", { type: "text", maxlength: 100, placeholder: "Write your own title", value: sel().custom_title || "" });
    const titleBox = h("div", {});
    const drawTitles = () => {
      clear(titleBox);
      const custom = !!sel().custom_title;
      m.titles.forEach((t, i) => titleBox.append(h("label", { class: "choice" + (!custom && (sel().title || 0) === i ? " on" : "") }, h("input", { type: "radio", name: "title", checked: !custom && (sel().title || 0) === i,
        onchange: async () => { await save({ title: i, custom_title: "" }); customT.value = ""; drawTitles(); } }), h("span", {}, t, h("small", { class: "faint num", style: "display:block" }, `${t.length} characters`)))));
      titleBox.append(h("div", { class: "choice" + (custom ? " on" : "") }, h("input", { type: "radio", name: "title", checked: custom, onchange: () => customT.focus() }), h("div", { style: "flex:1" }, customT)));
    };
    customT.addEventListener("change", async () => { if (customT.value.trim()) await save({ custom_title: customT.value }, "Title saved."); drawTitles(); });
    drawTitles();

    // hooks
    const hookBox = h("div", {}, m.hooks.map((t, i) => h("label", { class: "choice" + ((sel().hook || 0) === i ? " on" : ""), onclick: null }, h("input", { type: "radio", name: "hook", checked: (sel().hook || 0) === i,
      onchange: async (e) => { await save({ hook: i }); hookBox.querySelectorAll(".choice").forEach((x, j) => x.classList.toggle("on", j === i)); } }), h("span", {}, t))));

    // thumbnail
    const thumbText = h("input", { type: "text", maxlength: 60, value: m.thumbnail_text[sel().thumbnail_text || 0] || "" });
    const thumbImg = h("img", { alt: "Thumbnail preview", style: "width:100%;border-radius:8px;display:block", src: c.has_thumb ? clipThumb(c) : "" });
    thumbImg.onerror = () => { thumbImg.style.display = "none"; };
    const thumbChips = h("div", { class: "chips" }, m.thumbnail_text.map((t, i) => h("button", { class: "chip" + ((sel().thumbnail_text || 0) === i ? " on" : ""), onclick: async () => { thumbText.value = t; await save({ thumbnail_text: i }); thumbChips.querySelectorAll(".chip").forEach((x, j) => x.classList.toggle("on", j === i)); } }, t)));
    const makeThumb = async (btn) => {
      btn.disabled = true;
      const r = await guard(() => POST(`/api/clips/${c.id}/thumbnail`, { text: thumbText.value, t: c.duration * 0.35 }), "Thumbnail created.");
      btn.disabled = false;
      if (r) { thumbImg.style.display = "block"; thumbImg.src = `/api/clips/${c.id}/media/thumb_text?v=${r.v}`; dl.style.display = "inline-flex"; dl.href = thumbImg.src; }
    };
    const dl = h("a", { class: "btn small", style: "display:none", download: `thumbnail_${c.id}.jpg`, href: "#" }, "Download thumbnail");

    // description + tags
    const desc = h("textarea", { rows: 5, maxlength: 4500 }, sel().custom_description || m.description || "");
    const tags = h("input", { type: "text", value: tagsToText(sel().custom_hashtags || m.hashtags) });
    const saveText = async () => { await save({ custom_description: desc.value, custom_hashtags: textToTags(tags.value) }, "Saved."); };

    work.append(
      h("div", { class: "row sp" }, h("div", {}, h("h2", {}, clipTitle(c)), h("div", { class: "muted small" }, [m.category, m.niche, m.generated_by === "llm" || m.generated_by === "claude" ? "written by the LLM" : m.generated_by ? "written by local heuristics (no LLM key)" : null].filter(Boolean).join(" · "))),
        h("div", { class: "row" },
          h("button", { onclick: async (e) => { e.target.disabled = true; const r = await guard(() => POST(`/api/clips/${c.id}/metadata/regenerate`), "Fresh suggestions generated."); e.target.disabled = false; if (r) drawWork(); } }, "Regenerate"),
          h("button", { class: "primary", onclick: async () => { const r = await guard(() => POST("/api/publish", { clip_id: c.id }), "Added to the publishing queue for your approval."); if (r) location.hash = "#/publishing"; } }, icon("send"), "Send to publishing"))),
      m.summary ? h("div", { class: "notice" }, h("b", {}, "Summary. "), m.summary) : null,
      h("div", { class: "grid g2" },
        h("div", { class: "stack" }, h("div", { class: "panel" }, h("h3", {}, "Title"), titleBox),
          h("div", { class: "panel" }, h("h3", {}, "Opening hook (on-screen text)"), hookBox)),
        h("div", { class: "stack" },
          h("div", { class: "panel" }, h("h3", {}, "Thumbnail"), h("div", { class: "grid", style: "grid-template-columns:120px 1fr;align-items:start" }, thumbImg, h("div", { class: "stack" }, thumbChips, field("Text on thumbnail", thumbText), h("div", { class: "row" }, h("button", { onclick: (e) => makeThumb(e.target) }, "Create thumbnail"), dl)))),
          h("div", { class: "panel" }, h("h3", {}, "Description & hashtags"), h("div", { class: "stack" }, field("Description", desc), field("Hashtags", tags, "Separate with spaces. YouTube shows the first three above the title."), h("div", {}, h("button", { onclick: saveText }, "Save text")))))));
  }
  drawList(); await drawWork();
}, "/studio");

/* ---------- publishing ---------- */
function editItemModal(item, done) {
  const title = h("input", { type: "text", maxlength: 100, value: item.title }), desc = h("textarea", { rows: 6, maxlength: 4500 }, item.description || "");
  const tags = h("input", { type: "text", value: tagsToText(item.tags) }), priv = selectEl(PRIVACY, item.privacy), when = h("input", { type: "datetime-local", value: toLocalInput(item.schedule_at) });
  const body = h("div", { class: "stack" }, field("Title", title), field("Description", desc), field("Hashtags", tags), h("div", { class: "field-grid" }, field("Privacy", priv),
    field("Schedule (optional)", when, "Scheduled videos are uploaded privately and YouTube makes them public at that time.")));
  modal("Edit upload", body, (close) => [h("button", { onclick: close }, "Cancel"), h("button", { onclick: async () => {
    const r = await guard(() => PATCH("/api/publish/" + item.id, { title: title.value, description: desc.value, tags: textToTags(tags.value), privacy: priv.value, schedule_at: fromLocalInput(when.value) }), "Saved.");
    if (r) { close(); done(); } } }, "Save")]);
}

route(/^\/publishing$/, async (main, _, q) => {
  if (q.yt === "connected") toast("YouTube channel connected.");
  if (q.yt_error) toast("YouTube connection failed: " + q.yt_error, true);
  if (q.yt || q.yt_error) history.replaceState(null, "", "#/publishing");
  const top = h("div"), queue = h("div"), history_ = h("div"), ready = h("div");
  main.append(page("Publishing", "Review, schedule and upload finished clips to your YouTube channel. Nothing is uploaded until you approve it."), top, queue, ready, history_);

  const draw = async () => {
    const [pub, rc] = await Promise.all([GET("/api/publish"), GET("/api/clips?status=ready")]);
    const st = pub.status, items = pub.items;
    clear(top);
    if (!st.configured) top.append(h("div", { class: "panel" }, h("h2", {}, "Connect YouTube"), h("p", { class: "muted", style: "margin:6px 0 12px" }, "Publishing uses the official YouTube Data API with your own Google OAuth client. Add the client ID and secret first."),
      h("div", { class: "notice", style: "margin-bottom:12px" }, "Authorised redirect URI to register in Google Cloud Console: ", h("code", {}, st.redirect_uri)), h("a", { class: "btn primary", href: "#/settings" }, "Open Settings")));
    else if (!st.connected) top.append(h("div", { class: "panel" }, h("h2", {}, "Connect YouTube"), h("p", { class: "muted", style: "margin:6px 0 12px" }, "Sign in with Google to let ClipForge upload to your channel. You can disconnect at any time."),
      h("button", { class: "primary", onclick: async () => { const r = await guard(() => GET("/api/youtube/connect")); if (r) location.href = r.url; } }, "Connect YouTube account")));
    else top.append(h("div", { class: "panel" }, h("div", { class: "row sp" }, h("div", {}, h("h2", {}, "YouTube connected"), h("div", { class: "muted", style: "margin-top:4px" }, st.channel ? (st.channel.title || st.channel.id || "Your channel") : "")),
      h("div", { class: "row" }, h("span", { class: "muted small" }, `Quota today ${st.quota.used}/${st.quota.limit} · ${st.quota.uploads_left} uploads left`), h("button", { class: "danger", onclick: () => confirmBox("Disconnect YouTube?", "Queued uploads stay in the queue but cannot be sent until you reconnect.", "Disconnect", async () => { await guard(() => POST("/api/youtube/disconnect"), "Disconnected."); draw(); }) }, "Disconnect"))),
      h("div", { class: "hint", style: "margin-top:10px" }, pub.auto_publish ? "Automatic publishing is ON (see Settings): new clips are queued as approved, within your daily cap." : "Human approval is required for every upload. Automatic publishing is off.")));

    const active = items.filter((i) => ["pending_approval", "queued", "uploading", "rate_limited", "failed"].includes(i.status));
    const doneI = items.filter((i) => !["pending_approval", "queued", "uploading", "rate_limited", "failed"].includes(i.status));
    clear(queue).append(h("div", { class: "panel", style: "margin-top:16px" }, h("h2", {}, "Publishing queue"),
      active.length ? h("div", { style: "overflow:auto" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Clip"), h("th", {}, "Privacy"), h("th", {}, "When"), h("th", {}, "Status"), h("th", {}))),
        h("tbody", {}, active.map((i) => h("tr", {}, h("td", {}, h("div", { style: "font-weight:500" }, i.title), h("div", { class: "faint small" }, i.clip ? i.clip.project_title : ""), i.error ? h("div", { class: "small", style: "color:var(--bad)" }, i.error) : null),
          h("td", {}, i.privacy), h("td", { class: "small" }, i.schedule_at ? fmtDate(i.schedule_at) : "As soon as approved"), h("td", {}, pill(i.status)),
          h("td", { style: "text-align:right;white-space:nowrap" }, h("div", { class: "row", style: "justify-content:flex-end;flex-wrap:nowrap" },
            ["pending_approval", "queued", "failed", "rate_limited"].includes(i.status) ? h("button", { class: "small", onclick: () => editItemModal(i, draw) }, "Edit") : null,
            i.status === "pending_approval" ? h("button", { class: "small primary", disabled: !st.connected, title: st.connected ? "" : "Connect YouTube first", onclick: async () => { await guard(() => POST(`/api/publish/${i.id}/approve`), "Approved. Uploading shortly."); draw(); } }, "Approve") : null,
            ["failed", "rate_limited"].includes(i.status) ? h("button", { class: "small", onclick: async () => { await guard(() => POST(`/api/publish/${i.id}/retry`), "Queued again."); draw(); } }, "Retry") : null,
            i.status !== "uploading" ? h("button", { class: "small ghost danger", onclick: async () => { await guard(() => POST(`/api/publish/${i.id}/cancel`), "Removed from queue."); draw(); } }, "Remove") : null)))))))
        : h("div", { class: "muted" }, "The queue is empty. Add clips below or from the Content studio.")));

    const queued = new Set(active.map((i) => i.clip_id)), avail = rc.clips.filter((c) => !queued.has(c.id));
    clear(ready).append(avail.length ? h("div", { class: "panel", style: "margin-top:16px" }, h("h2", {}, "Ready to queue"), h("div", { class: "stack", style: "gap:8px" }, avail.slice(0, 12).map((c) =>
      h("div", { class: "row sp" }, h("div", { class: "row", style: "flex-wrap:nowrap" }, c.has_thumb ? h("img", { src: clipThumb(c), alt: "", style: "width:34px;height:60px;object-fit:cover;border-radius:4px" }) : null, h("div", {}, h("div", { style: "font-weight:500" }, clipTitle(c)), h("div", { class: "faint small" }, c.project_title + " · " + fmtTC(c.duration)))),
        h("button", { onclick: async () => { const r = await guard(() => POST("/api/publish", { clip_id: c.id }), "Added to the queue."); if (r) draw(); } }, "Add to queue"))))) : null);

    clear(history_).append(h("div", { class: "panel", style: "margin-top:16px" }, h("h2", {}, "Upload history"),
      doneI.length ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Title"), h("th", {}, "Status"), h("th", {}, "Date"), h("th", {}))),
        h("tbody", {}, doneI.map((i) => h("tr", {}, h("td", {}, i.title), h("td", {}, pill(i.status)), h("td", { class: "small muted" }, fmtDate(i.published_at || i.updated_at)),
          h("td", { style: "text-align:right" }, i.url ? h("a", { href: i.url, target: "_blank", rel: "noopener noreferrer" }, "Open on YouTube ↗") : null))))) : h("div", { class: "muted" }, "Nothing has been uploaded yet.")));
    return items.some((i) => ["queued", "uploading"].includes(i.status));
  };
  await draw();
  every(5000, () => draw().catch(() => {}));
}, "/publishing");

/* ---------- settings ---------- */
function secretField(label, s, hint) {
  const i = h("input", { type: "password", autocomplete: "off", placeholder: s.set ? `Saved (${s.hint || "hidden"}). Type to replace.` : "Not set" });
  return { i, el: field(label, i, hint), set: s.set };
}

route(/^\/settings$/, async (main) => {
  const [s, st] = await Promise.all([GET("/api/settings"), GET("/api/storage")]);
  const caps = state.meta.caps;
  const sectionSave = (label, build, after) => h("button", { class: "primary", onclick: async (e) => { e.target.disabled = true; const r = await guard(() => PUT("/api/settings", build()), label + " saved."); e.target.disabled = false; if (r) { state.meta = await GET("/api/meta"); after && after(r); } } }, "Save");

  // AI
  const key = secretField("Anthropic API key", s.anthropic_api_key, "Used for moment analysis, agent decisions, metadata and caption translation. Stored encrypted; never sent to the browser.");
  const model = h("input", { type: "text", value: s.anthropic_model }), base = h("input", { type: "text", value: s.anthropic_base_url, placeholder: "https://api.anthropic.com" });
  const llmOn = check("Use the LLM agent", s.llm_enabled, "Turn off to run heuristics only (no API calls).");
  const tKey = secretField("Transcription API key (optional)", s.transcribe_api_key, "Only for the hosted Whisper-compatible engine.");
  const eng = selectEl([["auto", "Auto (best available)"], ["faster_whisper", "faster-whisper (local)"], ["openai_whisper", "openai-whisper (local)"], ["api", "Hosted API"]], s.transcribe_engine);
  const tModel = h("input", { type: "text", value: s.transcribe_model }), tBase = h("input", { type: "text", value: s.transcribe_base_url, placeholder: "https://api.openai.com/v1" });
  const testOut = h("span", { class: "small" });
  const ai = h("div", { class: "panel stack" }, h("h2", {}, "AI models"),
    h("div", { class: "notice" + (caps.llm ? " ok" : " warn") }, caps.llm ? "LLM agent is active." : "No working LLM configuration: ClipForge runs in heuristic mode until you add a key."),
    key.el, h("div", { class: "field-grid" }, field("Model", model), field("API base URL", base, "Leave empty for the default.")), llmOn.el,
    h("div", { class: "row" }, sectionSave("AI settings", () => ({ ...(key.i.value ? { anthropic_api_key: key.i.value } : {}), anthropic_model: model.value, anthropic_base_url: base.value, llm_enabled: llmOn.input.checked }), () => renderRoute()),
      h("button", { onclick: async () => { testOut.textContent = "Testing…"; const r = await guard(() => POST("/api/settings/test-llm")); if (r) { testOut.textContent = r.ok ? `Works. Model ${r.model} replied "${r.reply}".` : r.error; testOut.style.color = r.ok ? "var(--ok)" : "var(--bad)"; } } }, "Test connection"), testOut),
    h("hr", { style: "border:0;border-top:1px solid var(--line);width:100%" }),
    h("h3", {}, "Speech recognition"),
    h("div", { class: "hint" }, "Available here: " + (Object.entries(caps.transcribe || {}).filter(([, v]) => v).map(([k]) => k).join(", ") || "none") + (caps.faces ? ` · face tracking: ${caps.faces}` : " · face tracking unavailable")),
    h("div", { class: "field-grid" }, field("Engine", eng), field("Model", tModel), field("API base URL", tBase)), tKey.el,
    sectionSave("Transcription settings", () => ({ ...(tKey.i.value ? { transcribe_api_key: tKey.i.value } : {}), transcribe_engine: eng.value, transcribe_model: tModel.value, transcribe_base_url: tBase.value }), () => renderRoute()));

  // Google
  const gid = h("input", { type: "text", value: s.google_client_id, autocomplete: "off" }), gsec = secretField("Google OAuth client secret", s.google_client_secret);
  const google = h("div", { class: "panel stack" }, h("h2", {}, "YouTube / Google credentials"),
    h("p", { class: "muted" }, "Create an OAuth client (type: Web application) in Google Cloud Console, enable the YouTube Data API v3, and register this redirect URI:"),
    h("div", { class: "row" }, h("code", { style: "word-break:break-all" }, s.youtube_redirect_uri), h("button", { class: "small", onclick: () => navigator.clipboard.writeText(s.youtube_redirect_uri).then(() => toast("Copied."), () => toast("Copy failed.", true)) }, "Copy")),
    field("Client ID", gid), gsec.el,
    h("div", { class: "row" }, sectionSave("Google credentials", () => ({ google_client_id: gid.value, ...(gsec.i.value ? { google_client_secret: gsec.i.value } : {}) }), () => renderRoute()), h("a", { href: "#/publishing" }, "Go to Publishing →")));

  // editing defaults
  const pf = prefsForm("defaults", s.default_prefs);
  const editing = h("div", { class: "panel stack" }, h("h2", {}, "Editing preferences"), h("div", { class: "hint" }, "These apply to every new project unless you override them there. A chosen preset is applied on top of these values."), pf.el,
    h("div", {}, sectionSave("Editing preferences", () => ({ default_prefs: pf.get() }))));

  // automation
  const auto = check("Publish automatically after clips pass quality control", s.auto_publish, "Off by default. When on, new clips are queued as pre-approved and uploaded without asking. Requires a connected YouTube channel.");
  const priv = selectEl(PRIVACY, s.default_privacy), cap = h("input", { type: "number", min: 1, max: 50, value: s.max_publish_per_day }), shorts = check("Add #Shorts to descriptions", s.append_shorts_tag);
  const automation = h("div", { class: "panel stack" }, h("h2", {}, "Automation"),
    h("div", { class: "notice warn" }, "Human approval stays on until you switch automatic publishing on here. ClipForge never bypasses YouTube's quota or policies; uploads respect the daily cap below and the API quota."),
    auto.el, h("div", { class: "field-grid" }, field("Default privacy", priv, "Use Private while you are testing."), field("Maximum uploads per day", cap)), shorts.el,
    h("div", {}, sectionSave("Automation", () => ({ auto_publish: auto.input.checked, default_privacy: priv.value, max_publish_per_day: Number(cap.value), append_shorts_tag: shorts.input.checked }))));

  // storage
  const ret = h("input", { type: "number", min: 0, max: 365, value: s.source_retention_days }), lim = h("input", { type: "number", min: 1, max: 10000, value: s.storage_max_gb });
  const stBox = h("div", { class: "stack", style: "gap:8px" });
  const drawStorage = (u) => {
    clear(stBox).append(h("div", { class: "row sp" }, h("b", {}, fmtBytes(u.total) + " used"), h("span", { class: "muted small" }, `limit ${fmtBytes(u.limit)} · disk free ${fmtBytes(u.disk_free)}`)), progressBar(Math.min(1, u.total / Math.max(1, u.limit))),
      h("div", { class: "grid g3 small" }, Object.entries(u.bytes).map(([k, v]) => h("div", { class: "row sp" }, h("span", { class: "muted" }, k), h("span", { class: "num" }, fmtBytes(v))))));
  };
  drawStorage(st);
  const storage = h("div", { class: "panel stack" }, h("h2", {}, "Storage"), stBox,
    h("div", { class: "field-grid" }, field("Keep source videos for (days)", ret, "0 = delete the source as soon as clips are rendered. Rendered clips are always kept."), field("Storage limit (GB)", lim, "Old sources are removed first when the limit is exceeded.")),
    h("div", { class: "row" }, sectionSave("Storage settings", () => ({ source_retention_days: Number(ret.value), storage_max_gb: Number(lim.value) })),
      h("button", { onclick: async () => { const r = await guard(() => POST("/api/storage/cleanup", {})); if (r) { toast(`Cleanup freed ${fmtBytes(r.bytes_freed || 0)}.`); drawStorage(await GET("/api/storage")); } } }, "Clean up now"),
      h("button", { class: "danger", onclick: () => confirmBox("Remove all source videos?", "Source and temporary analysis files are deleted. Finished clips are kept, but clips can no longer be re-rendered from the original.", "Remove sources", async () => { const r = await guard(() => POST("/api/storage/cleanup", { all: true })); if (r) { toast("Source files removed."); drawStorage(await GET("/api/storage")); } }) }, "Remove all sources")));

  main.append(page("Settings", "Models, credentials, defaults, automation and storage."),
    h("div", { class: "stack" }, ai, google, editing, automation, storage));
}, "/settings");
