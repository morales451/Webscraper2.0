"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function safeUrl(url) {
  return /^https?:\/\//i.test(url || "") ? url : "";
}
function shortUrl(url) {
  return (url || "").replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, "");
}
function lines(text) {
  return (text || "").split("\n").map((s) => s.trim()).filter(Boolean);
}
function numOrNull(v) {
  return v === "" || v == null ? null : Number(v);
}
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), 2600);
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") msg = data.detail;
      else if (Array.isArray(data.detail)) msg = data.detail.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
      else msg = JSON.stringify(data.detail || data);
    } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

function scoreBadge(score) {
  if (score === null || score === undefined) return `<span class="score" title="No keywords defined">–</span>`;
  const cls = score >= 60 ? "hi" : score >= 25 ? "mid" : "";
  return `<span class="score ${cls}">${score}</span>`;
}
function keywordChips(matches, max = 6) {
  const pos = (matches || []).filter((m) => !m.term.startsWith("NOT: "));
  const neg = (matches || []).filter((m) => m.term.startsWith("NOT: "));
  const chips = pos.slice(0, max).map((m) => `<span class="chip" title="${m.count}× on site">${esc(m.term)}</span>`);
  if (pos.length > max) chips.push(`<span class="chip">+${pos.length - max}</span>`);
  neg.forEach((m) => chips.push(`<span class="chip neg">${esc(m.term)}</span>`));
  return `<div class="chips">${chips.join("")}</div>`;
}

function emailCheckChip(check) {
  if (check === "invalid") return ` <span class="chip bad" title="This domain can't receive email">undeliverable</span>`;
  if (check === "ok_a") return ` <span class="chip warn" title="No MX record — mail may not arrive">unverified</span>`;
  if (check === "ok") return ` <span class="chip ok" title="Domain accepts email">✓</span>`;
  return "";
}
function signalChips(signals, max = 3) {
  const s = (signals || []).filter((x) => x !== "Google Tag Manager");
  if (!s.length) return "";
  return `<div class="chips" style="margin-top:4px">${s.slice(0, max).map((x) => `<span class="chip sig">${esc(x)}</span>`).join("")}${s.length > max ? `<span class="chip sig">+${s.length - max}</span>` : ""}</div>`;
}

// ---------------------------------------------------------------- state
const state = {
  settings: null,
  profiles: [],
  profileId: null, // profile currently loaded in the search form
  activeRun: null,
  pollTimer: null,
  leads: { sort: "score", desc: true, offset: 0, pageSize: 100, rows: [] },
  selected: new Set(),
};

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  if (name === "leads") loadLeadsTab();
  if (name === "runs") loadRuns();
  if (name === "profiles") renderProfiles();
  if (name === "settings") loadSettingsForm();
}
$$(".tab").forEach((b) => b.addEventListener("click", () => {
  if (location.hash !== `#${b.dataset.tab}`) history.pushState(null, "", `#${b.dataset.tab}`);
  showTab(b.dataset.tab);
}));
window.addEventListener("popstate", () => {
  const tab = location.hash.replace("#", "");
  if (tab && $(`#tab-${tab}`)) showTab(tab);
});
window.addEventListener("hashchange", () => {
  const tab = location.hash.replace("#", "");
  if (tab && $(`#tab-${tab}`) && !$(`#tab-${tab}`).classList.contains("active")) showTab(tab);
});

// ---------------------------------------------------------------- search form <-> profile
const form = $("#search-form");

function formToProfile() {
  const f = form.elements;
  return {
    name: f.name.value.trim() || "Quick search",
    description: f.description.value.trim(),
    queries: lines(f.queries.value),
    locations: lines(f.locations.value),
    keywords: lines(f.keywords.value),
    required_keywords: lines(f.required_keywords.value),
    exclude_keywords: lines(f.exclude_keywords.value),
    filters: {
      min_rating: numOrNull(f.min_rating.value),
      min_reviews: numOrNull(f.min_reviews.value),
      max_reviews: numOrNull(f.max_reviews.value),
      require_website: f.require_website.checked,
      require_phone: f.require_phone.checked,
      require_email: f.require_email.checked,
      exclude_closed: f.exclude_closed.checked,
      include_categories: lines(f.include_categories.value),
      exclude_categories: lines(f.exclude_categories.value),
      exclude_names: lines(f.exclude_names.value),
    },
    max_results_per_search: numOrNull(f.max_results_per_search.value) || 60,
    max_leads: numOrNull(f.max_leads.value),
    pages_per_site: numOrNull(f.pages_per_site.value) || 5,
    min_score: numOrNull(f.min_score.value) || 0,
    skip_seen: f.skip_seen.checked,
    split_areas: f.split_areas.checked,
    max_areas: numOrNull(f.max_areas.value) || 9,
    expand_queries: f.expand_queries.checked,
    check_emails: f.check_emails.checked,
    ai_notes: f.ai_notes.checked,
    schedule: f.schedule.value,
  };
}

