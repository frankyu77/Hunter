(() => {
  const data = JSON.parse(document.getElementById("data").textContent);
  // Who the page greets. Anonymous until unlocked: the name (profile.name in
  // sources.yaml) ships only inside the sealed private layer.
  const ANON = "Individual";
  let profileName = "";
  const firstName = () => (profileName ? profileName.split(/\s+/)[0] : ANON);
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const fmt = (n) => (n == null ? "—" : n.toLocaleString());
  // Per-browser conveniences (API key, GitHub token, resume, unsent changes).
  const saved = {
    get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
    set: (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch { /* storage blocked */ } },
  };
  const session = {
    get: (k) => { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* storage blocked */ } },
  };
  const shortDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, {month: "short", day: "numeric"});
  const relTime = (ms) => {
    const mins = Math.round((Date.now() - ms) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    if (mins < 24 * 60) return `${Math.round(mins / 60)}h ago`;
    if (mins < 7 * 24 * 60) return `${Math.round(mins / 1440)}d ago`;
    return new Date(ms).toLocaleDateString(undefined, {month: "short", day: "numeric"});
  };
  const generated = Date.parse(data.generated);

  function renderProfile() {
    $("workspace-name").textContent = profileName ? `${firstName()}'s workspace` : "Your workspace";
    $("me-name").textContent = profileName || ANON;
    $("me-initial").textContent = (profileName || ANON)[0].toUpperCase();
    $("greeting").textContent = `Hi there, ${firstName()}.`;
  }
  $("today").textContent = new Date().toLocaleDateString(undefined, {weekday: "long", month: "long", day: "numeric"});
  $("built-at").textContent = `Updated ${new Date(generated).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"})}.`;

  // Tiles (Sources page)
  const s = data.stats;
  $("tiles").innerHTML = [
    [fmt(s.tracked), "postings tracked (60 days)"],
    [fmt(s.new_week), "new postings, last 7 days"],
    [fmt(s.sent_week), "sent in the last 7 days"],
    [s.open_matching == null ? "—" : fmt(s.open_matching), "open now, never sent to you"],
    [s.median_close == null ? "—" : s.median_close < 1 ? "<1d" : `${s.median_close}d`, "typical time a posting stays open"],
  ].map(([v, l]) => `<div class="tile"><div class="value num">${v}</div><div class="label">${l}</div></div>`).join("");

  // --- jobs ------------------------------------------------------------------
  // One list for the board: everything sent to you (90 days) plus, when the
  // build had a fetch, what's open now but never sent. The two never overlap.
  // Pages of 25, 50 or 100 rows; the choice is kept per browser.
  const PER_PAGE_KEY = "hunter.jobs.perPage", PAGE_SIZES = [25, 50, 100];
  let perPage = PAGE_SIZES.includes(Number(saved.get(PER_PAGE_KEY))) ? Number(saved.get(PER_PAGE_KEY)) : 25;
  const hasOpen = Array.isArray(data.open);
  const when = (r) => (r.f ? r.f * 1000 : r.d ? Date.parse(r.d + "T00:00:00") : 0);
  const ALL = [...data.history.map((r) => ({...r, sent: true})), ...(data.open || []).map((r) => ({...r, sent: false}))]
    .sort((a, b) => when(b) - when(a));
  const sentIds = new Set(data.history.map((r) => r.i));
  let tab = "all", page = 1;
  // Filled in by the private layer once unlocked (see below); empty otherwise.
  // `pending` holds clicks not yet reflected in the published data.
  let mine = null, mineById = new Map(), pending = {};
  if (!hasOpen) { $("tab-open").disabled = true; $("tab-open").title = "Available on the published site"; }

  // "New since your last visit": rows that reached you (sent, or first seen
  // by Hunter: `f`) after the build you last looked at. localStorage keeps
  // that build's time per browser. sessionStorage pins this visit's cutoff,
  // so reloading the tab keeps today's highlights until "Mark all seen" or
  // your next visit. A first visit ever highlights nothing, rather than
  // calling the whole backlog new.
  const LAST_KEY = "hunter.lastVisit", CUTOFF_KEY = "hunter.visitCutoff";
  const built = Math.floor(generated / 1000);
  let cutoff = Number(session.get(CUTOFF_KEY));
  if (!cutoff) {
    cutoff = Number(saved.get(LAST_KEY)) || built;
    session.set(CUTOFF_KEY, String(cutoff));
  }
  saved.set(LAST_KEY, String(Math.max(built, Number(saved.get(LAST_KEY)) || 0)));
  // Jobs you've opened stop being new straight away. Kept per browser, and
  // only while they would still count as new - older ids are dropped here,
  // so the list never grows past one visit's worth.
  const OPENED_KEY = "hunter.opened";
  let opened;
  try { opened = new Set(JSON.parse(saved.get(OPENED_KEY) || "[]")); } catch { opened = new Set(); }
  const stillNew = new Set(ALL.filter((r) => r.f != null && r.f > cutoff).map((r) => r.i));
  opened = new Set([...opened].filter((id) => stillNew.has(id)));
  const saveOpened = () => saved.set(OPENED_KEY, opened.size ? JSON.stringify([...opened]) : "");
  saveOpened();
  const isNew = (r) => r.f != null && r.f > cutoff && !opened.has(r.i);
  const newCount = () => ALL.filter(isNew).length;

  // The application board's columns (scraper/feedback.py STAGES, same order).
  const STAGE_NAMES = {saved: "Saved", applied: "Applied", oa: "Online assessment",
                       interview: "Interview", offer: "Offer", rejected: "Rejected"};
  const STAGE_COLORS = {saved: "var(--text-muted)", applied: "var(--stage-applied)", oa: "var(--accent)",
                        interview: "var(--accent)", offer: "var(--success)", rejected: "var(--danger)"};
  const STAGES = Object.keys(STAGE_NAMES);
  const FIELDS = ["vote", "applied", "stage", "notes", ...STAGES.map((s) => `date:${s}`)];
  const LEVELS = {internship: "Intern", new_grad: "New grad", full_time: "Full-time"};
  const BOARD_NAMES = {workday: "Workday", greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby",
    oracle: "Oracle", smartrecruiters: "SmartRecruiters", workable: "Workable", rippling: "Rippling",
    bamboohr: "BambooHR", eightfold: "Eightfold", eightfold_v2: "Eightfold", successfactors: "SuccessFactors",
    jibe: "iCIMS", tiktok: "TikTok", amazon: "Amazon", atlassian: "Atlassian"};
  // Where a row came from: the board type from its id, or the feed's name.
  const srcName = (r) => {
    const kind = (r.i || "").split(":")[0];
    return kind === "github" ? r.s : BOARD_NAMES[kind] || r.s || kind;
  };
  const today = () => new Date().toLocaleDateString("en-CA");  // local YYYY-MM-DD

  // Your marks on a row - vote, applied, board stage, stage dates, notes:
  // what the last build published, with any change still on its way
  // overlaid on top. A job marked applied but never placed on the board
  // (e.g. from Telegram before the board existed) sits in Applied.
  function marksOf(r) {
    const m = mineById.get(r.i) || {};
    const out = {vote: m.vote ?? null, applied: m.applied || "", stage: m.stage ?? null,
                 dates: {...(m.dates || {})}, notes: m.notes || "", waiting: {}};
    for (const field of FIELDS) {
      const p = pending[`${r.i}|${field}`];
      if (!p) continue;
      if (field === "applied") out.applied = p.value ? p.at.slice(0, 10) : "";
      else if (field.startsWith("date:")) {
        if (p.value) out.dates[field.slice(5)] = p.value; else delete out.dates[field.slice(5)];
      } else out[field] = p.value;
      out.waiting[field] = p.sent ? "sent" : "unsent";
    }
    out.stage = out.stage || (out.applied ? "applied" : null);
    return out;
  }
  // Everything you marked, for the Applied tab and the board: published rows
  // plus jobs whose first mark is still pending.
  function myRows() {
    const rows = new Map((mine || []).map((r) => [r.i, r]));
    for (const p of Object.values(pending)) {
      if (p.id && FIELDS.includes(p.field) && !rows.has(p.id)) rows.set(p.id, {...p.row, i: p.id, d: p.at.slice(0, 10)});
    }
    return [...rows.values()].filter((r) => { const m = marksOf(r); return m.vote || m.applied || m.stage || m.notes; })
      .map((r) => ({...r, sent: sentIds.has(r.i)}));
  }
  // Moving a job to `stage` (null = off the board): dates the new stage
  // today unless it already has a date, and keeps "applied" in step - every
  // stage after Saved means you applied.
  function stageChanges(m, stage) {
    const changes = {stage};
    if (stage && !m.dates[stage]) changes[`date:${stage}`] = today();
    if (stage && stage !== "saved" && !m.applied) changes.applied = true;
    if ((!stage || stage === "saved") && m.applied) changes.applied = false;
    return changes;
  }

  function currentRows() {
    const q = $("q").value.trim().toLowerCase();
    const cat = $("cat").value, region = $("f-region").value, source = $("f-source").value;
    const status = $("f-status").value, sent = $("f-sent").value, days = Number($("f-days").value);
    const since = days ? Date.now() - days * 864e5 : 0;
    const rows = tab === "applied" ? (mine ? myRows().filter((r) => marksOf(r).applied) : [])
      : tab === "open" ? ALL.filter((r) => !r.sent)
      : tab === "new" ? ALL.filter(isNew) : ALL;
    return rows.filter((r) => {
      if (cat && r.k !== cat) return false;
      if (region && r.g !== region) return false;
      if (source && srcName(r) !== source) return false;
      if (sent && (sent === "yes") !== !!r.sent) return false;
      if (since && when(r) < since) return false;
      if (status === "open" || status === "closed") { if (r.st !== status) return false; }
      else if (status) {
        const m = marksOf(r);
        if (!(status === "saved" ? m.stage : m.vote === status)) return false;
      }
      return !q || `${r.t} ${r.c} ${r.l} ${r.s}`.toLowerCase().includes(q);
    });
  }
  const sources = [...new Set(ALL.map(srcName).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  $("f-source").insertAdjacentHTML("beforeend", sources.map((n) => `<option>${esc(n)}</option>`).join(""));

  const ICON = {
    applied: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--success)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Applied"><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/></svg>`,
    new: `<svg width="18" height="18" viewBox="0 0 24 24" role="img" aria-label="New"><circle cx="12" cy="12" r="5" fill="var(--accent)"/></svg>`,
    closed: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--danger)" stroke-width="2" stroke-linecap="round" role="img" aria-label="Closed"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>`,
    seen: `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" stroke-width="2" role="img" aria-label="Seen"><circle cx="12" cy="12" r="9"/></svg>`,
    save: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h12v18l-6-4-6 4z"/></svg>`,
  };
  const statusOf = (r) => (mine && marksOf(r).applied ? "applied" : r.st === "closed" ? "closed" : isNew(r) ? "new" : "seen");
  const seenText = (r) => (r.f ? relTime(r.f * 1000) : r.d ? shortDate(r.d) : "—");

  let visible = [];
  function saveButton(r, i) {
    const m = mine ? marksOf(r) : {};
    const on = !!m.stage, unsent = mine && m.waiting.stage === "unsent";
    const label = on ? `On your board: ${STAGE_NAMES[m.stage]}` : "Save to your application board";
    return `<button class="row-act square save-btn${unsent ? " unsent" : ""}" data-i="${i}" aria-pressed="${on}" title="${esc(label)}${unsent ? " · not sent yet - will retry" : ""}" aria-label="${esc(label)}">${ICON.save}</button>`;
  }
  function renderRows() {
    const locked = tab === "applied" && !mine;
    $("applied-locked").hidden = !locked;
    $("jobs-table").hidden = locked;
    const rows = locked ? [] : currentRows();
    const pages = Math.max(1, Math.ceil(rows.length / perPage));
    page = Math.min(Math.max(1, page), pages);
    const first = (page - 1) * perPage;
    visible = rows.slice(first, first + perPage);
    $("date-h").textContent = tab === "applied" ? "Marked" : "First seen";
    $("rows").innerHTML = visible.map((r, i) => `<tr class="${r.st === "closed" ? "closed" : ""}">
      <td><span class="st">${ICON[statusOf(r)]}</span></td>
      <td class="role">${esc(r.t)}</td>
      <td>${esc(r.c)}</td>
      <td class="loc">${esc(r.l) || "—"}</td>
      <td>${r.k ? `<span class="level">${LEVELS[r.k] || esc(r.k)}</span>` : ""}</td>
      <td class="seen num" title="${r.d ? esc(r.sent ? `Sent ${r.d}` : `Posted ${r.d}`) : ""}">${esc(seenText(r))}</td>
      <td class="src" title="${esc(r.s)}">${esc(srcName(r))}</td>
      <td>${saveButton(r, i)} <button class="kit-btn" data-i="${i}" title="Tailor your resume to this job">Make kit</button>
        <a class="row-act open" href="${esc(r.u)}" target="_blank" rel="noopener" data-i="${i}">Open ↗</a></td></tr>`).join("");
    $("empty").hidden = locked || rows.length > 0;
    $("empty").textContent = tab === "new" ? "Nothing new since your last visit." : tab === "applied" ? "No applications yet - mark a job Applied from its kit, or move it on your board." : "No jobs match.";
    const range = rows.length ? `${fmt(first + 1)}–${fmt(first + visible.length)}` : "0";
    $("jobs-count").textContent = locked ? "" : `Showing ${range} of ${fmt(rows.length)}`;
    $("pager").hidden = locked || !rows.length;
    $("page-info").textContent = `Page ${fmt(page)} of ${fmt(pages)}`;
    renderPageLinks(pages);
    for (const sel of document.querySelectorAll(".filters select")) sel.classList.toggle("set", !!sel.value);
    const nNew = newCount();
    $("tab-new").textContent = nNew ? `New (${fmt(nNew)})` : "New";
    $("tab-open").textContent = hasOpen ? `Not sent (${fmt(data.open.length)})` : "Not sent";
    $("mark-seen").disabled = !nNew;
  }
  function selectTab(name) {
    tab = name; page = 1;
    for (const t of ["all", "new", "applied", "open"]) $(`tab-${t}`).setAttribute("aria-selected", t === name);
    renderRows();
  }
  // Everything that shows marks or counts redraws together.
  function render() {
    renderProfile();
    renderRows();
    renderHome();
    renderNav();
    renderBoard();
    renderAlertsView();
    renderSources();
  }
  for (const id of ["cat", "f-region", "f-source", "f-status", "f-sent", "f-days"]) {
    $(id).addEventListener("change", () => { page = 1; renderRows(); });
  }
  $("q").addEventListener("input", () => { page = 1; renderRows(); });
  for (const t of ["all", "new", "applied", "open"]) $(`tab-${t}`).addEventListener("click", () => !$(`tab-${t}`).disabled && selectTab(t));
  // Page links: first, last, and two either side of the current page, with
  // gaps shown as "…".
  function renderPageLinks(pages) {
    const nums = [...new Set([1, pages, page - 2, page - 1, page, page + 1, page + 2])]
      .filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
    const link = (n, label, aria) => `<button type="button" class="page-btn" data-page="${n}"${aria}>${label}</button>`;
    let html = page > 1 ? link(page - 1, "‹ Prev", ` aria-label="Previous page"`) : "";
    nums.forEach((n, k) => {
      if (k && n - nums[k - 1] > 1) html += `<span class="page-gap" aria-hidden="true">…</span>`;
      html += link(n, fmt(n), n === page ? ` aria-current="page" aria-label="Page ${n}, current"` : ` aria-label="Page ${n}"`);
    });
    if (page < pages) html += link(page + 1, "Next ›", ` aria-label="Next page"`);
    $("page-links").innerHTML = html;
  }
  $("page-links").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-page]");
    if (!b) return;
    page = Number(b.dataset.page);
    renderRows();
    $("jobs-h").scrollIntoView({block: "start"});
  });
  $("per-page").value = String(perPage);
  $("per-page").addEventListener("change", () => {
    const firstRow = (page - 1) * perPage;  // keep the top row in view
    perPage = Number($("per-page").value);
    saved.set(PER_PAGE_KEY, perPage === 25 ? "" : String(perPage));
    page = Math.floor(firstRow / perPage) + 1;
    renderRows();
  });
  $("refresh").addEventListener("click", () => location.reload());
  // Opening a job (click, middle-click or Cmd-click) marks it seen. Under
  // the New tab it then drops out of the list, like an inbox.
  function markOpened(r) {
    if (!r || !isNew(r)) return;
    opened.add(r.i);
    saveOpened();
    setTimeout(render, 0);  // after the browser has followed the link
  }
  const openHandler = (e) => {
    const link = e.target.closest("a.open");
    if (!link || (e.type === "auxclick" && e.button !== 1)) return;
    markOpened(visible[Number(link.dataset.i)]);
  };
  $("rows").addEventListener("click", openHandler);
  $("rows").addEventListener("auxclick", openHandler);
  $("mark-seen").addEventListener("click", () => {
    cutoff = built;
    session.set(CUTOFF_KEY, String(cutoff));
    opened.clear();
    saveOpened();
    if (tab === "new") selectTab("all");
    render();
  });

  // --- home --------------------------------------------------------------------
  function renderHome() {
    $("greet-sub").textContent = `Hunter checked your boards ${relTime(generated)}. Here's where things stand.`;
    $("stat-new").textContent = fmt(newCount());
    const rows = mine ? myRows().map(marksOf) : null;
    $("stat-applied").textContent = rows ? fmt(rows.filter((m) => m.applied).length) : "—";
    $("stat-applied-sub").textContent = rows ? "Across every stage" : hasPrivate ? "Unlock to see" : "Private layer is off";
    const tex = resumeText();
    $("stat-resume").textContent = tex ? `v${Number(saved.get(TEX_REV)) || 1}` : "—";
    const at = Number(saved.get(TEX_AT));
    $("stat-resume-sub").textContent = tex ? (at ? `Edited ${relTime(at)}` : "Open the editor") : "Write it in LaTeX";

    const fresh = ALL.filter(isNew);
    const picks = (fresh.length ? fresh : ALL).slice(0, 5);
    $("fresh").innerHTML = picks.length ? picks.map((r) => `<a class="fresh" href="${esc(r.u)}" target="_blank" rel="noopener" data-id="${esc(r.i)}">
        <span class="initial" aria-hidden="true">${esc((r.c || "?").trim()[0] || "?")}</span>
        <span class="fresh-text"><span class="fresh-role">${esc(r.t)}</span><span class="fresh-meta">${esc([r.c, r.l].filter(Boolean).join(" · "))}</span></span>
        <time>${esc(seenText(r))}</time></a>`).join("")
      : `<p class="empty">No postings yet.</p>`;
    if (picks.length && !fresh.length) $("fresh").insertAdjacentHTML("afterbegin", `<p class="note" style="margin:0 0 8px">Nothing new since your last visit - here are the latest.</p>`);

    if (!rows) {
      $("pipeline").innerHTML = `<div class="locked"><p>${hasPrivate ? "Your pipeline is private. Unlock to see it." : "No private layer is published on this site."}</p>${hasPrivate ? `<button class="btn sm primary" data-unlock>Unlock</button>` : ""}</div>`;
      return;
    }
    const counts = STAGES.map((st) => rows.filter((m) => m.stage === st).length);
    const total = Math.max(1, ...counts);
    $("pipeline").innerHTML = STAGES.map((st, i) => `<div class="pipe">
        <span class="pipe-name">${STAGE_NAMES[st]}</span>
        <span class="pipe-track"><span class="pipe-fill" style="display:block;width:${counts[i] ? Math.max(4, Math.round(counts[i] / total * 100)) : 0}%;background:${STAGE_COLORS[st]}"></span></span>
        <span class="pipe-n num">${counts[i]}</span></div>`).join("");
  }
  const freshClick = (e) => {
    const a = e.target.closest("a.fresh");
    if (!a || (e.type === "auxclick" && e.button !== 1)) return;
    markOpened(ALL.find((r) => r.i === a.dataset.id));
  };
  $("fresh").addEventListener("click", freshClick);
  $("fresh").addEventListener("auxclick", freshClick);
  $("card-new").addEventListener("click", () => selectTab(newCount() ? "new" : "all"));

  // --- sidebar -----------------------------------------------------------------
  function renderNav() {
    const nNew = newCount();
    $("nav-new").hidden = !nNew;
    $("nav-new").textContent = `${fmt(nNew)} new`;
    const onBoard = mine ? myRows().filter((r) => marksOf(r).stage).length : 0;
    $("nav-apps").hidden = !onBoard;
    $("nav-apps").textContent = fmt(onBoard);
    const stale = Date.now() - generated > 3 * 3600e3;
    $("auto-poll-dot").className = `dot ${stale ? "warn" : "ok"}`;
    $("auto-poll").textContent = relTime(generated);
    const a = mine ? currentAlerts() : null;
    $("auto-alerts-dot").className = `dot ${!a ? "off" : a.paused ? "warn" : "ok"}`;
    $("auto-alerts").textContent = !a ? (hasPrivate ? "locked" : "") : a.paused ? "paused" : "on";
    $("auto-tex-dot").className = `dot ${{ok: "ok", error: "warn", running: "ok"}[texStatus] || "off"}`;
    $("auto-tex").textContent = {ok: "ok", error: "error", running: "running"}[texStatus] || "idle";
    $("me-state").textContent = mine ? "Unlocked" : hasPrivate ? "Locked" : "Public view";
    $("lock-shackle").setAttribute("d", mine ? "M8 11V7a4 4 0 0 1 7.5-2" : "M8 11V7a4 4 0 0 1 8 0v4");
    $("lock").title = mine ? "Lock" : "Unlock your data";
    $("lock").setAttribute("aria-label", mine ? "Lock" : "Unlock your data");
    $("open-voting").hidden = !(mine && data.inbox);
    for (const o of document.querySelectorAll("#f-status option[data-private]")) o.hidden = !mine;
  }
  $("settings").addEventListener("click", (e) => {
    e.stopPropagation();
    const open = $("settings-menu").hidden;
    $("settings-menu").hidden = !open;
    $("settings").setAttribute("aria-expanded", open);
  });
  document.addEventListener("click", (e) => {
    if (!$("settings-menu").hidden && !e.target.closest("#settings-menu")) {
      $("settings-menu").hidden = true;
      $("settings").setAttribute("aria-expanded", "false");
    }
  });
  $("menu-alerts").addEventListener("click", () => { location.hash = "#alerts"; });
  $("menu-sources").addEventListener("click", () => { location.hash = "#sources"; });
  $("nav-toggle").addEventListener("click", () => {
    const open = !$("app").classList.contains("nav-open");
    $("app").classList.toggle("nav-open", open);
    $("nav-toggle").setAttribute("aria-expanded", open);
  });
  function searchJobs(text) {
    $("q").value = text.trim();
    for (const id of ["cat", "f-region", "f-source", "f-status", "f-sent", "f-days"]) $(id).value = "";
    selectTab("all");
    location.hash = "#jobs";
  }
  $("gsearch").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); searchJobs($("gsearch").value); $("gsearch").value = ""; $("q").focus(); }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      $("app").classList.add("nav-open");
      $("gsearch").focus();
    }
  });

  // Private layer. Personal data ships only as AES-GCM ciphertext (see
  // scraper/private.py); this decrypts it in the browser with a key derived
  // from your passphrase. Nothing personal is in the page until then, and
  // the passphrase never leaves this browser. "Remember" stores the derived
  // key in IndexedDB as a non-extractable CryptoKey: usable for decrypting
  // (and sealing votes) here, but script can't read the key bytes back out.
  const sealed = JSON.parse($("private").textContent);
  const hasPrivate = !!(sealed && sealed.v === 1 && window.crypto && crypto.subtle);
  const KEY_DB = "hunter-private", KEY_STORE = "keys", KEY_ID = "dashboard";
  const bytes = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  // Identifies the KDF settings and key usages a remembered key was made
  // with; if the page changes either, the old key is simply not used.
  const kdfId = sealed ? `${sealed.kdf.hash}:${sealed.kdf.iterations}:${sealed.kdf.salt}:ed` : "";
  let unlockedKey = null;

  function keyStore(mode, act) {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(KEY_DB, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(KEY_STORE);
      req.onerror = () => reject(req.error);
      req.onsuccess = () => {
        const db = req.result, tx = db.transaction(KEY_STORE, mode);
        const op = act(tx.objectStore(KEY_STORE));
        tx.oncomplete = () => { db.close(); resolve(op.result); };
        tx.onerror = () => { db.close(); reject(tx.error); };
      };
    });
  }
  const remembered = () => keyStore("readonly", (s) => s.get(KEY_ID)).catch(() => null);
  const remember = (key) => keyStore("readwrite", (s) => s.put({kdf: kdfId, key}, KEY_ID)).catch(() => {});
  const forget = () => keyStore("readwrite", (s) => s.delete(KEY_ID)).catch(() => {});

  async function deriveKey(phrase) {
    // Same normalization as private.py, or the keys won't match.
    const raw = new TextEncoder().encode(phrase.trim().normalize("NFC"));
    const base = await crypto.subtle.importKey("raw", raw, "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      {name: "PBKDF2", hash: sealed.kdf.hash, salt: bytes(sealed.kdf.salt), iterations: sealed.kdf.iterations},
      base, {name: "AES-GCM", length: 256}, false, ["encrypt", "decrypt"]);
  }
  async function decrypt(key) {  // throws on a wrong key: GCM authenticates
    const plain = await crypto.subtle.decrypt({name: "AES-GCM", iv: bytes(sealed.iv)}, key, bytes(sealed.ct));
    return JSON.parse(new TextDecoder().decode(plain));
  }

  function showMine(payload, key) {
    unlockedKey = key;
    const status = new Map(data.history.map((r) => [r.i, r.st]));
    mine = payload.mine.map((r) => ({...r, st: status.get(r.i) || ""}));
    mineById = new Map(mine.map((r) => [r.i, r]));
    alertsPublished = payload.alerts || {};
    profileName = ((payload.profile || {}).name || "").trim();
    loadPending();
    fillAlerts(currentAlerts());
    render();
    sendPending();
  }
  function hideMine() {
    mine = null; mineById = new Map(); pending = {}; unlockedKey = null; alertsPublished = null; profileName = "";
    $("act-status").textContent = "";
    if ($("f-status").selectedOptions[0] && $("f-status").selectedOptions[0].dataset.private !== undefined) $("f-status").value = "";
    render();
  }

  // Sync: marks, board moves and settings go back to Hunter as encrypted
  // comments on one GitHub issue (see scraper/inbox.py for why an issue). A
  // click is saved in this browser first and only then posted, so a click
  // made offline or with a bad token is retried rather than lost. The run
  // that applies it republishes the page within minutes; until then this
  // browser overlays the click. The overlay goes once a published build
  // shows the run applied it (its `set` stamp is as new as the click), or
  // the build is two hours newer than the click - by then the published
  // state is the truth.
  const PENDING_KEY = "hunter.votes.pending", TOKEN_KEY = "hunter.github.token";
  const SETTLE_MS = 2 * 60 * 60 * 1000;
  function loadPending() {
    try { pending = JSON.parse(saved.get(PENDING_KEY) || "{}"); } catch { pending = {}; }
    for (const [k, p] of Object.entries(pending)) {
      const m = mineById.get(p.id);
      const stamp = p.field === "alerts" ? alertsPublished && alertsPublished.set_at
        : p.field === "source" ? (sourceByKey.get(p.id) || {}).set_at
        : m && m.set && m.set[p.field];
      if ((stamp && Date.parse(stamp) >= Date.parse(p.at)) || (p.sent && generated - Date.parse(p.at) > SETTLE_MS)) {
        delete pending[k];
      }
    }
    savePending();
  }
  const savePending = () => saved.set(PENDING_KEY, Object.keys(pending).length ? JSON.stringify(pending) : "");
  const b64 = (u8) => btoa(String.fromCharCode(...u8));

  async function sealAction(action) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({name: "AES-GCM", iv}, unlockedKey,
      new TextEncoder().encode(JSON.stringify(action)));
    return JSON.stringify({v: 1, iv: b64(iv), ct: b64(new Uint8Array(ct))});
  }
  function postError(status) {
    if (status === 401) return "GitHub rejected your token - check it under Settings → GitHub token.";
    if (status === 403 || status === 404) return `Your token can't comment on ${data.inbox.repo} issues - it needs Issues: Read and write on this repo.`;
    return `GitHub had a problem (${status}) - your changes are kept and will be retried.`;
  }
  let sending = null;
  function sendPending() {  // one sender at a time; a later call waits its turn
    sending = (sending || Promise.resolve()).then(sendAll, sendAll);
    return sending;
  }
  function syncStatus(text) {
    $("act-status").textContent = text;
    if (text) toast(esc(text));
  }
  async function sendAll() {
    const queue = Object.entries(pending).filter(([, p]) => !p.sent);
    if (!queue.length || !mine || !unlockedKey || !data.inbox) return;
    const token = saved.get(TOKEN_KEY);
    if (!token) { syncStatus("Your changes are saved in this browser - add a GitHub token under Settings to send them."); return; }
    // Everything queued goes in one comment, so one edit is one run.
    const actions = queue.map(([, p]) => ({id: p.id, field: p.field, value: p.value, at: p.at, row: p.row}));
    let res;
    try {
      res = await fetch(`https://api.github.com/repos/${data.inbox.repo}/issues/${data.inbox.issue}/comments`, {
        method: "POST",
        headers: {Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "Content-Type": "application/json"},
        body: JSON.stringify({body: await sealAction({actions})}),
      });
    } catch {
      syncStatus("Offline? Your changes are kept and will be sent when you're back.");
      return;
    }
    if (!res.ok) { syncStatus(postError(res.status)); return; }
    for (const [k, p] of queue) if (pending[k] && pending[k].at === p.at) pending[k].sent = true;
    savePending();
    $("act-status").textContent = "";
    render();
  }
  window.addEventListener("online", () => mine && sendPending());

  // Queue every change from one click or edit (one shared timestamp) and
  // send them together.
  function queueChanges(r, changes) {
    const at = new Date().toISOString(), row = {t: r.t, c: r.c, l: r.l, u: r.u, k: r.k, s: r.s, d: r.d};
    for (const [field, value] of Object.entries(changes)) {
      pending[`${r.i}|${field}`] = {id: r.i, field, value, at, sent: false, row};
    }
    savePending();
    render();
    if (!saved.get(TOKEN_KEY)) openVoting(); else sendPending();
  }
  // Marking needs the unlocked layer and somewhere to send the change.
  function canMark() {
    if (mine && data.inbox) return true;
    if (!hasPrivate) toast("Saving needs the private layer - set the HUNTER_PASSPHRASE secret.");
    else if (!mine) openUnlock();
    else toast("Sync isn't set up yet - Hunter creates its inbox issue on the next run.");
    return false;
  }
  function saveJob(r) {
    if (!canMark()) return;
    const m = marksOf(r);
    if (m.stage) { openCard(r); return; }  // already on the board: open its card
    queueChanges(r, stageChanges(m, "saved"));
    toast(`Saved ${esc(r.c)} - see it in <a href="#applications">Applications</a>`);
  }
  $("rows").addEventListener("click", (e) => {
    const btn = e.target.closest(".save-btn");
    if (btn) saveJob(visible[Number(btn.dataset.i)]);
  });
  // 👍 / 👎 / applied, from the job's kit window.
  function markClick(r, field, value) {
    if (!canMark()) return;
    const m = marksOf(r);
    if (field === "vote") {
      queueChanges(r, {vote: m.vote === value ? null : value});
    } else {
      // Same rule as Telegram's ✅ (feedback._board_follows_applied).
      const changes = {applied: !m.applied};
      if (!m.applied && (!m.stage || m.stage === "saved")) Object.assign(changes, stageChanges({...m, applied: "x"}, "applied"));
      if (m.applied && m.stage === "applied") changes.stage = null;
      queueChanges(r, changes);
    }
    renderKitMarks();
  }
  let toastTimer = null;
  function toast(html) {
    $("toast").innerHTML = html;
    $("toast").classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toast").classList.remove("on"), 4000);
  }

  // Telegram alert settings (scraper/alerts.py): which matched jobs ping
  // your phone. Saved like a vote - one encrypted inbox comment, applied by
  // the next run, which confirms in Telegram. The preview runs the same
  // rules as scraper.filters over the jobs already sent to you.
  let alertsPublished = null;
  const ALERT_KEY = "|alerts";
  const words = (s) => s.split(",").map((w) => w.trim()).filter(Boolean);
  const checked = (name) => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((i) => i.value);
  const currentAlerts = () => (pending[ALERT_KEY] ? pending[ALERT_KEY].value : alertsPublished || {});
  function formAlerts() {
    return {paused: !$("al-on").checked, categories: checked("al-cat"), regions: checked("al-reg"),
            title_keywords: words($("al-title").value), locations: words($("al-loc").value)};
  }
  function fillAlerts(a) {
    $("al-on").checked = !a.paused;
    for (const i of document.querySelectorAll('input[name="al-cat"]')) i.checked = (a.categories || []).includes(i.value);
    for (const i of document.querySelectorAll('input[name="al-reg"]')) i.checked = (a.regions || []).includes(i.value);
    $("al-title").value = (a.title_keywords || []).join(", ");
    $("al-loc").value = (a.locations || []).join(", ");
    renderAlertsView();
  }
  const sameAlerts = (a, b) => {
    const norm = (x) => JSON.stringify([!!x.paused, ...["categories", "regions", "title_keywords", "locations"]
      .map((k) => [...(x[k] || [])].map((w) => w.toLowerCase()).sort())]);
    return norm(a) === norm(b);
  };
  function alertMatch(r, a) {
    const has = (list) => list && list.length;
    if (has(a.categories) && !a.categories.includes(r.k)) return false;
    const loc = (r.l || "").trim().toLowerCase();
    if (has(a.regions) && !a.regions.includes(r.g) && loc && loc !== "remote") return false;
    if (has(a.title_keywords) && !a.title_keywords.some((w) => r.t.toLowerCase().includes(w.toLowerCase()))) return false;
    if (has(a.locations) && !a.locations.some((w) => loc.includes(w.toLowerCase()))) return false;
    return true;
  }
  function renderAlertsView() {
    $("alerts-card").hidden = !mine;
    $("alerts-locked").hidden = !!mine;
    if (!mine) return;
    const a = formAlerts();
    $("al-fields").setAttribute("aria-disabled", a.paused);
    const rows = data.history;
    if (a.paused) {
      $("al-preview").innerHTML = "<b>Paused.</b> Telegram sends no job alerts; new jobs still show up on the job board.";
    } else {
      const hits = rows.filter((r) => alertMatch(r, a));
      const perWeek = Math.round(hits.length / (90 / 7));
      $("al-preview").innerHTML = `<b>${fmt(hits.length)}</b> of the ${fmt(rows.length)} jobs sent to you in the last 90 days match`
        + (hits.length ? ` - about ${fmt(perWeek)} a week.` : ". Loosen a setting, or you'll get no alerts.")
        + (hits.length ? `<ul>${hits.slice(0, 6).map((r) => `<li>${esc(r.c)} - ${esc(r.t)}${r.l ? ` · ${esc(r.l)}` : ""}</li>`).join("")}</ul>` : "");
    }
    const p = pending[ALERT_KEY], dirty = !sameAlerts(a, currentAlerts());
    $("al-save").disabled = !dirty;
    const since = alertsPublished && alertsPublished.set_at;
    $("al-status").textContent = dirty ? "Unsaved changes."
      : p && !p.sent ? "Saved in this browser - not sent yet, will retry."
      : p ? "Sent. Hunter applies it on its next run (a few minutes) and confirms in Telegram."
      : since ? `In use since ${new Date(since).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"})}.`
      : "In use: every new match is sent (no settings saved yet).";
  }
  $("alerts-card").addEventListener("input", renderAlertsView);
  $("alerts-card").addEventListener("change", renderAlertsView);
  $("al-reset").addEventListener("click", () => fillAlerts({paused: false}));
  $("al-save").addEventListener("click", () => {
    if (!canMark()) return;
    pending[ALERT_KEY] = {id: "", field: "alerts", value: formAlerts(), at: new Date().toISOString(), sent: false, row: {}};
    savePending();
    render();
    if (!saved.get(TOKEN_KEY)) openVoting(); else sendPending();
  });

  // Sources (scraper/autosources.py): company boards Hunter polls directly
  // because their jobs kept reaching you late through the feeds. The list
  // is public data (like sources.yaml); Add / Remove / Resume go through the
  // inbox like a vote, so they need the page unlocked.
  const SRC = data.sources;
  const sourceByKey = new Map(SRC ? SRC.auto.map((r) => [r.key, r]) : []);
  const STATUS_TEXT = {active: "Polling", paused: "Paused - failing", requested: "Adding…",
                       failed: "Couldn't add", removed: "Removed"};
  function srcStatus(key, published) {
    const p = pending[`${key}|source`];
    if (!p) return {status: published, waiting: false};
    const status = {add: "requested", remove: "removed", resume: "active"}[p.value];
    return {status, waiting: true, unsent: !p.sent};
  }
  function srcButton(key, action, label) {
    return `<button class="src-btn" data-key="${esc(key)}" data-act="${action}">${label}</button>`;
  }
  function renderSources() {
    $("sources-locked").hidden = !!mine;
    if (!mine) { $("sources-card").hidden = $("suggest-card").hidden = true; return; }
    $("sources-card").hidden = $("suggest-card").hidden = !SRC;
    if (!SRC) { $("sources-locked").hidden = false; $("sources-locked").querySelector("p").textContent = "Available on the published site."; $("sources-locked").querySelector("button").hidden = true; return; }
    $("sources-sub").textContent = `Hunter polls these company boards itself, so new postings reach you within minutes instead of days later through the feeds. It adds one on its own once ${SRC.min_matched} matching jobs have arrived late from it (up to ${SRC.per_week} a week), or when a daily web search finds a board with jobs matching your filters ("search"), and tells you in Telegram. Plus ${fmt(SRC.configured)} boards from sources.yaml.`;
    const rows = SRC.auto.map((r) => ({...r, ...srcStatus(r.key, r.status)}));
    const shownRows = rows.filter((r) => r.status !== "removed"), removed = rows.filter((r) => r.status === "removed");
    $("auto-rows").innerHTML = shownRows.map((r) => `<tr>
      <td>${esc(r.name)}</td>
      <td class="muted">${esc(BOARD_NAMES[r.type] || r.type)}</td>
      <td class="muted">${r.added ? esc(shortDate(r.added.slice(0, 10))) : "—"}${r.how === "auto" || r.how === "search" ? ` · ${r.how}` : ""}</td>
      <td class="num">${fmt(r.caught)}</td>
      <td><span class="src-status ${r.status}">${STATUS_TEXT[r.status] || r.status}${r.unsent ? " (not sent yet)" : ""}</span></td>
      <td>${r.waiting ? "" : r.status === "paused" ? srcButton(r.key, "resume", "Resume") + " " + srcButton(r.key, "remove", "Remove")
        : r.status === "failed" ? srcButton(r.key, "add", "Try again") + " " + srcButton(r.key, "remove", "Dismiss")
        : r.status === "requested" ? "" : srcButton(r.key, "remove", "Remove")}</td></tr>`).join("");
    $("auto-empty").hidden = shownRows.length > 0;
    $("removed-wrap").hidden = !removed.length;
    $("removed-sum").textContent = `Removed (${removed.length}) - never suggested again`;
    $("removed-rows").innerHTML = removed.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.waiting ? "" : srcButton(r.key, "add", "Add back")}</td></tr>`).join("");
    const listed = new Set(SRC.auto.map((r) => r.key));
    const sugg = SRC.suggestions.filter((x) => !listed.has(x.key) || pending[`${x.key}|source`]);
    $("suggest-rows").innerHTML = sugg.map((x) => {
      const st = srcStatus(x.key, null);
      return `<tr><td>${esc(x.name)}</td><td class="num">${fmt(x.matched)} <span class="muted">of ${fmt(x.jobs)}</span></td>
        <td class="muted">${esc(BOARD_NAMES[x.key.split("/")[0]] || x.key.split("/")[0])}</td>
        <td>${st.waiting ? `<span class="src-status requested">${STATUS_TEXT[st.status]}</span>` : srcButton(x.key, "add", "Add")}</td></tr>`;
    }).join("");
    $("suggest-empty").hidden = sugg.length > 0;
  }
  $("view-sources").addEventListener("click", (e) => {
    const b = e.target.closest(".src-btn");
    if (!b || !canMark()) return;
    pending[`${b.dataset.key}|source`] = {id: b.dataset.key, field: "source", value: b.dataset.act,
      at: new Date().toISOString(), sent: false, row: {}};
    savePending();
    renderSources();
    $("sources-status").textContent = "Sent to Hunter - the next run applies it and confirms in Telegram.";
    if (!saved.get(TOKEN_KEY)) openVoting(); else sendPending();
  });

  // Application board. Built from the same marks as the job rows, so a move
  // shows at once and syncs through the inbox like a vote.
  let boardRows = new Map();
  function renderBoard() {
    $("board").hidden = !mine;
    $("apps-locked").hidden = !!mine;
    if (!mine) { $("board").innerHTML = ""; return; }
    const now = today();
    const cards = myRows().map((r) => ({r, m: marksOf(r)})).filter((x) => x.m.stage);
    boardRows = new Map(cards.map((x) => [x.r.i, x.r]));
    $("board").innerHTML = STAGES.map((st) => {
      const col = cards.filter((x) => x.m.stage === st)
        .sort((a, b) => (b.m.dates[st] || "").localeCompare(a.m.dates[st] || ""));
      return `<div class="bcol" data-stage="${st}" aria-label="${STAGE_NAMES[st]}">
        <h3><span class="dot" style="background:${STAGE_COLORS[st]}"></span><span>${STAGE_NAMES[st]}</span><span class="n num">${col.length}</span></h3>
        ${col.map(({r, m}) => boardCard(r, m, st, now)).join("") || `<p class="bempty">${st === "saved" ? "Save a job from the job board" : "Nothing yet"}</p>`}
      </div>`;
    }).join("");
  }
  function boardCard(r, m, st, now) {
    const since = m.dates[st];
    const days = since ? Math.round((Date.parse(now) - Date.parse(since)) / 864e5) : null;
    const whenText = since ? `${STAGE_NAMES[st]} ${shortDate(since)}${days > 0 ? ` · ${days}d ago` : " · today"}` : "No date";
    const unsent = Object.values(m.waiting).includes("unsent");
    return `<article class="bcard${unsent ? " unsent" : ""}" draggable="true" tabindex="0" data-id="${esc(r.i)}"
        title="${unsent ? "Not sent yet - will retry" : "Click to edit"}">
      <span class="bco">${esc(r.c)}</span>
      <a href="${esc(r.u)}" target="_blank" rel="noopener">${esc(r.t)}</a>
      <span class="bmeta">${esc(whenText)}</span>
      ${m.notes ? `<p class="bnote">${esc(m.notes.split("\n")[0])}</p>` : ""}
    </article>`;
  }
  const cardOf = (el) => { const c = el.closest(".bcard"); return c && boardRows.get(c.dataset.id); };
  $("board").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;  // the link opens the posting
    const r = cardOf(e.target);
    if (r) openCard(r);
  });
  $("board").addEventListener("keydown", (e) => {
    const r = e.key === "Enter" && cardOf(e.target);
    if (r) { e.preventDefault(); openCard(r); }
  });
  $("board").addEventListener("dragstart", (e) => {
    const c = e.target.closest(".bcard");
    if (c) { e.dataTransfer.setData("text/plain", c.dataset.id); e.dataTransfer.effectAllowed = "move"; }
  });
  $("board").addEventListener("dragover", (e) => {
    const col = e.target.closest(".bcol");
    if (!col) return;
    e.preventDefault();
    document.querySelectorAll(".bcol.over").forEach((c) => c !== col && c.classList.remove("over"));
    col.classList.add("over");
  });
  $("board").addEventListener("dragleave", (e) => {
    const col = e.target.closest(".bcol");
    if (col && !col.contains(e.relatedTarget)) col.classList.remove("over");
  });
  $("board").addEventListener("drop", (e) => {
    const col = e.target.closest(".bcol");
    if (!col) return;
    e.preventDefault();
    col.classList.remove("over");
    const r = boardRows.get(e.dataTransfer.getData("text/plain"));
    const m = r && marksOf(r);
    if (m && m.stage !== col.dataset.stage && canMark()) queueChanges(r, stageChanges(m, col.dataset.stage));
  });

  let cardRow = null, cardStage = null;
  function renderCardStages() {
    $("card-stages").innerHTML = STAGES.map((st) =>
      `<button type="button" role="radio" data-s="${st}" aria-checked="${st === cardStage}">${STAGE_NAMES[st]}</button>`).join("");
  }
  function openCard(r) {
    cardRow = r;
    const m = marksOf(r);
    cardStage = m.stage || "saved";
    $("card-h").textContent = r.t;
    $("card-meta").textContent = [r.c, r.l].filter(Boolean).join(" · ");
    $("card-link").href = r.u;
    renderCardStages();
    $("card-dates").innerHTML = STAGES.map((st) =>
      `<label>${STAGE_NAMES[st]}<input type="date" data-s="${st}" value="${esc(m.dates[st] || "")}"></label>`).join("");
    $("card-notes").value = m.notes;
    $("card-remove").hidden = !m.stage;
    $("card").showModal();
  }
  $("card-stages").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-s]");
    if (b) { cardStage = b.dataset.s; renderCardStages(); }
  });
  $("card-cancel").addEventListener("click", () => $("card").close());
  $("card-save").addEventListener("click", () => {
    const m = marksOf(cardRow), changes = {};
    if (cardStage !== m.stage) Object.assign(changes, stageChanges(m, cardStage));
    // A date typed here wins over the automatic "today".
    for (const input of $("card-dates").querySelectorAll("input")) {
      const field = `date:${input.dataset.s}`, value = input.value || null;
      if (value !== (m.dates[input.dataset.s] || null)) changes[field] = value;
    }
    const notes = $("card-notes").value.trim();
    if (notes !== m.notes) changes.notes = notes || null;
    $("card").close();
    if (Object.keys(changes).length && canMark()) queueChanges(cardRow, changes);
  });
  $("card-remove").addEventListener("click", () => {
    if (!confirm(`Remove ${cardRow.c} - ${cardRow.t} from your board? Its notes are kept.`)) return;
    $("card").close();
    if (canMark()) queueChanges(cardRow, stageChanges(marksOf(cardRow), null));
  });

  function openVoting() {
    $("voting-token").value = saved.get(TOKEN_KEY);
    $("voting-repo").textContent = data.inbox ? data.inbox.repo : "this repo";
    if (!$("voting").open) $("voting").showModal();
  }
  $("open-voting").addEventListener("click", openVoting);
  $("voting-cancel").addEventListener("click", () => $("voting").close());
  $("voting-save").addEventListener("click", () => {
    saved.set(TOKEN_KEY, $("voting-token").value.trim());
    $("voting").close();
    sendPending();
  });
  $("voting-clear").addEventListener("click", () => { saved.set(TOKEN_KEY, ""); $("voting-token").value = ""; });

  function openUnlock() {
    if (!hasPrivate) return;
    $("unlock-pass").value = ""; $("unlock-error").textContent = "";
    $("unlock").showModal();
  }
  document.addEventListener("click", (e) => { if (e.target.closest("[data-unlock]")) openUnlock(); });
  if (hasPrivate) {
    $("lock").hidden = false;
    $("lock").addEventListener("click", () => {
      if (mine) { forget(); hideMine(); return; }
      openUnlock();
    });
    $("unlock-cancel").addEventListener("click", () => $("unlock").close());
    $("unlock-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      $("unlock-go").disabled = true;
      $("unlock-error").textContent = "";
      try {
        const key = await deriveKey($("unlock-pass").value);
        let payload;
        try { payload = await decrypt(key); }
        catch { $("unlock-error").textContent = "That passphrase doesn't match."; return; }
        if ($("unlock-remember").checked) await remember(key);
        $("unlock-pass").value = "";
        $("unlock").close();
        showMine(payload, key);
      } finally {
        $("unlock-go").disabled = false;
      }
    });
    // Unlock silently with a remembered key. If the passphrase was changed
    // since, that key no longer opens the page: drop it and stay locked.
    remembered().then(async (rec) => {
      if (!rec || rec.kdf !== kdfId) return;
      let payload;
      try { payload = await decrypt(rec.key); }
      catch { forget(); return; }
      showMine(payload, rec.key);
    });
  } else {
    // No private layer published: say so instead of offering an unlock.
    for (const id of ["apps-locked", "alerts-locked", "sources-locked", "applied-locked"]) {
      $(id).querySelector("p").textContent = "No private layer is published on this site (the HUNTER_PASSPHRASE secret isn't set).";
      $(id).querySelector("button").hidden = true;
    }
  }

  // --- routing -------------------------------------------------------------------
  // Each section is a hash route in this one file, so they all share the
  // unlock, the data and the queue of unsent changes.
  const ROUTES = {home: "Hunter", jobs: "Job board", applications: "Applications", resume: "Resume",
                  chat: "Chat", alerts: "Telegram alerts", sources: "Sources"};
  let current = null;
  function route() {
    const name = ROUTES[location.hash.slice(1)] ? location.hash.slice(1) : "home";
    for (const r of Object.keys(ROUTES)) $(`view-${r}`).hidden = r !== name;
    for (const a of document.querySelectorAll("[data-route]")) {
      if (a.dataset.route === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    document.title = name === "home" ? "Hunter" : `${ROUTES[name]} · Hunter`;
    $("app").classList.remove("nav-open");
    $("nav-toggle").setAttribute("aria-expanded", "false");
    $("toast").classList.remove("on");
    if (name !== current) window.scrollTo(0, 0);
    current = name;
    if (name === "resume") {
      loadEditor();
      const e = tabEntry(activeId);
      if (!e.pdfUrl && !e.error && !compiling && autoCompile() && (!isResume(activeId) || resumeText())) compileTex();
    }
    if (name === "home") renderHome();
  }
  window.addEventListener("hashchange", route);

  // --- resume editor --------------------------------------------------------------
  // The LaTeX source is your one resume: Make kit and Chat send it to Claude.
  // It lives only in this browser's storage, like the API key - the page is
  // public and ships none of it. CodeMirror is fetched (pinned, with SRI) the
  // first time the editor opens; until then, or if that fails, it is a
  // plain textarea.
  const TEX_KEY = "hunter.resume.tex", TEX_AT = "hunter.resume.savedAt", TEX_REV = "hunter.resume.rev";
  const OLD_RESUME_KEY = "hunter.kit.resume";  // the plain-text resume from the old Kit setup
  if (!saved.get(TEX_KEY) && saved.get(OLD_RESUME_KEY)) {
    saved.set(TEX_KEY, `% Moved here from Kit setup (plain text). Make kit and Chat read whatever is\n% in this editor - replace it with your LaTeX source whenever you like.\n\n${saved.get(OLD_RESUME_KEY)}`);
    saved.set(OLD_RESUME_KEY, "");
  }
  const STARTER = String.raw`\documentclass[letterpaper,11pt]{article}