function profileToForm(p) {
  const f = form.elements;
  const fl = p.filters || {};
  f.name.value = p.name || "";
  f.description.value = p.description || "";
  f.queries.value = (p.queries || []).join("\n");
  f.locations.value = (p.locations || []).join("\n");
  f.keywords.value = (p.keywords || []).map((k) => (k.weight === 1 ? k.term : `${k.term} | ${k.weight}`)).join("\n");
  f.required_keywords.value = (p.required_keywords || []).join("\n");
  f.exclude_keywords.value = (p.exclude_keywords || []).join("\n");
  f.min_rating.value = fl.min_rating ?? "";
  f.min_reviews.value = fl.min_reviews ?? "";
  f.max_reviews.value = fl.max_reviews ?? "";
  f.require_website.checked = !!fl.require_website;
  f.require_phone.checked = !!fl.require_phone;
  f.require_email.checked = !!fl.require_email;
  f.exclude_closed.checked = fl.exclude_closed !== false;
  f.include_categories.value = (fl.include_categories || []).join("\n");
  f.exclude_categories.value = (fl.exclude_categories || []).join("\n");
  f.exclude_names.value = (fl.exclude_names || []).join("\n");
  f.max_results_per_search.value = p.max_results_per_search ?? 60;
  f.max_leads.value = p.max_leads ?? "";
  f.pages_per_site.value = p.pages_per_site ?? 5;
  f.min_score.value = p.min_score ?? 0;
  f.skip_seen.checked = p.skip_seen !== false;
  f.split_areas.checked = !!p.split_areas;
  f.max_areas.value = p.max_areas ?? 9;
  f.expand_queries.checked = !!p.expand_queries;
  f.check_emails.checked = p.check_emails !== false;
  f.ai_notes.checked = !!p.ai_notes;
  f.schedule.value = p.schedule || "";
  updateSearchCount();
}

function clearForm() {
  form.reset();
  state.profileId = null;
  $("#profile-select").value = "";
  updateProfileButtons();
  updateSearchCount();
}

function updateSearchCount() {
  const q = lines(form.elements.queries.value).length;
  const l = lines(form.elements.locations.value).length || 1;
  const per = Number(form.elements.max_results_per_search.value) || 60;
  const split = form.elements.split_areas.checked && lines(form.elements.locations.value).length;
  const areas = split ? Number(form.elements.max_areas.value) || 9 : 1;
  const n = q * l * areas;
  $("#search-count").textContent = q
    ? `${split ? "up to " : ""}${n} Google Maps search${n === 1 ? "" : "es"} · up to ${n * per} listings${split ? " (big cities are split into areas)" : ""}`
    : "";
}
form.addEventListener("input", updateSearchCount);

function updateProfileButtons() {
  $("#save-profile-btn").textContent = state.profileId ? "Update profile" : "Save profile";
  $("#save-as-btn").hidden = !state.profileId;
  const p = state.profiles.find((x) => x.id === state.profileId);
  $("#profile-hint").textContent = p
    ? `Editing saved profile “${p.name}”. Changes apply to this search; click Update profile to keep them.`
    : "Pick a saved customer profile, or fill in a quick search below.";
}

async function loadProfiles() {
  state.profiles = await api("/api/profiles");
  const sel = $("#profile-select");
  sel.innerHTML = `<option value="">— Quick search / new profile —</option>` +
    state.profiles.filter((p) => !p.error).map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  sel.value = state.profileId || "";
}

$("#profile-select").addEventListener("change", (e) => {
  const id = e.target.value;
  if (!id) return clearForm();
  const p = state.profiles.find((x) => x.id === id);
  if (!p) return;
  state.profileId = id;
  profileToForm(p);
  updateProfileButtons();
});

function showFormError(msg) {
  const el = $("#form-error");
  el.textContent = msg || "";
  el.hidden = !msg;
}

$("#save-profile-btn").addEventListener("click", async () => {
  showFormError("");
  try {
    const body = formToProfile();
    const saved = state.profileId
      ? await api(`/api/profiles/${state.profileId}`, { method: "PUT", body })
      : await api("/api/profiles", { method: "POST", body });
    state.profileId = saved.id;
    await loadProfiles();
    updateProfileButtons();
    toast("Profile saved");
  } catch (err) {
    showFormError(err.message);
  }
});

$("#save-as-btn").addEventListener("click", async () => {
  const name = prompt("Name for the new profile:", `${form.elements.name.value} (copy)`);
  if (!name) return;
  form.elements.name.value = name;
  state.profileId = null;
  $("#save-profile-btn").click();
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  showFormError("");
  const f = form.elements;
  const btn = $("#start-btn");
  btn.disabled = true;
  try {
    const run = await api("/api/runs", {
      method: "POST",
      body: {
        profile_id: state.profileId,
        profile: formToProfile(),
        source: f.source.value,
        details: f.details.checked,
        enrich: f.enrich.checked,
      },
    });
    watchRun(run.id);
  } catch (err) {
    showFormError(err.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- live run panel
function watchRun(runId) {
  state.activeRun = runId;
  $("#run-empty").hidden = true;
  $("#run-live").hidden = false;
  clearTimeout(state.pollTimer);
  pollRun();
}

async function pollRun() {
  const id = state.activeRun;
  if (!id) return;
  let run, leads;
  try {
    [run, leads] = await Promise.all([
      api(`/api/runs/${id}`),
      api(`/api/leads?run_id=${id}&limit=300&sort=score`),
    ]);
  } catch (err) {
    state.pollTimer = setTimeout(pollRun, 4000);
    return;
  }
  renderRun(run, leads);
  if (run.active || run.status === "queued" || run.status === "running") {
    state.pollTimer = setTimeout(pollRun, 1500);
  }
}

function renderRun(run, leads) {
  $("#run-title").textContent = `#${run.id} · ${run.profile_name || "Search"}`;
  const st = $("#run-status");
  st.textContent = run.status;
  st.className = `badge ${run.status}`;
  $("#run-stop").hidden = !run.active;

  const running = run.active;
  const pct = run.searches_total ? Math.round((100 * run.searches_done) / run.searches_total) : 0;
  const prog = $("#run-bar").parentElement;
  prog.classList.toggle("indeterminate", running && pct === 0);
  $("#run-bar").style.width = running && pct === 0 ? "" : running ? `${pct}%` : "100%";

  $("#run-stats").innerHTML = [
    ["Searches", `${run.searches_done}/${run.searches_total}`],
    ["Found", run.found],
    ["Seen before", run.skipped_seen],
    ["Checked", run.processed],
    ["Qualified", run.qualified],
  ].map(([k, v]) => `<div class="stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");

  const err = $("#run-error");
  err.hidden = !run.error;
  err.textContent = run.error || "";

  const sug = $("#run-suggestions");
  const ideas = run.suggestions || [];
  sug.hidden = !ideas.length;
  if (ideas.length) {
    const have = lines(form.elements.keywords.value).map((k) => k.split("|")[0].trim().toLowerCase());
    sug.innerHTML = `<b>Keyword ideas</b><span class="muted small">Common on your best leads' websites, rare on the rest — click to add to the form.</span>
      <div class="chips" style="margin-top:6px">${ideas.map((s) => `<button type="button" class="chip ${have.includes(s.term) ? "added" : ""}" data-term="${esc(s.term)}"
        title="${s.best_leads_pct}% of best leads vs ${s.other_leads_pct}% of others">${esc(s.term)}</button>`).join("")}</div>`;
    $$("#run-suggestions button.chip").forEach((b) => b.addEventListener("click", () => {
      if (b.classList.contains("added")) return;
      const ta = form.elements.keywords;
      ta.value = (ta.value.trim() ? ta.value.trim() + "\n" : "") + b.dataset.term;
      b.classList.add("added");
      updateSearchCount();
      toast(state.profileId ? "Added — click Update profile to keep it" : "Added to keywords");
    }));
  }

  const log = $("#run-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 10;
  log.innerHTML = (run.log || []).map((l) => `<li><span class="t">${esc(l.t)}</span>${esc(l.msg)}</li>`).join("");
  if (atBottom) log.scrollTop = log.scrollHeight;

  $("#run-leads").innerHTML = leads.leads.length
    ? leads.leads.map((l) => `
      <tr data-id="${l.id}" class="${l.qualified ? "" : "unq"}" title="${esc((l.reasons || []).join("; "))}">
        <td>${scoreBadge(l.score)}</td>
        <td><b>${esc(l.name)}</b><div class="sub">${esc(l.category)}${l.rating ? ` · ★ ${l.rating}` : ""}</div></td>
        <td>${esc((l.emails || [])[0] || "")}</td>
        <td>${keywordChips(l.matches, 4)}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="muted">Waiting for the first results…</td></tr>`;
  $$("#run-leads tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openLead(Number(tr.dataset.id))));
}

$("#run-stop").addEventListener("click", async () => {
  try {
    await api(`/api/runs/${state.activeRun}/cancel`, { method: "POST" });
    toast("Stopping…");
  } catch (err) {
    toast(err.message);
  }
});
$("#run-open").addEventListener("click", () => {
  $("#lf-run").dataset.pending = state.activeRun;
  $("#lf-qualified").value = "";
  showTab("leads");
});

// ---------------------------------------------------------------- leads tab
async function loadLeadsTab() {
  const runs = await api("/api/runs?limit=200");
  const sel = $("#lf-run");
  const wanted = sel.dataset.pending || sel.value;
  delete sel.dataset.pending;
  sel.innerHTML = `<option value="">All runs</option>` +
    runs.map((r) => `<option value="${r.id}">#${r.id} · ${esc(r.profile_name || "Search")} · ${fmtDate(r.created_at)} (${r.qualified})</option>`).join("");
  sel.value = wanted && runs.some((r) => String(r.id) === String(wanted)) ? wanted : "";
  const statusSel = $("#lf-status");
  if (statusSel.options.length <= 1 && state.settings) {
    statusSel.innerHTML += state.settings.lead_statuses.map((s) => `<option value="${s}">${s}</option>`).join("");
    $("#bulk-status").innerHTML = state.settings.lead_statuses.map((s) => `<option>${s}</option>`).join("");
  }
  state.leads.offset = 0;
  await fetchLeads(false);
}

function leadQuery() {
  const p = new URLSearchParams();
  const add = (k, v) => { if (v !== "" && v != null) p.set(k, v); };
  add("run_id", $("#lf-run").value);
  add("q", $("#lf-q").value.trim());
  add("qualified", $("#lf-qualified").value);
  add("has_email", $("#lf-email").value);
  add("status", $("#lf-status").value);
  add("min_score", $("#lf-score").value);
  if ($("#lf-chains").checked) p.set("group_chains", "true");
  p.set("sort", state.leads.sort);
  p.set("desc", state.leads.desc);
  return p;
}

async function fetchLeads(append) {
  const p = leadQuery();
  p.set("limit", state.leads.pageSize);
  p.set("offset", state.leads.offset);
  const data = await api(`/api/leads?${p}`);
  state.leads.rows = append ? state.leads.rows.concat(data.leads) : data.leads;
  renderLeads(data.total);
}

function renderLeads(total) {
  const rows = state.leads.rows;
  const statuses = state.settings ? state.settings.lead_statuses : ["new"];
  $("#leads-body").innerHTML = rows.length
    ? rows.map((l) => {
      const web = safeUrl(l.website);
      const emails = l.emails || [];
      const check = (l.email_checks || {})[emails[0]];
      const formOnly = !emails.length && (l.signals || []).includes("Contact form");
      return `<tr data-id="${l.id}" class="${l.qualified ? "" : "unq"}" title="${esc((l.reasons || []).join("; "))}">
        <td class="sel stop"><input type="checkbox" class="pick" data-id="${l.id}" ${state.selected.has(l.id) ? "checked" : ""}></td>
        <td>${scoreBadge(l.score)}${l.ai_score != null ? `<div class="sub" title="AI fit rating">AI ${l.ai_score}</div>` : ""}</td>
        <td><b>${esc(l.name)}</b>${l.chain_size > 1 ? ` <span class="chip sig" title="Locations sharing this website">${l.chain_size} locations</span>` : ""}
          <div class="sub">${esc(l.category)}</div><div class="sub">${esc(l.address)}</div>
          ${l.ai_note ? `<div class="ai-note">${esc(l.ai_note)}</div>` : ""}</td>
        <td>${emails.length ? `<a href="mailto:${esc(emails[0])}" class="stop">${esc(emails[0])}</a>${emailCheckChip(check)}${emails.length > 1 ? `<div class="sub">+${emails.length - 1} more</div>` : ""}` : formOnly ? `<span class="muted">contact form only</span>` : `<span class="muted">—</span>`}</td>
        <td class="nowrap">${esc(l.phone) || `<span class="muted">—</span>`}</td>
        <td>${web ? `<a href="${esc(web)}" target="_blank" rel="noopener noreferrer" class="stop">${esc(shortUrl(web))}</a>` : `<span class="muted">—</span>`}${l.site_error ? `<div class="sub">⚠ ${esc(l.site_error)}</div>` : ""}</td>
        <td class="nowrap">${l.rating ? `★ ${l.rating}` : "—"}${l.reviews != null ? `<div class="sub">${l.reviews} reviews</div>` : ""}</td>
        <td>${keywordChips(l.matches)}${signalChips(l.signals)}</td>
        <td><select class="status stop" data-id="${l.id}">${statuses.map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
      </tr>`;
    }).join("")
    : `<tr><td colspan="9" class="muted">No leads match these filters.</td></tr>`;

  $("#lf-summary").textContent = `${total} lead${total === 1 ? "" : "s"}${rows.length < total ? ` · showing ${rows.length}` : ""}`;
  $("#lf-more").hidden = rows.length >= total;

  $$("#leads-body tr[data-id]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest(".stop")) return;
    openLead(Number(tr.dataset.id));
  }));
  $$("#leads-body input.pick").forEach((c) => c.addEventListener("change", () => {
    const id = Number(c.dataset.id);
    if (c.checked) state.selected.add(id); else state.selected.delete(id);
    updateBulkBar();
  }));
  $("#sel-all").checked = rows.length > 0 && rows.every((r) => state.selected.has(r.id));
  updateBulkBar();
  $$("#leads-body select.status").forEach((s) => s.addEventListener("change", async () => {
    await api(`/api/leads/${s.dataset.id}`, { method: "PATCH", body: { status: s.value } });
    toast("Status updated");
  }));
  $$("th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.leads.sort);
    th.classList.toggle("asc", th.dataset.sort === state.leads.sort && !state.leads.desc);
  });
}

let leadSearchTimer;
["#lf-run", "#lf-qualified", "#lf-email", "#lf-status", "#lf-chains"].forEach((s) =>
  $(s).addEventListener("change", () => { state.leads.offset = 0; fetchLeads(false); }));
["#lf-q", "#lf-score"].forEach((s) => $(s).addEventListener("input", () => {
  clearTimeout(leadSearchTimer);
  leadSearchTimer = setTimeout(() => { state.leads.offset = 0; fetchLeads(false); }, 300);
}));
$$("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  state.leads.desc = state.leads.sort === key ? !state.leads.desc : key !== "name";
  state.leads.sort = key;
  state.leads.offset = 0;
  fetchLeads(false);
}));
$("#lf-more").addEventListener("click", () => {
  state.leads.offset += state.leads.pageSize;
  fetchLeads(true);
});
function exportLeads(format, ids) {
  const p = ids ? new URLSearchParams({ ids: ids.join(",") }) : leadQuery();
  p.set("format", format);
  p.set("preset", $("#export-preset").value);
  window.location = `/api/leads/export?${p}`;
}
function updateBulkBar() {
  const n = state.selected.size;
  $("#bulkbar").hidden = n === 0;
  $("#bulk-count").textContent = `${n} selected`;
}
$("#sel-all").addEventListener("change", (e) => {
  state.leads.rows.forEach((r) => (e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id)));
  $$("#leads-body input.pick").forEach((c) => (c.checked = e.target.checked));
  updateBulkBar();
});
$("#bulk-apply").addEventListener("click", async () => {
  const status = $("#bulk-status").value;
  const res = await api("/api/leads/bulk", { method: "POST", body: { ids: [...state.selected], status } });
  state.leads.rows.forEach((r) => { if (state.selected.has(r.id)) r.status = status; });
  toast(`${res.updated} lead${res.updated === 1 ? "" : "s"} marked ${status}`);
  renderLeads(Number(($("#lf-summary").textContent.match(/\d+/) || [0])[0]));
});
$("#bulk-export").addEventListener("click", () => exportLeads("xlsx", [...state.selected]));
$("#bulk-clear").addEventListener("click", () => {
  state.selected.clear();
  $$("#leads-body input.pick").forEach((c) => (c.checked = false));
  $("#sel-all").checked = false;
  updateBulkBar();
});
$("#export-csv").addEventListener("click", () => exportLeads("csv"));
$("#export-xlsx").addEventListener("click", () => exportLeads("xlsx"));