\usepackage[margin=0.6in]{geometry}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\pagestyle{empty}
\setlength{\parindent}{0pt}

\begin{document}

\begin{center}
  {\Huge \textbf{Your Name}} \\[2pt]
  you@example.com $|$ github.com/you $|$ linkedin.com/in/you
\end{center}

\section*{Education}
\textbf{University} \hfill City \\
\textit{B.Sc. Computer Science} \hfill Expected 2027

\section*{Experience}
\textbf{Company} \hfill City \\
\textit{Role} \hfill Dates
\begin{itemize}[leftmargin=*, nosep]
  \item What you built, and the result it had.
\end{itemize}

\section*{Projects}
\textbf{Project} \hfill Tech stack
\begin{itemize}[leftmargin=*, nosep]
  \item What it does and why it matters.
\end{itemize}

\section*{Technical Skills}
Languages: \\
Tools:

\end{document}
`;
  const resumeText = () => saved.get(TEX_KEY);

  // Templates: named LaTeX sources, kept in this browser like the resume.
  // The starter is added once, as an ordinary template you can rename or
  // delete.
  const TPL_KEY = "hunter.resume.templates";
  const loadTemplates = () => { try { return JSON.parse(saved.get(TPL_KEY) || "[]"); } catch { return []; } };
  let templates = loadTemplates();
  const SEEDED_KEY = "hunter.resume.starterAdded";
  if (!saved.get(SEEDED_KEY)) {
    templates.push({id: "starter", name: "Starter", tex: STARTER, at: 0});
    saved.set(TPL_KEY, JSON.stringify(templates));
    saved.set(SEEDED_KEY, "1");
  }
  const saveTemplates = () => saved.set(TPL_KEY, templates.length ? JSON.stringify(templates) : "");
  const tplById = (id) => templates.find((x) => x.id === id);

  // Tabs, like an IDE: resume.tex (your resume - what Make kit and Chat
  // read) is always open; a template opens in a tab of its own and you edit
  // the template itself. Each tab keeps its own CodeMirror document, last
  // PDF and log. Which tabs are open is remembered.
  const RESUME_TAB = "resume", TABS_KEY = "hunter.resume.tabs";
  const tabs = new Map();  // id -> {doc, text, pdfUrl, log, error}
  let openIds = [RESUME_TAB], activeId = RESUME_TAB;
  try {
    const kept = JSON.parse(saved.get(TABS_KEY) || "{}");
    openIds = [RESUME_TAB, ...(kept.open || []).filter((id) => id !== RESUME_TAB && tplById(id))];
    if (openIds.includes(kept.active)) activeId = kept.active;
  } catch { /* start with just the resume */ }
  const isResume = (id) => id === RESUME_TAB;
  const tabName = (id) => (isResume(id) ? "resume.tex" : (tplById(id) || {}).name || "template");
  const fileBase = (id) => (isResume(id) ? "resume" : tabName(id).replace(/[^\w.-]+/g, "-").replace(/^-+|-+$/g, "") || "template");
  const sourceOf = (id) => (isResume(id) ? resumeText() || STARTER : (tplById(id) || {}).tex || "");
  function tabEntry(id) {
    if (!tabs.has(id)) tabs.set(id, {doc: null, text: sourceOf(id), pdfUrl: null, log: "", error: null});
    return tabs.get(id);
  }
  const tabText = (id) => { const e = tabs.get(id); return e ? (e.doc ? e.doc.getValue() : e.text) : sourceOf(id); };
  let cm = null, editorLoading = false, texTimer = null, dirtyId = null, bumped = false;
  const texValue = () => tabText(activeId);
  function texState(text) {
    $("tex-state").textContent = `${isResume(activeId) ? "resume.tex" : `${tabName(activeId)} · template`} · ${text}`;
  }
  function saveTab(id) {
    const text = tabText(id);
    if (isResume(id)) {
      saved.set(TEX_KEY, text);
      saved.set(TEX_AT, String(Date.now()));
      if (!bumped) { saved.set(TEX_REV, String((Number(saved.get(TEX_REV)) || 0) + 1)); bumped = true; }
    } else {
      const t = tplById(id);
      if (!t) return;
      t.tex = text;
      t.edited = Date.now();
      saveTemplates();
    }
  }
  function flushSave() {
    clearTimeout(texTimer);
    if (dirtyId) { saveTab(dirtyId); dirtyId = null; }
  }
  function onTexInput() {
    if (!cm) tabEntry(activeId).text = $("tex").value;
    dirtyId = activeId;  // saved to the tab it was typed in, even after a switch
    texState("saving…");
    clearTimeout(texTimer);
    texTimer = setTimeout(() => {
      flushSave();
      texState("saved in this browser");
      renderTemplates();
      if (autoCompile()) compileSoon();
    }, 500);
  }
  $("tex").addEventListener("input", onTexInput);
  function renderTabs() {
    $("file-tabs").innerHTML = openIds.map((id) => {
      const name = esc(tabName(id)), on = id === activeId;
      return `<span class="ftab${on ? " on" : ""}${isResume(id) ? "" : " tpl-tab"}">
        <button class="ftab-name" role="tab" aria-selected="${on}" data-tab="${esc(id)}" title="${isResume(id) ? "Your resume - what Make kit and Chat use" : `Template: ${name}`}">${name}</button>
        ${isResume(id) ? "" : `<button class="ftab-x" data-close="${esc(id)}" aria-label="Close ${name}" title="Close">×</button>`}</span>`;
    }).join("") + (isResume(activeId) ? "" : `<button class="btn sm use-resume" id="tpl-apply" title="Copy this template into resume.tex">Use as resume</button>`);
  }
  function saveTabsState() {
    saved.set(TABS_KEY, openIds.length > 1 || !isResume(activeId) ? JSON.stringify({open: openIds, active: activeId}) : "");
  }
  function showTab(id) {
    if (id !== RESUME_TAB && !tplById(id)) return;
    flushSave();
    const e = tabEntry(id);
    if (!openIds.includes(id)) openIds.push(id);
    activeId = id;
    if (cm) {
      if (!e.doc) e.doc = window.CodeMirror.Doc(e.text, "stex");
      if (cm.getDoc() !== e.doc) cm.swapDoc(e.doc);
    } else {
      $("tex").value = e.text;
    }
    renderTabs();
    renderTemplates();
    showPdf();
    saveTabsState();
    texState(isResume(id) && !resumeText() ? "starter template - not saved until you edit" : "saved in this browser");
    if (!e.pdfUrl && !e.error && current === "resume" && autoCompile() && (!isResume(id) || resumeText())) compileSoon();
  }
  function closeTab(id) {
    if (isResume(id) || !openIds.includes(id)) return;
    if (dirtyId === id) flushSave();
    const at = openIds.indexOf(id);
    openIds.splice(at, 1);
    const e = tabs.get(id);
    if (e && e.pdfUrl) URL.revokeObjectURL(e.pdfUrl);
    tabs.delete(id);
    if (activeId === id) showTab(openIds[Math.min(at, openIds.length - 1)]);
    else { renderTabs(); renderTemplates(); saveTabsState(); }
  }
  $("file-tabs").addEventListener("click", (e) => {
    const close = e.target.closest("[data-close]"), tab = e.target.closest("[data-tab]");
    if (close) closeTab(close.dataset.close);
    else if (tab) showTab(tab.dataset.tab);
    else if (e.target.closest("#tpl-apply")) useAsResume(activeId);
  });
  $("file-tabs").addEventListener("auxclick", (e) => {  // middle-click closes, as in an IDE
    const tab = e.button === 1 && e.target.closest("[data-tab]");
    if (tab) { e.preventDefault(); closeTab(tab.dataset.tab); }
  });
  // Copies a template into resume.tex - the one way a template becomes the
  // resume that Make kit and Chat use.
  function useAsResume(id) {
    const t = tplById(id);
    if (!t || !confirm(`Replace resume.tex with "${t.name}"? Save your current resume as a template first if you want to keep it.`)) return;
    flushSave();
    const r = tabEntry(RESUME_TAB), text = tabText(id);
    if (r.doc) r.doc.setValue(text); else r.text = text;
    saveTab(RESUME_TAB);
    r.error = null;
    showTab(RESUME_TAB);
    toast(`resume.tex now holds "${esc(t.name)}"`);
    if (autoCompile()) compileSoon();
  }
  $("tex").value = tabEntry(activeId).text;

  const CM = "https://cdn.jsdelivr.net/npm/codemirror@5.65.21";
  const loadScript = (src, integrity) => new Promise((resolve, reject) => {
    const el = document.createElement("script");
    Object.assign(el, {src, integrity, crossOrigin: "anonymous", onload: resolve, onerror: reject});
    document.head.append(el);
  });
  async function loadEditor() {
    if (cm || editorLoading) return;
    editorLoading = true;
    const css = document.createElement("link");
    Object.assign(css, {rel: "stylesheet", href: `${CM}/lib/codemirror.min.css`, crossOrigin: "anonymous",
      integrity: "sha384-5051nNF3/zZWmqR8xLbnEemOtE5aiJ1EQkEq9vNXYOnGP/3BgYGknkOKuic0TtLg"});
    document.head.append(css);
    try {
      await loadScript(`${CM}/lib/codemirror.min.js`, "sha384-0g/X0mKSHhn466L6gmwQ9T6fCIeXJnwNSxvvJ0BdQtCDDS0FexoLx+DrxcZEZzEg");
      await loadScript(`${CM}/mode/stex/stex.min.js`, "sha384-Qg7Ah3Bzo+N+IgWb3JvRtDVOkqbGdpvskWvAo7gCjJGO/Owdh8uTqcuCS7DZp/AD");
      cm = window.CodeMirror.fromTextArea($("tex"), {mode: "stex", lineNumbers: true, lineWrapping: true, indentUnit: 2});
      tabEntry(activeId).doc = cm.getDoc();
      cm.on("change", onTexInput);
    } catch {
      // Plain textarea it is: it saves the same way.
    }
  }
  function download(url, name) {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
  }
  $("tex-download").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([texValue()], {type: "application/x-tex"}));
    download(url, `${fileBase(activeId)}.tex`);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // Compiling. pdfLaTeX runs right here, as WebAssembly in a worker (the
  // Siglum engine): the source never leaves the browser. Only the engine,
  // its TeX Live bundles and any extra packages are downloaded - by package
  // name, from packages.siglum.org - and cached by the browser for next time.
  // It compiles the open tab; each tab keeps its own PDF.
  const SIGLUM = "https://cdn.jsdelivr.net/npm/@siglum/engine@0.2.2";
  const SIGLUM_WORKER_SHA384 = "OwmVo5W8mu/m1D3AtZ5UtXpDNsMXfTB1vDgGnj7Tstr/seZtyrUMLxLqXmUBR697";
  const AUTO_KEY = "hunter.resume.autoCompile";
  const autoCompile = () => saved.get(AUTO_KEY) !== "off";
  $("tex-auto").checked = autoCompile();
  $("tex-auto").addEventListener("change", () => {
    saved.set(AUTO_KEY, $("tex-auto").checked ? "" : "off");
    if ($("tex-auto").checked) compileSoon();
  });
  let compiler = null, compiling = false, compileAgain = false, compileTimer = null;
  let texStatus = "idle";  // idle | running | ok | error
  async function siglum() {
    if (compiler) return compiler;
    const { SiglumCompiler, resolveRuntime } = await import(`${SIGLUM}/+esm`);
    // The worker is loaded from a Blob URL (a cross-origin script can't be a
    // Worker), so check it against its pinned hash first, as SRI would.
    const code = await (await fetch(`${SIGLUM}/src/worker.js`)).arrayBuffer();
    const digest = btoa(String.fromCharCode(...new Uint8Array(await crypto.subtle.digest("SHA-384", code))));
    if (digest !== SIGLUM_WORKER_SHA384) throw new Error("The TeX engine's worker didn't match its pinned hash.");
    const workerUrl = URL.createObjectURL(new Blob([code], {type: "text/javascript"}));
    const c = new SiglumCompiler({...await resolveRuntime(), workerUrl, eagerBundles: {pdflatex: ["cm-super"]},
      onProgress: (stage) => { if (compiling) texState(`compiling… (${stage})`); }});
    await c.init();
    compiler = c;
    return c;
  }
  function compileSoon() {
    clearTimeout(compileTimer);
    compileTimer = setTimeout(compileTex, 1200);
  }
  // Pulls the first "! ..." error out of a TeX log, with its line number.
  function texError(log) {
    const lines = (log || "").split("\n");
    const at = lines.findIndex((l) => l.startsWith("!"));
    if (at < 0) return "";
    const lineNo = lines.slice(at, at + 12).map((l) => l.match(/^l\.(\d+)/)).find(Boolean);
    return `${lines[at].slice(1).trim()}${lineNo ? ` (line ${lineNo[1]})` : ""}`;
  }
  // The preview shows the open tab's last good PDF; while it has none, its
  // last error (or how to start).
  function showPdf() {
    const e = tabEntry(activeId);
    $("pdf-download").disabled = !e.pdfUrl;
    $("tex-log").textContent = e.log;
    $("tex-log-wrap").hidden = !e.log;
    if (e.pdfUrl) {
      $("pdf-frame").src = `${e.pdfUrl}#toolbar=0&navpanes=0&view=FitH`;
      $("pdf-frame").hidden = false;
      $("paper").hidden = true;
      return;
    }
    $("pdf-frame").hidden = true;
    $("pdf-frame").removeAttribute("src");
    $("paper").hidden = false;
    $("paper").classList.toggle("error", !!e.error);
    $("paper-h").textContent = e.error ? e.error.title : "PDF preview";
    $("paper-msg").textContent = e.error ? e.error.message
      : `Press Recompile to build this PDF.${compiler ? "" : " The first compile downloads the TeX engine (about 40 MB, cached after that)."}`;
  }
  async function compileTex() {
    clearTimeout(compileTimer);
    if (compiling) { compileAgain = true; return; }
    compiling = true;
    texStatus = "running";
    renderNav();
    $("tex-compile").disabled = true;
    const id = activeId, source = texValue();
    texState(compiler ? "compiling…" : "loading the TeX engine… (first time: ~40 MB)");
    const started = performance.now();
    const failed = (title, message) => {
      const e = tabs.get(id);
      if (!e) return;
      // The last good PDF stays up while you fix an error, like Overleaf.
      if (e.pdfUrl) { if (id === activeId) toast(`${esc(title)}: ${esc(message)}`); }
      else e.error = {title, message};
    };
    try {
      const c = await siglum();
      const result = await c.compile(source, {engine: "pdflatex"});
      const e = tabs.get(id);
      if (e) e.log = result.log || "";
      if (result.success && result.pdf) {
        if (e) {
          if (e.pdfUrl) URL.revokeObjectURL(e.pdfUrl);
          e.pdfUrl = URL.createObjectURL(new Blob([result.pdf], {type: "application/pdf"}));
          e.error = null;
        }
        texStatus = "ok";
        if (id === activeId) texState(`compiled in ${((performance.now() - started) / 1000).toFixed(1)}s`);
      } else {
        texStatus = "error";
        failed("Couldn't compile", texError(result.log) || result.error || "pdfLaTeX stopped without a PDF. See the compile log below.");
        if (id === activeId) texState("compile error - see the preview");
      }
    } catch (err) {
      texStatus = "error";
      failed("Couldn't load the TeX engine", `${err && err.message ? err.message : err}. Check your connection and try Recompile again.`);
      if (id === activeId) texState("compile failed");
    } finally {
      compiling = false;
      $("tex-compile").disabled = false;
      if (id === activeId) showPdf();
      renderNav();
      if (compileAgain) { compileAgain = false; compileTex(); }
    }
  }
  $("tex-compile").addEventListener("click", compileTex);
  document.addEventListener("keydown", (e) => {
    if (current === "resume" && (e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "Enter")) {
      e.preventDefault();
      compileTex();
    }
  });
  $("pdf-download").addEventListener("click", () => {
    const e = tabs.get(activeId);
    if (e && e.pdfUrl) download(e.pdfUrl, `${fileBase(activeId)}.pdf`);
  });

  const ICON_EDIT = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>`;
  const ICON_DEL = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/></svg>`;
  function renderTemplates() {
    const item = (t) => `<li class="tpl${t.id === activeId ? " on" : openIds.includes(t.id) ? " open" : ""}">
        <button class="tpl-use" data-use="${esc(t.id)}" title="Open ${esc(t.name)} in a tab"><span>${esc(t.name)}</span><small>${
          t.edited ? esc(`Edited ${relTime(t.edited)}`) : t.at ? esc(`Saved ${relTime(t.at)}`) : "Starter template"}</small></button>
        <button class="icon-btn" data-rename="${esc(t.id)}" aria-label="Rename ${esc(t.name)}">${ICON_EDIT}</button>
        <button class="icon-btn" data-delete="${esc(t.id)}" aria-label="Delete ${esc(t.name)}">${ICON_DEL}</button></li>`;
    $("tpl-list").innerHTML = templates.length ? [...templates].sort((a, b) => b.at - a.at).map(item).join("")
      : `<li class="note">No templates yet.</li>`;
  }
  let tplEditing = null;  // a template's id when renaming, null when saving a new one
  function openTemplateDialog(tpl) {
    tplEditing = tpl ? tpl.id : null;
    $("tpl-dialog-h").textContent = tpl ? "Rename template" : "Save as template";
    $("tpl-ok").textContent = tpl ? "Rename" : "Save";
    $("tpl-name").value = tpl ? tpl.name : "";
    $("tpl-note").textContent = tpl ? "" : "Saves what's in the open tab now. Saving under an existing name replaces that template.";
    $("tpl-dialog").showModal();
    $("tpl-name").select();
  }
  $("tpl-save").addEventListener("click", () => openTemplateDialog(null));
  $("tpl-cancel").addEventListener("click", () => $("tpl-dialog").close());
  $("tpl-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("tpl-name").value.trim();
    if (!name) return;
    if (tplEditing) {
      const t = tplById(tplEditing);
      if (t) t.name = name;
    } else {
      flushSave();
      const text = texValue(), same = templates.find((x) => x.name.toLowerCase() === name.toLowerCase());
      if (same) {
        if (same.id !== activeId && !confirm(`Replace the template "${same.name}" with what's in this tab?`)) return;
        same.tex = text;
        same.edited = Date.now();
        const open = tabs.get(same.id);  // keep its tab, if open, in step
        if (open && same.id !== activeId) { if (open.doc) open.doc.setValue(text); else open.text = text; }
      } else {
        templates.push({id: `t${Date.now().toString(36)}`, name, tex: text, at: Date.now()});
      }
      toast(`Saved template "${esc(name)}"`);
    }
    saveTemplates();
    $("tpl-dialog").close();
    renderTemplates();
    renderTabs();
  });
  $("tpl-list").addEventListener("click", (e) => {
    const use = e.target.closest("[data-use]"), ren = e.target.closest("[data-rename]"), del = e.target.closest("[data-delete]");
    if (use) {
      showTab(use.dataset.use);
    } else if (ren) {
      openTemplateDialog(tplById(ren.dataset.rename));
    } else if (del) {
      const t = tplById(del.dataset.delete);
      if (t && confirm(`Delete the template "${t.name}"? Your resume isn't affected.`)) {
        if (dirtyId === t.id) dirtyId = null;
        closeTab(t.id);
        templates = templates.filter((x) => x !== t);
        saveTemplates();
        renderTemplates();
      }
    }
  });
  renderTabs();
  renderTemplates();
  texState(isResume(activeId) && !resumeText() ? "starter template - not saved until you edit" : "saved in this browser");

  // The templates panel folds to a thin strip; the choice is kept.
  const TPL_OPEN_KEY = "hunter.resume.templatesCollapsed";
  function setTemplatesOpen(open) {
    $("tpl-pane").classList.toggle("collapsed", !open);
    $("tpl-toggle").setAttribute("aria-expanded", open);
    const label = open ? "Collapse templates" : "Expand templates";
    $("tpl-toggle").setAttribute("aria-label", label);
    $("tpl-toggle").title = label;
    saved.set(TPL_OPEN_KEY, open ? "" : "1");
    if (cm) setTimeout(() => cm.refresh(), 0);  // the editor got wider or narrower
  }
  setTemplatesOpen(!saved.get(TPL_OPEN_KEY));
  $("tpl-toggle").addEventListener("click", () => setTemplatesOpen($("tpl-pane").classList.contains("collapsed")));

  // Drag the handle under the panes to set their height. Pointer capture
  // keeps the drag going over the PDF frame; arrow keys work too, and a
  // double-click goes back to filling the window.
  const PANE_H_KEY = "hunter.resume.paneHeight", PANE_MIN = 360, PANE_MAX = 3000;
  function setPaneHeight(px, keep = true) {
    if (px == null) $("tex-split").style.removeProperty("--pane-h");
    else $("tex-split").style.setProperty("--pane-h", `${Math.round(Math.min(PANE_MAX, Math.max(PANE_MIN, px)))}px`);
    if (keep) saved.set(PANE_H_KEY, px == null ? "" : String(Math.round(Math.min(PANE_MAX, Math.max(PANE_MIN, px)))));
    if (cm) cm.refresh();
  }
  if (Number(saved.get(PANE_H_KEY))) setPaneHeight(Number(saved.get(PANE_H_KEY)), false);
  let drag = null;
  $("pane-resize").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    drag = {y: e.clientY, h: document.querySelector(".editor-pane").getBoundingClientRect().height};
    $("pane-resize").setPointerCapture(e.pointerId);
    $("pane-resize").classList.add("dragging");
    document.body.classList.add("resizing");
  });
  $("pane-resize").addEventListener("pointermove", (e) => {
    if (drag) setPaneHeight(drag.h + e.clientY - drag.y, false);
  });
  const endDrag = (e) => {
    if (!drag) return;
    setPaneHeight(drag.h + e.clientY - drag.y);
    drag = null;
    $("pane-resize").classList.remove("dragging");
    document.body.classList.remove("resizing");
  };
  $("pane-resize").addEventListener("pointerup", endDrag);
  $("pane-resize").addEventListener("pointercancel", endDrag);
  $("pane-resize").addEventListener("dblclick", () => setPaneHeight(null));
  $("pane-resize").addEventListener("keydown", (e) => {
    const step = {ArrowUp: -40, ArrowDown: 40}[e.key];
    if (!step) return;
    e.preventDefault();
    setPaneHeight(document.querySelector(".editor-pane").getBoundingClientRect().height + step);
  });

  $("tex-tailor").addEventListener("click", () => {
    location.hash = "#chat";
    $("chat-in").value = "Tailor my resume to this posting:\n\n";
    $("chat-in").focus();
  });

  // Application kit. Everything personal - resume and API key - lives only in
  // this browser's storage and goes only to Anthropic's API; the page itself
  // is public and ships none of it. The SDK is fetched on first use, so
  // viewers who never make a kit never download it.
  const KIT_MODEL = "claude-opus-5";
  const SDK_URL = "https://cdn.jsdelivr.net/npm/@anthropic-ai/sdk@0.129.0/+esm";
  const API_KEY = "hunter.kit.apiKey";
  const WORKSPACE_KEY = "hunter.kit.workspace";
  // One line of a standard one-page resume holds roughly this many characters.
  const BULLET_MAX = 110;
  const KIT_SYSTEM = `You help a job seeker tailor an application to one posting. You get their resume and the job description.

Every resume bullet you write must fit on one line of a one-page resume: at most ${BULLET_MAX} characters, ideally 80-${BULLET_MAX}. Cut filler words, articles and soft adjectives before cutting the result or the metric, and never let a bullet run onto a second line.

Reply with exactly these four sections, as plain text with these headings and no preamble:

ATS MATCH
List the 8-15 hard requirements an applicant tracking system would screen this posting for: skills, tools, languages, frameworks, certifications, degree, years of experience. Mark each must-have (stated as required) or nice-to-have (preferred, bonus, "a plus"). Then check the candidate's ORIGINAL resume, not your rewritten bullets: a requirement is matched only if the resume states it (common synonyms count, e.g. Postgres = PostgreSQL, JS = JavaScript). Write exactly these four lines, each a comma-separated list, "None" when empty:
Must-have matched: ...
Must-have missing: ...
Nice-to-have matched: ...
Nice-to-have missing: ...

TAILORED RESUME BULLETS
4-6 bullets rewritten from the candidate's real experience so they speak to what this job asks for. Reorder and reword freely, lead with the most relevant work, start each with a strong past-tense verb. Use only facts in the resume - never invent employers, projects, numbers, tools or dates.

COVER NOTE
3-4 sentences the candidate can paste into an application or a referral request: specific to this company and role, grounded in the resume, plain and direct.

SKILLS GAP (REFERENCE ONLY - NOT YOUR EXPERIENCE)
3-5 bullets, one per skill or experience this posting wants that the resume lacks. Anchor each to one of the candidate's real roles or projects from the resume, and write the resume bullet that role could have carried had they used the missing skill there - so they can see what their resume could have looked like. Write it as a finished resume bullet in Google's XYZ form ("Accomplished [X] as measured by [Y], by doing [Z]"): a strong past-tense verb, a concrete result, a plausible metric, and how the missing skill produced it, kept consistent with the scale of that role. Format each exactly as (the length limit applies to the XYZ bullet itself):
- [Missing: <skill>] At <role or project from the resume>: <the XYZ bullet>
These show the candidate their gaps; the bullets and their numbers are illustrative, not the candidate's experience, and must not be presented as such. Write "None - your resume covers this posting" if nothing important is missing.`;
  // Copy takes only what belongs in an application - the bullets and cover
  // note - never the score or the reference-only skills gap.
  const GAP_HEADING = "SKILLS GAP";
  const BULLETS_HEADING = "TAILORED RESUME BULLETS";

  let client = null, clientConfig = "", kitJob = null, kitRunning = false, kitText = "";

  function openSetup() {
    $("setup-key").value = saved.get(API_KEY);
    $("setup-workspace").value = saved.get(WORKSPACE_KEY);
    $("settings-menu").hidden = true;
    if (!$("setup").open) $("setup").showModal();
  }
  $("open-setup").addEventListener("click", openSetup);
  $("setup-cancel").addEventListener("click", () => $("setup").close());
  $("setup-save").addEventListener("click", () => {
    saved.set(API_KEY, $("setup-key").value.trim());
    saved.set(WORKSPACE_KEY, $("setup-workspace").value.trim());
    $("setup").close();
    renderChatHead();
  });
  $("setup-clear").addEventListener("click", () => {
    saved.set(API_KEY, ""); saved.set(WORKSPACE_KEY, "");
    $("setup-key").value = ""; $("setup-workspace").value = "";
    renderChatHead();
  });

  function renderKitMarks() {
    const show = !!(kitJob && kitJob.i && mine && data.inbox);
    $("kit-marks").hidden = !show;
    if (!show) return;
    const m = marksOf(kitJob);
    for (const b of $("kit-marks").querySelectorAll("button")) {
      const on = b.dataset.f === "vote" ? m.vote === b.dataset.v : !!m.applied;
      b.setAttribute("aria-pressed", on);
      b.title = m.waiting[b.dataset.f] === "unsent" && on ? "Not sent yet - will retry" : "";
    }
  }
  $("kit-marks").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]");
    if (b && kitJob) markClick(kitJob, b.dataset.f, b.dataset.v);
  });
  function openKit(job) {
    kitJob = job;
    $("kit-h").textContent = kitJob.t;
    $("kit-meta").textContent = [kitJob.c, kitJob.l].filter(Boolean).join(" · ");
    $("kit-link").href = kitJob.u;
    $("kit-desc").value = "";
    setKitText("");
    flushKit();
    $("kit-status").textContent = "";
    $("kit-copy").hidden = true;
    renderKitMarks();
    $("kit").showModal();
    if (!resumeText()) $("kit-status").innerHTML = `First, write your resume in the <a href="#resume">Resume editor</a>.`;
    else if (!saved.get(API_KEY)) $("kit-status").textContent = "First, add your Claude API key under Settings.";
  }
  $("rows").addEventListener("click", (e) => {
    const btn = e.target.closest(".kit-btn");
    if (btn) openKit(visible[Number(btn.dataset.i)]);
  });
  $("kit-status").addEventListener("click", (e) => { if (e.target.closest("a")) $("kit").close(); });
  $("kit-close").addEventListener("click", () => $("kit").close());
  $("kit-copy").addEventListener("click", async () => {
    const start = Math.max(kitText.indexOf(BULLETS_HEADING), 0);
    const gap = kitText.indexOf(GAP_HEADING);
    try {
      await navigator.clipboard.writeText(kitText.slice(start, gap > start ? gap : undefined).trim());
      $("kit-status").textContent = "Copied the bullets and cover note.";
    }
    catch { $("kit-status").textContent = "Couldn't copy - select the text instead."; }
  });

  // Formatting. Claude answers in plain text under four fixed headings; the
  // page parses that into sections and builds them with DOM nodes and
  // textContent only, so nothing in a response is ever parsed as HTML.
  const SECTIONS = [
    { match: "ATS MATCH", title: "ATS match - your current resume", kind: "ats" },
    { match: BULLETS_HEADING, title: "Tailored resume bullets", kind: "list", copy: true },
    { match: "COVER NOTE", title: "Cover note", kind: "prose", copy: true },
    { match: "KEYWORDS TO CONSIDER", title: "Keywords to consider", kind: "chips" },
    { match: GAP_HEADING, title: "Skills gap", kind: "gap" },
  ];

  // The score is computed here, not by the model: coverage of the posting's
  // requirements, must-haves counting double. Deterministic given the lists,
  // and every point is traceable to a named requirement.
  const ATS_LINES = {
    "must-have matched": "mustHit", "must-have missing": "mustMiss",
    "nice-to-have matched": "niceHit", "nice-to-have missing": "niceMiss",
  };
  function parseAts(lines) {
    const ats = { mustHit: [], mustMiss: [], niceHit: [], niceMiss: [], seen: 0 };
    for (const line of lines) {
      const m = line.replace(BULLET, "").match(/^\s*([a-z- ]+?)\s*:\s*(.*)$/i);
      const field = m && ATS_LINES[m[1].toLowerCase()];
      if (!field) continue;
      ats.seen += 1;
      ats[field] = /^none\.?$/i.test(m[2].trim()) ? []
        : m[2].split(",").map((w) => w.trim()).filter(Boolean);
    }
    const total = 2 * (ats.mustHit.length + ats.mustMiss.length) + ats.niceHit.length + ats.niceMiss.length;
    ats.score = total ? Math.round(100 * (2 * ats.mustHit.length + ats.niceHit.length) / total) : null;
    return ats;
  }

  function renderAts(sec, lines, done) {
    const ats = parseAts(lines);
    if (ats.seen < 4 && !done) { sec.append(el("p", "muted", "Scoring…")); return; }
    if (ats.score == null) { sec.append(el("p", "muted", "Couldn't find requirements to score.")); return; }
    const [level, icon, verdict] = ats.score >= 75 ? ["strong", "✓", "Strong match"]
      : ats.score >= 50 ? ["fair", "⚠", "Fair match"] : ["weak", "✗", "Weak match"];
    const top = el("div", "ats-top");
    const label = el("div", `ats-verdict ${level}`);
    label.append(el("span", "icon", icon), document.createTextNode(verdict));
    top.append(el("div", "ats-score num", `${ats.score}%`), label);
    const meter = el("div", "ats-meter");
    meter.setAttribute("role", "meter");
    meter.setAttribute("aria-valuenow", String(ats.score));
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", "100");
    meter.setAttribute("aria-label", `ATS match ${ats.score}%`);
    const fill = el("div", `ats-fill ${level}`);
    fill.style.width = `${ats.score}%`;
    meter.append(fill);
    sec.append(top, meter);
    const group = (label, words, kind) => {
      if (!words.length) return;
      const row = el("div", "ats-row");
      row.append(el("span", "ats-label", label));
      const chips = el("div", "chips");
      words.forEach((w) => chips.append(el("span", `chip ${kind}`, `${kind === "hit" ? "✓" : "✗"} ${w}`)));
      row.append(chips);
      sec.append(row);
    };
    group("Must-have", [...ats.mustHit], "hit");
    group("", [...ats.mustMiss], "miss-chip");
    group("Nice-to-have", [...ats.niceHit], "hit");
    group("", [...ats.niceMiss], "miss-chip");
    const hits = ats.mustHit.length + ats.niceHit.length;
    const all = hits + ats.mustMiss.length + ats.niceMiss.length;
    sec.append(el("p", "note", `${hits} of ${all} requirements found in your resume; must-haves count double. An estimate - real ATS systems vary.`));
  }
  const BULLET = /^\s*(?:[-*•]|\d+[.)])\s+/;

  function parseKit(text) {
    const parts = [];
    let current = { spec: null, lines: [] };
    for (const line of text.split("\n")) {
      const heading = line.trim().replace(/^#+\s*/, "").toUpperCase();
      const spec = SECTIONS.find((s) => heading.startsWith(s.match));
      if (spec) {
        if (current.spec || current.lines.some((l) => l.trim())) parts.push(current);
        current = { spec, lines: [] };
      } else {
        current.lines.push(line);
      }
    }
    parts.push(current);
    return parts.filter((p) => p.spec || p.lines.some((l) => l.trim()));
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function renderSection({ spec, lines }) {
    const body = lines.join("\n").trim();
    const sec = el("section", `kit-sec${spec && spec.kind === "gap" ? " gap" : ""}`);
    if (spec) {
      const head = el("header");
      head.append(el("h3", null, spec.title));
      if (spec.kind === "gap") head.append(el("span", "ref", "Reference only - not your experience"));
      if (spec.copy && body) {
        const btn = el("button", "mini", "Copy");
        btn.addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(body); btn.textContent = "Copied"; }
          catch { btn.textContent = "Select to copy"; }
        });
        head.append(btn);
      }
      sec.append(head);
    }
    const kind = spec ? spec.kind : "prose";
    const items = lines.map((l) => l.trim()).filter(Boolean);
    if (kind === "ats") {
      renderAts(sec, lines, !kitRunning);
    } else if (kind === "chips") {
      const words = body.split(/[,\n]/).map((w) => w.replace(BULLET, "").trim()).filter(Boolean);
      if (!words.length || /^none\b/i.test(body)) {
        sec.append(el("p", "muted", body || "…"));
      } else {
        const chips = el("div", "chips");
        words.forEach((w) => chips.append(el("span", "chip", w)));
        sec.append(chips);
      }
    } else if ((kind === "list" || kind === "gap") && items.some((l) => BULLET.test(l))) {
      const ul = el("ul");
      for (const line of items) {
        const text = line.replace(BULLET, "");
        const li = el("li");
        const miss = kind === "gap" && text.match(/^\[\s*missing:\s*([^\]]+)\]\s*/i);
        let bullet = text;  // the part that would go on the resume
        if (miss) {
          const rest = text.slice(miss[0].length);
          const where = rest.match(/^(at [^:]{1,120}):\s*/i);  // "At <role/project>:" from the resume
          bullet = where ? rest.slice(where[0].length) : rest;
          li.append(el("span", "miss", miss[1].trim()));
          if (where) li.append(el("strong", "where", `${where[1]}: `));
          li.append(document.createTextNode(bullet));
        } else {
          li.textContent = text;
        }
        if (bullet.length > BULLET_MAX) {
          li.append(el("span", "long", `long · ${bullet.length} chars`));
        }
        ul.append(li);
      }
      sec.append(ul);
    } else {
      for (const para of body.split(/\n\s*\n/)) sec.append(el("p", body ? null : "muted", para.trim() || "…"));
    }
    return sec;
  }

  // Streaming redraws are batched to one per frame; flushKit() draws the
  // final text right away, since frames can stall (e.g. a background tab).
  let renderQueued = false;
  function flushKit() {
    renderQueued = false;
    $("kit-out").replaceChildren(...parseKit(kitText).map(renderSection));
  }
  function setKitText(text) {
    kitText = text;
    if (renderQueued) return;
    renderQueued = true;
    requestAnimationFrame(() => { if (renderQueued) flushKit(); });
  }

  // Keys created at the organization level (not inside a workspace) must name
  // the workspace to bill on every request.
  async function kitClient(key, workspace) {
    const config = `${key}\n${workspace}`;
    if (client && clientConfig === config) return client;
    const { default: Anthropic } = await import(SDK_URL);
    client = new Anthropic({
      apiKey: key,
      dangerouslyAllowBrowser: true,
      defaultHeaders: workspace ? { "anthropic-workspace-id": workspace } : {},
    });
    clientConfig = config;
    return client;
  }

  function kitError(err) {
    const status = err && err.status;
    if (status === 401) return "Anthropic rejected the API key - check it under Settings → Claude API key.";
    if (status === 400 && String(err.message).includes("anthropic-workspace-id")) {
      return "This key isn't tied to a workspace. Add your Workspace ID under Settings, or create the key inside a workspace in the Anthropic Console.";
    }
    if (status === 403) return "This API key isn't allowed to use this model.";
    if (status === 429) return "Rate limited by Anthropic - wait a minute and try again.";
    if (status >= 500) return "Anthropic's API had a problem - try again shortly.";
    return `Something went wrong: ${err && err.message ? err.message : err}`;
  }

  $("kit-go").addEventListener("click", async () => {
    if (kitRunning || !kitJob) return;
    const resume = resumeText(), key = saved.get(API_KEY);
    if (!key) { $("kit").close(); openSetup(); return; }
    if (!resume) { $("kit").close(); location.hash = "#resume"; return; }
    const description = $("kit-desc").value.trim();
    if (description.length < 200) {
      $("kit-status").textContent = "Paste the full job description first - the kit is only as specific as what you give it.";
      return;
    }
    kitRunning = true;
    $("kit-go").disabled = true;
    $("kit-copy").hidden = true;
    setKitText("");
    $("kit-status").textContent = "Reading the posting…";
    try {
      const anthropic = await kitClient(key, saved.get(WORKSPACE_KEY));
      const stream = anthropic.beta.messages.stream({
        model: KIT_MODEL,
        max_tokens: 16000,
        // If Opus 5 declines, Anthropic re-runs the request on its recommended backup.
        betas: ["server-side-fallback-2026-07-01"],
        fallbacks: "default",
        system: KIT_SYSTEM,
        messages: [{
          role: "user",
          content: `<resume>\n${resume}\n</resume>\n\n<job>\nTitle: ${kitJob.t}\nCompany: ${kitJob.c}\nLocation: ${kitJob.l}\n\n${description}\n</job>`,
        }],
      });
      for await (const event of stream) {
        if (event.type === "content_block_start" && event.content_block.type === "fallback") {
          setKitText("");  // the backup model starts over
          $("kit-status").textContent = "Switching to a backup model…";
        } else if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
          $("kit-status").textContent = "Writing…";
          setKitText(kitText + event.delta.text);
        }
      }
      const message = await stream.finalMessage();
      flushKit();
      if (message.stop_reason === "refusal") {
        setKitText("");
        $("kit-status").textContent = "Claude declined this request. Try trimming the description to just the role details.";
      } else if (message.stop_reason === "max_tokens") {
        $("kit-status").textContent = "The answer was cut off - try again.";
        $("kit-copy").hidden = !kitText;
      } else {
        $("kit-status").textContent = "Done - review before you use it.";
        $("kit-copy").hidden = false;
      }
    } catch (err) {
      $("kit-status").textContent = kitError(err);
    } finally {
      kitRunning = false;  // before the final draw, so it renders as finished
      flushKit();
      $("kit-go").disabled = false;
    }
  });

  // --- chat -----------------------------------------------------------------------
  // Claude, called from this page with your own API key (like the kit). The
  // job board and your resume go into the system prompt once, when a
  // conversation starts, and stay frozen for it: the history is append-only,
  // which keeps the prompt cache warm and every earlier thinking block valid.
  // Claude names postings as [[J12]]; the page turns those into job cards.
  // The conversation lives only in this tab.
  const MODELS = [["claude-opus-5-5", "Opus 5.5"], ["claude-sonnet-5-5", "Sonnet 5.5"], ["claude-haiku-5-5", "Haiku 5.5"]];
  const FALLBACK_MODELS = new Set(["claude-opus-5-5", "claude-sonnet-5-5"]);  // Haiku has no server-side fallback
  const MODEL_KEY = "hunter.chat.model";
  const CHAT_JOBS = 300;
  const modelName = (id) => (MODELS.find(([m]) => m === id) || [id, id])[1];
  let chatModel = MODELS.some(([m]) => m === saved.get(MODEL_KEY)) ? saved.get(MODEL_KEY) : MODELS[0][0];
  for (const sel of [$("ask-model"), $("chat-model")]) {
    sel.innerHTML = MODELS.map(([id, name]) => `<option value="${id}">Claude · ${name}</option>`).join("");
    sel.value = chatModel;
    sel.addEventListener("change", () => {
      chatModel = sel.value;
      saved.set(MODEL_KEY, chatModel);
      $("ask-model").value = $("chat-model").value = chatModel;
      renderChatHead();
    });
  }
  function renderChatHead() {
    const key = !!saved.get(API_KEY);
    $("chat-dot").className = `dot ${key ? "ok" : "off"}`;
    $("chat-who").textContent = key ? `Claude ${modelName(chatModel)} · your API key` : "Add your Claude API key";
  }
  $("chat-who").parentElement.addEventListener("click", () => !saved.get(API_KEY) && openSetup());
  renderChatHead();

  let convo = null, chatBusy = false;
  function chatSystem() {
    const jobs = ALL.slice(0, CHAT_JOBS);
    const lines = jobs.map((r, n) => {
      const m = mine ? marksOf(r) : {};
      return `[J${n + 1}] ${r.t} | ${r.c} | ${r.l || "no location"} | ${LEVELS[r.k] || r.k || "level unknown"} | first seen ${r.f ? new Date(r.f * 1000).toISOString().slice(0, 10) : r.d || "?"}`
        + ` | ${r.sent ? "sent to you" : "not sent"}${r.st ? ` | ${r.st}` : ""}${isNew(r) ? " | NEW" : ""}${m.stage ? ` | on your board: ${STAGE_NAMES[m.stage]}` : ""}`;
    });
    const resume = resumeText();
    const who = profileName ? firstName() : "the user";
    const text = `You are Hunter's assistant, helping ${who} with their job search. Hunter polls company job boards and sends ${who} new postings. Today is ${new Date().toDateString()}.

Write plain text: no Markdown (no **, #, tables or links); use "-" for lists. Be direct and specific.

When you point to a posting from the job board below, write its marker alone on its own line, like:
[[J12]]
The page turns each marker into a card with the job's title, a link and a "Make kit" button, so don't repeat the title or URL next to it. Only use markers from the list; never invent postings.

<resume>
${resume || "The user hasn't written a resume in the Resume editor yet."}
</resume>

<job_board count="${jobs.length}" note="newest first; NEW = arrived since their last visit">
${lines.join("\n")}
</job_board>`;
    return {text, jobs};
  }
  function chatEl(tag, cls, text) { return el(tag, cls, text); }
  function renderReply(body, text, jobs) {
    body.replaceChildren();
    for (const [n, part] of text.split(/\[\[J(\d+)\]\]/).entries()) {
      if (n % 2) {
        const r = jobs[Number(part) - 1];
        if (r) body.append(pickCard(r));
      } else if (part.trim()) {
        body.append(chatEl("p", null, part.replace(/^\n+|\n+$/g, "")));
      }
    }
  }
  function pickCard(r) {
    const card = chatEl("div", "pick");
    card.append(chatEl("span", "pick-role", r.t), chatEl("span", "pick-co", [r.c, r.l].filter(Boolean).join(" · ")));
    const acts = chatEl("span", "pick-acts");
    const kit = chatEl("button", "kit-btn", "Make kit");
    kit.addEventListener("click", () => openKit(r));
    const open = chatEl("a", "row-act open", "Open ↗");
    Object.assign(open, {href: r.u, target: "_blank", rel: "noopener"});
    open.addEventListener("click", () => markOpened(r));
    acts.append(kit, open);
    card.append(acts);
    return card;
  }
  function addUserBubble(text, files) {
    const b = chatEl("div", "msg-user", text);
    if (files.length) b.append(chatEl("div", "note", files.map((f) => `📎 ${f.name}`).join("  ")));
    $("thread").append(b);
  }
  function addBotBubble() {
    const wrap = chatEl("div", "msg-bot");
    wrap.append(chatEl("span", "logo", "H_"));
    const body = chatEl("div", "msg-body");
    body.append(chatEl("p", "typing", "Thinking…"));
    wrap.append(body);
    $("thread").append(wrap);
    return body;
  }
  // After a mid-answer fallback, model-internal blocks from before the
  // switch are left out of the history; text and everything after stay.
  function echoContent(content) {
    const cut = content.map((b) => b.type).lastIndexOf("fallback");
    if (cut < 0) return content;
    return content.filter((b, n) => n > cut || b.type === "text" || b.type === "fallback");
  }
  async function sendChat(text, files = []) {
    text = expandShortcut(text.trim());
    if (chatBusy || (!text && !files.length)) return;
    const key = saved.get(API_KEY);
    if (!key) { $("chat-in").value = text; openSetup(); return; }
    if (!convo) {
      const sys = chatSystem();
      convo = {system: sys.text, jobs: sys.jobs, messages: []};
    }
    $("chat-empty").hidden = true;
    addUserBubble(text, files);
    const content = [...files.map((f) => ({type: "text", text: `<attachment name="${f.name}">\n${f.text}\n</attachment>`})),
                     ...(text ? [{type: "text", text}] : [])];
    convo.messages.push({role: "user", content});
    const body = addBotBubble();
    body.scrollIntoView({block: "end", behavior: "smooth"});
    chatBusy = true;
    $("chat-send").disabled = true;
    let reply = "", queued = false;
    const draw = () => { queued = false; renderReply(body, reply, convo.jobs); };
    try {
      const anthropic = await kitClient(key, saved.get(WORKSPACE_KEY));
      const fallback = FALLBACK_MODELS.has(chatModel);
      const stream = anthropic.beta.messages.stream({
        model: chatModel,
        max_tokens: 64000,
        system: [{type: "text", text: convo.system, cache_control: {type: "ephemeral"}}],
        messages: convo.messages,
        // If the model declines, Anthropic re-runs the request on its recommended backup.
        ...(fallback ? {betas: ["server-side-fallback-2026-07-01"], fallbacks: "default"} : {}),
      });
      for await (const event of stream) {
        if (event.type === "content_block_start" && event.content_block.type === "fallback") {
          toast("Switching to a backup model…");
        } else if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
          reply += event.delta.text;
          if (!queued) { queued = true; requestAnimationFrame(draw); }
        }
      }
      const message = await stream.finalMessage();
      if (message.stop_reason === "refusal") {
        convo.messages.pop();  // nothing answered it; leave the history as it was
        body.replaceChildren(chatEl("p", "err", "Claude declined this one. Try rephrasing."));
      } else {
        convo.messages.push({role: "assistant", content: echoContent(message.content)});
        reply = message.content.filter((b) => b.type === "text").map((b) => b.text).join("");
        draw();
        if (message.stop_reason === "max_tokens") body.append(chatEl("p", "note", "The answer was cut off."));
      }
    } catch (err) {
      convo.messages.pop();
      body.replaceChildren(chatEl("p", "err", kitError(err)));
      if (err && err.status === 401) openSetup();
    } finally {
      chatBusy = false;
      $("chat-send").disabled = false;
    }
  }
  const SHORTCUTS = {
    "/apply": "Help me apply to this job - what should I highlight, and what should I double-check first?",
    "/tailor": "Tailor my resume to this posting:",
    "/prep": "Prep me for an interview for this role - likely questions and what to brush up on:",
  };
  function expandShortcut(text) {
    const m = text.match(/^(\/\w+)\b\s*/);
    return m && SHORTCUTS[m[1]] ? `${SHORTCUTS[m[1]]} ${text.slice(m[0].length)}`.trim() : text;
  }
  const chatFiles = [], askFiles = [];
  let fileTarget = null;
  function renderFiles(list, box) {
    $(box).innerHTML = list.map((f, n) => `<span class="file-chip">${esc(f.name)}<button type="button" data-n="${n}" aria-label="Remove ${esc(f.name)}">×</button></span>`).join("");
  }
  for (const [list, box, btn] of [[chatFiles, "chat-files", "chat-attach"], [askFiles, "ask-files", "ask-attach"]]) {
    $(btn).addEventListener("click", () => { fileTarget = [list, box]; $("file-pick").click(); });
    $(box).addEventListener("click", (e) => {
      const b = e.target.closest("button[data-n]");
      if (b) { list.splice(Number(b.dataset.n), 1); renderFiles(list, box); }
    });
  }
  $("file-pick").addEventListener("change", async () => {
    const file = $("file-pick").files[0];
    $("file-pick").value = "";
    if (!file || !fileTarget) return;
    if (file.size > 300_000) { toast("That file is too big - attach plain text under 300 KB."); return; }
    const [list, box] = fileTarget;
    list.push({name: file.name, text: await file.text()});
    renderFiles(list, box);
  });
  function submitChat() {
    const text = $("chat-in").value, files = chatFiles.splice(0);
    $("chat-in").value = "";
    renderFiles(chatFiles, "chat-files");
    sendChat(text, files);
  }
  $("chat-form").addEventListener("submit", (e) => { e.preventDefault(); submitChat(); });
  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("ask").value, files = askFiles.splice(0);
    $("ask").value = "";
    renderFiles(askFiles, "ask-files");
    location.hash = "#chat";
    if (text.trim() || files.length) sendChat(text, files); else $("chat-in").focus();
  });
  for (const id of ["ask", "chat-in"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (id === "ask") $("composer").requestSubmit(); else submitChat();
      }
    });
  }
  $("ask-search").addEventListener("click", () => searchJobs($("ask").value));
  $("ask-command").addEventListener("click", () => {
    if (!$("ask").value.startsWith("/")) $("ask").value = "/" + $("ask").value;
    $("ask").focus();
    toast("Shortcuts: /apply, /tailor, /prep");
  });
  document.querySelector(".shortcuts").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-cmd]");
    if (!b) return;
    $("ask").value = `${b.dataset.cmd} ${$("ask").value.replace(/^\/\w+\s*/, "")}`;
    $("ask").focus();
  });
  $("chat-chips").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-prompt]");
    if (!b) return;
    if (b.dataset.prompt.endsWith(" ")) { $("chat-in").value = b.dataset.prompt; $("chat-in").focus(); }
    else sendChat(b.dataset.prompt);
  });
  $("chat-new").addEventListener("click", () => {
    if (chatBusy) return;
    convo = null;
    $("thread").replaceChildren($("chat-empty"));
    $("chat-empty").hidden = false;
    $("chat-in").focus();
  });

  // Land on the tab with something new in it, then draw everything.
  if (newCount()) tab = "new";
  for (const t of ["all", "new", "applied", "open"]) $(`tab-${t}`).setAttribute("aria-selected", t === tab);
  render();
  route();
})();