// ---------------------------------------------------------------- lead drawer
function termRegex(term) {
  const body = term.replace(/^NOT: /, "").trim().split(/\s+\/\s+/).map((alt) => alt.split(/\s+/)
    .map((p) => p.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[\\w-]*")).join("\\s+")).join("|");
  try { return new RegExp(`(^|[^\\w])(${body})(?![\\w])`, "gi"); } catch (_) { return null; }
}
function highlight(text, term) {
  const safe = esc(text);
  const rx = termRegex(term);
  return rx ? safe.replace(rx, "$1<mark>$2</mark>") : safe;
}

async function openLead(id) {
  const l = await api(`/api/leads/${id}`).catch(() => null) || state.leads.rows.find((r) => r.id === id);
  if (!l) return;
  const web = safeUrl(l.website);
  const maps = safeUrl(l.maps_url);
  const statuses = state.settings ? state.settings.lead_statuses : ["new"];
  $("#drawer-body").innerHTML = `
    <div class="row">${scoreBadge(l.score)} <span class="badge ${l.qualified ? "done" : "failed"}">${l.qualified ? "qualified" : "not qualified"}</span></div>
    <h2 style="margin-top:8px">${esc(l.name)}</h2>
    <div class="muted">${esc((l.categories && l.categories.length ? l.categories : [l.category]).join(" · "))}</div>
    ${l.ai_note ? `<section><h4>AI fit note${l.ai_score != null ? ` · ${l.ai_score}/100` : ""}</h4><p class="ai-note" style="font-size:14px">${esc(l.ai_note)}</p></section>` : ""}
    ${(l.reasons || []).length ? `<section><h4>Why not qualified</h4><ul class="reasons">${l.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></section>` : ""}
    <section><h4>Contact</h4><dl class="kv">
      <dt>Emails</dt><dd>${(l.emails || []).length ? l.emails.map((e) => `<a href="mailto:${esc(e)}">${esc(e)}</a>${emailCheckChip((l.email_checks || {})[e])}`).join("<br>") : `<span class='muted'>none found${(l.signals || []).includes("Contact form") ? " — the site has a contact form" : ""}</span>`}</dd>
      <dt>Phone</dt><dd>${l.phone ? `<a href="tel:${esc(l.phone.replace(/[^\d+]/g, ""))}">${esc(l.phone)}</a>` : "—"}${(l.site_phones || []).filter((p) => p.replace(/\D/g, "").slice(-10) !== (l.phone || "").replace(/\D/g, "").slice(-10)).map((p) => `<div class="sub">also on website: <a href="tel:${esc(p)}">${esc(p)}</a></div>`).join("")}</dd>
      ${Object.keys(l.socials || {}).length ? `<dt>Social</dt><dd class="socials">${Object.entries(l.socials).filter(([, u]) => safeUrl(u)).map(([n, u]) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(n === "x" ? "X / Twitter" : n)}</a>`).join("")}</dd>` : ""}
      <dt>Website</dt><dd>${web ? `<a href="${esc(web)}" target="_blank" rel="noopener noreferrer">${esc(shortUrl(web))}</a>` : "—"}${l.site_error ? `<div class="sub muted">⚠ ${esc(l.site_error)}</div>` : ""}</dd>
      <dt>Address</dt><dd>${esc(l.address) || "—"}</dd>
      <dt>Google Maps</dt><dd>${maps ? `<a href="${esc(maps)}" target="_blank" rel="noopener noreferrer">Open listing</a>` : "—"}${l.rating ? ` · ★ ${l.rating}` : ""}${l.reviews != null ? ` (${l.reviews} reviews)` : ""}</dd>
      <dt>Found by</dt><dd>${esc(l.query)}</dd>
      ${l.site_title ? `<dt>Site title</dt><dd>${esc(l.site_title)}</dd>` : ""}
      ${l.site_description ? `<dt>Description</dt><dd>${esc(l.site_description)}</dd>` : ""}
      <dt>Pages read</dt><dd>${l.pages_crawled}</dd>
      ${l.chain_size > 1 ? `<dt>Locations</dt><dd>${l.chain_size} listings share this website</dd>` : ""}
    </dl></section>
    ${(l.signals || []).length ? `<section><h4>Website signals</h4><div class="chips">${l.signals.map((s) => `<span class="chip sig">${esc(s)}</span>`).join("")}</div></section>` : ""}
    <section><h4>Keyword evidence</h4>
      ${(l.matches || []).length ? `<ul class="evidence">${l.matches.map((m) => `
        <li><span class="chip ${m.term.startsWith("NOT: ") ? "neg" : ""}">${esc(m.term)}</span>
          <span class="muted small">found ${m.count}×${m.pages ? ` on ${m.pages} page${m.pages === 1 ? "" : "s"}` : ""}${m.zone ? ` · ${esc(m.zone)}` : ""}${m.credit && !m.term.startsWith("NOT: ") ? ` · ${Math.round(m.credit * 100)}% credit` : ""}${m.weight && m.weight !== 1 ? ` · weight ${m.weight}` : ""}</span>
          ${(m.snippets || []).map((s) => `<blockquote>${highlight(s.text, m.term)}<small>${esc(s.where)}${s.zone ? ` · ${esc(s.zone)}` : ""}</small></blockquote>`).join("")}
        </li>`).join("")}</ul>` : `<p class="muted">No keywords matched.</p>`}
    </section>
    <section><h4>Follow-up</h4>
      <div class="form">
        <label>Status <select id="d-status">${statuses.map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}</select></label>
        <label>Notes <textarea id="d-notes" rows="4">${esc(l.notes)}</textarea></label>
        <div><button class="primary" id="d-save">Save</button></div>
      </div>
    </section>`;
  $("#drawer").hidden = false;
  $("#d-save").addEventListener("click", async () => {
    const updated = await api(`/api/leads/${id}`, { method: "PATCH", body: { status: $("#d-status").value, notes: $("#d-notes").value } });
    const row = state.leads.rows.find((r) => r.id === id);
    if (row) Object.assign(row, updated);
    toast("Saved");
    if ($("#tab-leads").classList.contains("active")) renderLeads(Number(($("#lf-summary").textContent.match(/\d+/) || [0])[0]));
  });
}
$("#drawer-close").addEventListener("click", () => ($("#drawer").hidden = true));
$("#drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") $("#drawer").hidden = true; });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });

// ---------------------------------------------------------------- runs tab
async function loadRuns() {
  const runs = await api("/api/runs?limit=200");
  $("#runs-body").innerHTML = runs.length ? runs.map((r) => `
    <tr data-id="${r.id}">
      <td>${r.id}</td>
      <td class="nowrap">${fmtDate(r.created_at)}</td>
      <td>${esc(r.profile_name)}${r.trigger === "schedule" ? ` <span class="chip sig">scheduled</span>` : ""}${r.error ? `<div class="sub" style="color:var(--bad)">${esc(r.error)}</div>` : ""}</td>
      <td>${esc(r.source)}</td>
      <td><span class="badge ${r.status}">${esc(r.status)}</span></td>
      <td>${r.found}${r.skipped_seen ? `<div class="sub">${r.skipped_seen} seen before</div>` : ""}</td>
      <td>${r.processed}</td>
      <td><b>${r.qualified}</b></td>
      <td class="nowrap">
        <button class="small-btn" data-act="view">Leads</button>
        <button class="small-btn" data-act="load">Search again</button>
        ${r.active ? `<button class="small-btn danger" data-act="stop">Stop</button>` : `<button class="small-btn ghost" data-act="delete" title="Delete run and its leads">Delete</button>`}
      </td>
    </tr>`).join("") : `<tr><td colspan="9" class="muted">No runs yet.</td></tr>`;

  $$("#runs-body button").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const id = Number(b.closest("tr").dataset.id);
    const run = runs.find((r) => r.id === id);
    if (b.dataset.act === "view") {
      $("#lf-run").dataset.pending = id;
      showTab("leads");
    } else if (b.dataset.act === "load") {
      state.profileId = run.profile_id && state.profiles.some((p) => p.id === run.profile_id) ? run.profile_id : null;
      $("#profile-select").value = state.profileId || "";
      profileToForm(run.profile);
      updateProfileButtons();
      showTab("search");
    } else if (b.dataset.act === "stop") {
      await api(`/api/runs/${id}/cancel`, { method: "POST" });
      setTimeout(loadRuns, 1000);
    } else if (b.dataset.act === "delete") {
      if (!confirm(`Delete run #${id} and its ${run.processed} leads?`)) return;
      await api(`/api/runs/${id}`, { method: "DELETE" });
      loadRuns();
    }
  }));
  $$("#runs-body tr[data-id]").forEach((tr) => tr.addEventListener("click", () => {
    const id = Number(tr.dataset.id);
    const run = runs.find((r) => r.id === id);
    if (run && run.active) { watchRun(id); showTab("search"); }
  }));
}

// ---------------------------------------------------------------- profiles tab
function renderProfiles() {
  $("#profiles-dir").textContent = state.settings ? state.settings.profiles_dir : "profiles/";
  $("#profiles-list").innerHTML = state.profiles.length ? state.profiles.map((p) => p.error ? `
    <div class="card profile-card"><h3>${esc(p.id)}.yaml</h3><p class="error">${esc(p.error)}</p></div>` : `
    <div class="card profile-card" data-id="${esc(p.id)}">
      <h3>${esc(p.name)}</h3>
      ${p.description ? `<p class="muted">${esc(p.description)}</p>` : ""}
      <p class="small"><b>Searches:</b> ${esc(p.queries.join(", "))}</p>
      <p class="small"><b>Where:</b> ${esc(p.locations.join(", ") || "anywhere")}${p.split_areas ? ` <span class="chip sig">split into areas</span>` : ""}</p>
      ${p.schedule ? `<p class="small"><span class="chip ok">repeats ${esc(p.schedule)}</span> <span class="muted">next: ${p.next_run_at && new Date(p.next_run_at) > new Date() ? esc(fmtDate(p.next_run_at)) : "within a minute"} (while the dashboard is open)</span></p>` : ""}
      ${keywordChips(p.keywords.map((k) => ({ term: k.term, count: k.weight })), 8)}
      <div class="row">
        <button class="primary small-btn" data-act="use">Use</button>
        <button class="small-btn" data-act="dup">Duplicate</button>
        <button class="small-btn ghost" data-act="del">Delete</button>
      </div>
    </div>`).join("") : `<p class="muted">No profiles yet — create one from the Search tab.</p>`;

  $$("#profiles-list button").forEach((b) => b.addEventListener("click", async () => {
    const id = b.closest("[data-id]").dataset.id;
    const p = state.profiles.find((x) => x.id === id);
    if (b.dataset.act === "use") {
      state.profileId = id;
      $("#profile-select").value = id;
      profileToForm(p);
      updateProfileButtons();
      showTab("search");
    } else if (b.dataset.act === "dup") {
      const copy = { ...p, name: `${p.name} (copy)` };
      delete copy.id;
      await api("/api/profiles", { method: "POST", body: copy });
      await loadProfiles();
      renderProfiles();
    } else if (b.dataset.act === "del") {
      if (!confirm(`Delete profile “${p.name}”?`)) return;
      await api(`/api/profiles/${id}`, { method: "DELETE" });
      if (state.profileId === id) clearForm();
      await loadProfiles();
      renderProfiles();
    }
  }));
}
$("#new-profile").addEventListener("click", () => {
  clearForm();
  showTab("search");
  form.elements.name.focus();
});

// ---------------------------------------------------------------- settings tab
async function loadSettings() {
  state.settings = await api("/api/settings");
}
function loadSettingsForm() {
  const s = state.settings;
  if (!s) return;
  const f = $("#settings-form").elements;
  f.source.value = s.source;
  f.places_api_key.value = "";
  $("#key-state").textContent = s.has_places_api_key ? `(saved: ${s.places_api_key})` : "(not set)";
  f.headless.checked = s.headless;
  f.render_js.checked = s.render_js;
  f.respect_robots.checked = s.respect_robots;
  f.use_browser_profile.checked = s.use_browser_profile;
  f.site_concurrency.value = s.site_concurrency;
  f.request_timeout.value = s.request_timeout;
  f.search_delay.value = s.search_delay;
  f.maps_concurrency.value = s.maps_concurrency;
  f.anthropic_api_key.value = "";
  f.ai_model.value = s.ai_model;
  $("#ai-key-state").textContent = s.has_anthropic_api_key ? `(saved: ${s.anthropic_api_key})` : "(not set)";
  $("#ai-hint").textContent = s.has_anthropic_api_key ? "uses your Anthropic API key" : "add an Anthropic API key in Settings first";
  $("#data-dir").textContent = s.data_dir;
}
$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target.elements;
  const body = {
    source: f.source.value,
    headless: f.headless.checked,
    render_js: f.render_js.checked,
    respect_robots: f.respect_robots.checked,
    use_browser_profile: f.use_browser_profile.checked,
    site_concurrency: Number(f.site_concurrency.value) || 8,
    request_timeout: Number(f.request_timeout.value) || 15,
    search_delay: Number(f.search_delay.value) || 0,
    maps_concurrency: Number(f.maps_concurrency.value) || 4,
    ai_model: f.ai_model.value.trim() || "claude-opus-5-5",
  };
  if (f.places_api_key.value.trim()) body.places_api_key = f.places_api_key.value.trim();
  if (f.anthropic_api_key.value.trim()) body.anthropic_api_key = f.anthropic_api_key.value.trim();
  try {
    state.settings = await api("/api/settings", { method: "PUT", body });
    loadSettingsForm();
    toast("Settings saved");
  } catch (err) {
    toast(err.message);
  }
});

$("#health-btn").addEventListener("click", async () => {
  const btn = $("#health-btn");
  const list = $("#health-list");
  btn.disabled = true;
  list.innerHTML = `<li><span></span><span class="muted">Running checks — about 20 seconds…</span></li>`;
  const labels = { google_maps_browser: "Google Maps (browser)", places_api: "Google Places API",
    website_crawler: "Website crawler", email_check: "Email check", area_split: "Area splitting" };
  try {
    const res = await api("/api/health");
    list.innerHTML = Object.entries(res).map(([k, r]) => `<li>
      <span class="${r.ok ? "ok" : r.ok === null ? "skip" : "bad"}">${r.ok ? "✓" : r.ok === null ? "–" : "✗"}</span>
      <b>${esc(labels[k] || k)}</b><span>${esc(r.message)}</span></li>`).join("");
  } catch (err) {
    list.innerHTML = `<li><span class="bad">✗</span><span>${esc(err.message)}</span></li>`;
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- boot
(async function init() {
  await Promise.all([loadSettings(), loadProfiles()]);
  form.elements.source.value = state.settings.source;
  updateProfileButtons();
  updateSearchCount();
  const runs = await api("/api/runs?limit=5");
  const active = runs.find((r) => r.active);
  if (active) watchRun(active.id);
  else if (runs.length) watchRun(runs[0].id);
  const tab = location.hash.replace("#", "");
  if (tab && $(`#tab-${tab}`)) showTab(tab);
})();
