/* Gearsmith frontend: one small hash-routed app, no build step. */
"use strict";

const view = document.getElementById("view");
const topbar = document.getElementById("top");
const toastEl = document.getElementById("toast");
const modalEl = document.getElementById("modal");
const sheetEl = document.getElementById("sheet");

const state = {
  me: null,
  settings: null,
  appName: "Gearsmith",
  newToken: null, // freshly created API token, shown once until dismissed or you leave Settings
};

const TYPE_ICON = { guitar: "🎸", amp: "🔊", pedal: "🎛️", pick: "▲" };
const TYPE_ORDER = ["guitar", "amp", "pedal", "pick"];
const TYPE_LABEL = { guitar: "Guitars", amp: "Amps", pedal: "Pedals", pick: "Picks" };
const FEATURE_FOR_TYPE = { guitar: "feature_guitars", amp: "feature_amps", pedal: "feature_pedals", pick: "feature_picks" };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function fmtDate(value) {
  if (!value) return "";
  const d = new Date(value + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function fmtPrice(value) {
  if (value === null || value === undefined || value === "") return "";
  return "$" + Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && !path.startsWith("/api/login") && !path.startsWith("/api/setup")) {
    state.me = null;
    route();
    throw new Error("Signed out");
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    throw new Error((data && data.detail) || `Request failed (${res.status})`);
  }
  return data;
}

let toastTimer = null;
function toast(message) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
}

function openSheet(html) {
  sheetEl.innerHTML = html;
  modalEl.hidden = false;
}
function closeSheet() {
  modalEl.hidden = true;
  sheetEl.innerHTML = "";
}
modalEl.addEventListener("click", (e) => { if (e.target === modalEl) closeSheet(); });

function featureOn(name) {
  return !state.settings || state.settings[name] !== false;
}

function stringsChip(strings, big = false) {
  if (!strings || !featureOn("feature_maintenance")) return "";
  let text, cls;
  if (strings.state === "never") {
    text = "never restrung";
    cls = "";
  } else if (strings.state === "overdue") {
    text = `strings: ${strings.days}d · overdue`;
    cls = "overdue";
  } else if (strings.state === "aging") {
    text = `strings: ${strings.days}d`;
    cls = "aging";
  } else {
    text = `strings: ${strings.days}d`;
    cls = "fresh";
  }
  return `<span class="string-chip ${cls}${big ? " big" : ""}" title="Changed every ${strings.interval_days} days">${esc(text)}</span>`;
}

/* ---------------------------------------------------------------- auth */

function authView() {
  topbar.hidden = true;
  const setup = state.setupRequired;
  view.innerHTML = `
    <div class="auth">
      <div class="auth-logo"><img src="/static/icon.svg" alt="" width="40" height="40" /> ${esc(state.appName)}</div>
      <div class="card stack">
        <h1 style="margin-top:0">${setup ? "Set up " + esc(state.appName) : "Sign in"}</h1>
        ${setup ? `<p class="muted">Create the administrator account to get started.</p>` : ""}
        <form id="auth-form" class="stack">
          <div><label for="username">Username</label><input id="username" name="username" autocomplete="username" required /></div>
          <div><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required /></div>
          <p class="error" id="auth-error"></p>
          <button class="primary" type="submit">${setup ? "Create administrator" : "Sign in"}</button>
        </form>
      </div>
    </div>`;
  document.getElementById("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("auth-error");
    err.textContent = "";
    const body = {
      username: document.getElementById("username").value,
      password: document.getElementById("password").value,
    };
    try {
      await api(setup ? "/api/setup" : "/api/login", { method: "POST", body });
      await boot(true);
    } catch (ex) {
      err.textContent = ex.message;
    }
  });
}

/* ---------------------------------------------------------------- router */

function route() {
  if (!state.me) { authView(); return; }
  topbar.hidden = false;
  document.getElementById("user-badge").textContent = state.me.username + (state.me.is_admin ? " (admin)" : "");
  document.getElementById("tab-sets").hidden = !featureOn("feature_sets");
  document.getElementById("tab-due").hidden = !featureOn("feature_maintenance");
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("/").filter(Boolean);
  let tab = "gear";
  if (parts[0] !== "settings") state.newToken = null;
  if (parts[0] === "gear" && parts[1]) { gearDetailView(Number(parts[1])); }
  else if (parts[0] === "sets") { tab = "sets"; featureOn("feature_sets") ? setsView() : (location.hash = "#/"); }
  else if (parts[0] === "due") { tab = "due"; featureOn("feature_maintenance") ? dueView() : (location.hash = "#/"); }
  else if (parts[0] === "settings") { tab = "settings"; settingsView(); }
  else { gearListView(); }
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
}

window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------- gear list */

async function gearListView() {
  const gear = await api("/api/gear");
  const visibleTypes = TYPE_ORDER.filter((t) => featureOn(FEATURE_FOR_TYPE[t]));
  const sections = visibleTypes.map((t) => ({ type: t, items: gear.filter((g) => g.type === t) }));
  view.innerHTML = `
    <div class="pagehead">
      <h1>Gear</h1>
      <button class="primary" id="add-gear" type="button">Add gear</button>
    </div>
    <div class="toolbar"><input type="search" id="gear-search" placeholder="Filter by name, make, model..." /></div>
    <div id="gear-sections"></div>`;
  const container = document.getElementById("gear-sections");
  function render(filter = "") {
    const needle = filter.trim().toLowerCase();
    container.innerHTML = sections.map(({ type, items }) => {
      const shown = needle
        ? items.filter((g) => `${g.name} ${g.make} ${g.model}`.toLowerCase().includes(needle))
        : items;
      if (!shown.length) return "";
      return `
        <h2 class="section-title">${esc(TYPE_LABEL[type])} <span class="count">${shown.length}</span></h2>
        <div class="grid">
          ${shown.map((g) => `
            <a class="gear-card" href="#/gear/${g.id}">
              <span class="thumb">${g.cover ? `<img src="${g.cover}" alt="" />` : TYPE_ICON[g.type]}</span>
              <span class="gc-body">
                <span class="gc-name" title="${esc(g.name)}">${esc(g.name)}</span>
                <span class="gc-meta" title="${esc([g.make, g.model].filter(Boolean).join(" · "))}">${esc([g.make, g.model].filter(Boolean).join(" · ")) || "&nbsp;"}</span>
                <span class="gc-foot">
                  ${g.type === "guitar" ? stringsChip(g.strings) : ""}
                  ${g.status && g.status !== "home" ? `<span class="badge ${esc(g.status)}">${esc(g.status_label)}</span>` : ""}
                  ${g.sets.length ? `<span class="badge">${g.sets.length} set${g.sets.length > 1 ? "s" : ""}</span>` : ""}
                </span>
              </span>
            </a>`).join("")}
        </div>`;
    }).join("") || `<p class="empty">No gear yet. Add your first piece.</p>`;
  }
  render();
  document.getElementById("gear-search").addEventListener("input", (e) => render(e.target.value));
  document.getElementById("add-gear").addEventListener("click", () => gearForm());
}

/* ---------------------------------------------------------------- gear form */

const SPEC_FIELDS = {
  guitar: [
    ["finish", "Finish"], ["pickups", "Pickups"], ["nut", "Nut"], ["scale", "Scale length"],
    ["tuning", "Tuning"], ["string_gauge", "String gauge"], ["mods", "Mods"],
  ],
  amp: [["wattage", "Wattage"], ["speaker", "Speaker"], ["tubes", "Tubes"]],
  pedal: [["voltage", "Voltage"], ["ma_draw", "Current draw (mA)", "number"], ["polarity", "Polarity"], ["bypass", "Bypass"]],
  pick: [["thickness", "Thickness"], ["material", "Material"], ["quantity", "Quantity", "number"]],
};
const MAKE_LABEL = { guitar: "Make", amp: "Make", pedal: "Make", pick: "Brand" };

function gearForm(existing = null) {
  const g = existing || { type: "guitar", specs: {}, status: "home" };
  openSheet(`
    <h2>${existing ? "Edit" : "Add"} gear</h2>
    <form id="gear-form" class="stack">
      <div class="form-grid">
        <div><label for="gf-type">Type</label>
          <select id="gf-type" ${existing ? "disabled" : ""}>
            ${TYPE_ORDER.map((t) => `<option value="${t}" ${g.type === t ? "selected" : ""}>${TYPE_LABEL[t].slice(0, -1)}</option>`).join("")}
          </select>
        </div>
        <div><label for="gf-name">Name</label><input id="gf-name" required maxlength="80" value="${esc(g.name || "")}" placeholder="What you call it" /></div>
        <div><label for="gf-make" id="gf-make-label">${MAKE_LABEL[g.type]}</label><input id="gf-make" maxlength="80" value="${esc(g.make || "")}" /></div>
        <div><label for="gf-model">Model</label><input id="gf-model" maxlength="80" value="${esc(g.model || "")}" /></div>
        <div><label for="gf-year">Year</label><input id="gf-year" type="number" min="1900" max="2100" value="${g.year ?? ""}" /></div>
        <div><label for="gf-serial">Serial</label><input id="gf-serial" maxlength="80" value="${esc(g.serial || "")}" /></div>
      </div>
      <div class="form-grid" id="gf-specs"></div>
      <div class="form-grid">
        <div><label for="gf-status">Status</label>
          <select id="gf-status">
            <option value="">Unspecified</option>
            ${["home", "luthier", "lent"].map((s) => `<option value="${s}" ${g.status === s ? "selected" : ""}>${{ home: "Home", luthier: "At the luthier", lent: "Lent out" }[s]}</option>`).join("")}
          </select>
        </div>
        <div id="gf-interval-wrap" ${g.type !== "guitar" ? "hidden" : ""}>
          <label for="gf-interval">Restring every (days)</label>
          <input id="gf-interval" type="number" min="1" max="730" value="${g.restring_interval_days ?? 90}" />
        </div>
        <div><label for="gf-pdate">Purchase date</label><input id="gf-pdate" type="date" value="${esc(g.purchase_date || "")}" /></div>
        <div><label for="gf-pprice">Purchase price</label><input id="gf-pprice" type="number" min="0" step="0.01" value="${g.purchase_price ?? ""}" /></div>
        <div class="full"><label for="gf-notes">Notes</label><textarea id="gf-notes" maxlength="4000">${esc(g.notes || "")}</textarea></div>
      </div>
      <p class="error" id="gf-error"></p>
      <div class="sheet-actions">
        <button type="button" id="gf-cancel">Cancel</button>
        <button class="primary" type="submit">${existing ? "Save changes" : "Add gear"}</button>
      </div>
    </form>`);

  const typeSel = document.getElementById("gf-type");
  const specsWrap = document.getElementById("gf-specs");
  function renderSpecs() {
    const t = typeSel.value;
    document.getElementById("gf-make-label").textContent = MAKE_LABEL[t];
    document.getElementById("gf-interval-wrap").hidden = t !== "guitar";
    specsWrap.innerHTML = SPEC_FIELDS[t].map(([key, label, kind]) => `
      <div><label for="gf-spec-${key}">${label}</label>
      <input id="gf-spec-${key}" data-spec="${key}" ${kind === "number" ? 'type="number" step="any" min="0"' : ""}
        value="${esc(t === g.type ? (g.specs?.[key] ?? "") : "")}" /></div>`).join("");
  }
  renderSpecs();
  typeSel.addEventListener("change", renderSpecs);
  document.getElementById("gf-cancel").addEventListener("click", closeSheet);

  document.getElementById("gear-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("gf-error");
    err.textContent = "";
    const t = typeSel.value;
    const specs = {};
    specsWrap.querySelectorAll("[data-spec]").forEach((input) => {
      if (input.value !== "") specs[input.dataset.spec] = input.value;
    });
    const body = {
      type: t,
      name: document.getElementById("gf-name").value,
      make: document.getElementById("gf-make").value,
      model: document.getElementById("gf-model").value,
      year: document.getElementById("gf-year").value ? Number(document.getElementById("gf-year").value) : null,
      serial: document.getElementById("gf-serial").value,
      specs,
      status: document.getElementById("gf-status").value,
      purchase_date: document.getElementById("gf-pdate").value || null,
      purchase_price: document.getElementById("gf-pprice").value ? Number(document.getElementById("gf-pprice").value) : null,
      notes: document.getElementById("gf-notes").value,
      restring_interval_days: t === "guitar" ? Number(document.getElementById("gf-interval").value) : null,
    };
    try {
      const saved = existing
        ? await api(`/api/gear/${existing.id}`, { method: "PUT", body })
        : await api("/api/gear", { method: "POST", body });
      closeSheet();
      toast(existing ? "Saved" : "Added");
      location.hash = `#/gear/${saved.id}`;
      route();
    } catch (ex) {
      err.textContent = ex.message;
    }
  });
}

/* ---------------------------------------------------------------- gear detail */

async function gearDetailView(id) {
  const g = await api(`/api/gear/${id}`);
  const maintenance = featureOn("feature_maintenance");
  const facts = [
    [MAKE_LABEL[g.type], g.make], ["Model", g.model], ["Year", g.year], ["Serial", g.serial],
    ["Status", g.status_label !== "Unspecified" ? g.status_label : ""],
    ["Purchased", [fmtDate(g.purchase_date), fmtPrice(g.purchase_price)].filter(Boolean).join(" for ")],
    ["Added by", g.added_by],
  ].filter(([, v]) => v !== "" && v !== null && v !== undefined);
  const specs = (g.spec_fields || []).filter((f) => f.value !== null && f.value !== undefined && f.value !== "");
  view.innerHTML = `
    <div class="pagehead">
      <h1>${esc(g.name)}</h1>
      <div class="row">
        <button class="small" id="gd-edit" type="button">Edit</button>
        <button class="small danger" id="gd-delete" type="button">Delete</button>
      </div>
    </div>
    <p class="muted wrap-any">${esc(g.type_label.slice(0, -1))}${g.sets.length ? " · in " + g.sets.map((s) => esc(s.name)).join(", ") : ""}</p>
    <div class="hero">
      <div class="hero-photo">${g.cover ? `<img src="${g.cover}" alt="" />` : TYPE_ICON[g.type]}</div>
      <div>
        ${g.type === "guitar" && maintenance ? `<p>${stringsChip(g.strings, true)}</p>` : ""}
        <div class="facts">
          ${facts.map(([k, v]) => `<div class="fact"><span>${esc(k)}</span>${esc(v)}</div>`).join("")}
        </div>
        ${specs.length ? `<h2>Specs</h2><div class="facts">
          ${specs.map((f) => `<div class="fact"><span>${esc(f.label)}</span>${esc(f.value)}</div>`).join("")}
        </div>` : ""}
        ${g.notes ? `<h2>Notes</h2><p class="notes">${esc(g.notes)}</p>` : ""}
      </div>
    </div>
    <h2>Photos</h2>
    <div class="card">
      <div class="photo-strip" id="gd-photos">
        ${g.photos.map((p, i) => `
          <div class="photo-item">
            <img src="${p.url}" alt="" />
            <div class="row">
              ${i > 0 ? `<button class="small ghost" data-cover="${p.id}" type="button">Cover</button>` : `<span class="badge">Cover</span>`}
              <button class="small ghost danger" data-delphoto="${p.id}" type="button">Delete</button>
            </div>
          </div>`).join("")}
      </div>
      <form id="photo-form" class="row">
        <input type="file" id="photo-file" accept="image/*" required />
        <button class="small" type="submit">Upload</button>
      </form>
    </div>
    ${g.type === "guitar" && maintenance ? `
    <h2>Strings</h2>
    <div class="card" id="gd-strings">
      <div class="row" style="justify-content:space-between">
        <div>${stringsChip(g.strings, true)}</div>
        <button class="primary small" id="log-restring" type="button">Log a restring</button>
      </div>
      <p class="hint wrap-any">Changed every ${g.strings.interval_days} days${g.strings.last_date ? ` · last: ${esc(g.strings.last_brand || "unknown")} ${esc(g.strings.last_gauge || "")} on ${fmtDate(g.strings.last_date)}` : ""}.</p>
      <div id="restring-history" class="stack" style="margin-top:10px"></div>
    </div>` : ""}
    ${featureOn("feature_sets") ? `
    <h2>Sets</h2>
    <div class="card">
      <div class="set-chips" id="gd-sets">
        ${g.sets.map((s) => `<span class="set-chip" title="${esc(s.name)}">${esc(s.name)}</span>`).join("") || `<span class="muted">Not in any set.</span>`}
      </div>
    </div>` : ""}`;

  document.getElementById("gd-edit").addEventListener("click", () => gearForm(g));
  document.getElementById("gd-delete").addEventListener("click", () => {
    openSheet(`
      <h2>Delete ${esc(g.name)}?</h2>
      <p class="muted">This removes the ${esc(g.type_label.slice(0, -1).toLowerCase())}, its photos${g.type === "guitar" ? " and its restring history" : ""}. It stays in no sets.</p>
      <div class="sheet-actions">
        <button type="button" id="del-cancel">Cancel</button>
        <button class="primary danger" id="del-confirm" type="button">Delete</button>
      </div>`);
    document.getElementById("del-cancel").addEventListener("click", closeSheet);
    document.getElementById("del-confirm").addEventListener("click", async () => {
      await api(`/api/gear/${id}`, { method: "DELETE" });
      closeSheet();
      toast("Deleted");
      location.hash = "#/";
    });
  });

  document.getElementById("photo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = document.getElementById("photo-file").files[0];
    if (!file) return;
    const data = new FormData();
    data.append("photo", file);
    try {
      await api(`/api/gear/${id}/photos`, { method: "POST", body: data });
      toast("Photo added");
      gearDetailView(id);
    } catch (ex) { toast(ex.message); }
  });
  view.querySelectorAll("[data-cover]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/photos/${b.dataset.cover}/cover`, { method: "POST" });
    gearDetailView(id);
  }));
  view.querySelectorAll("[data-delphoto]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/photos/${b.dataset.delphoto}`, { method: "DELETE" });
    gearDetailView(id);
  }));

  if (g.type === "guitar" && maintenance) {
    loadRestringHistory(g);
    document.getElementById("log-restring").addEventListener("click", () => restringForm(g));
  }
}

async function loadRestringHistory(g) {
  const rows = await api(`/api/gear/${g.id}/restrings`);
  const el = document.getElementById("restring-history");
  if (!el) return;
  el.innerHTML = rows.length ? rows.map((r) => `
    <div class="restring-row">
      <span class="rs-date">${fmtDate(r.date)}</span>
      <span>${esc([r.brand, r.gauge].filter(Boolean).join(" ")) || "Restring"}${r.note ? ` <span class="muted">- ${esc(r.note)}</span>` : ""}
        <span class="muted"> · ${esc(r.logged_by)}</span></span>
      <button class="small ghost danger" data-delrs="${r.id}" type="button">Delete</button>
    </div>`).join("") : `<p class="muted">No restrings logged yet.</p>`;
  el.querySelectorAll("[data-delrs]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/restrings/${b.dataset.delrs}`, { method: "DELETE" });
    toast("Entry deleted");
    gearDetailView(g.id);
  }));
}

function restringForm(g) {
  const todayStr = new Date().toLocaleDateString("en-CA");
  openSheet(`
    <h2>Log a restring - ${esc(g.name)}</h2>
    <form id="rs-form" class="stack">
      <div class="form-grid">
        <div><label for="rs-brand">String brand</label><input id="rs-brand" maxlength="80" placeholder="e.g. Example Co" /></div>
        <div><label for="rs-gauge">Gauge</label><input id="rs-gauge" maxlength="40" value="${esc(g.specs?.string_gauge || "")}" placeholder="e.g. 10-46" /></div>
        <div><label for="rs-date">Date</label><input id="rs-date" type="date" value="${todayStr}" max="${todayStr}" /></div>
        <div><label for="rs-note">Note</label><input id="rs-note" maxlength="1000" placeholder="Optional" /></div>
      </div>
      <p class="error" id="rs-error"></p>
      <div class="sheet-actions">
        <button type="button" id="rs-cancel">Cancel</button>
        <button class="primary" type="submit">Log restring</button>
      </div>
    </form>`);
  document.getElementById("rs-cancel").addEventListener("click", closeSheet);
  document.getElementById("rs-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api(`/api/gear/${g.id}/restrings`, {
        method: "POST",
        body: {
          brand: document.getElementById("rs-brand").value,
          gauge: document.getElementById("rs-gauge").value,
          date: document.getElementById("rs-date").value || null,
          note: document.getElementById("rs-note").value,
        },
      });
      closeSheet();
      toast("Restring logged");
      gearDetailView(g.id);
    } catch (ex) {
      document.getElementById("rs-error").textContent = ex.message;
    }
  });
}

/* ---------------------------------------------------------------- sets */

async function setsView() {
  const sets = await api("/api/sets");
  view.innerHTML = `
    <div class="pagehead">
      <h1>Sets</h1>
      <button class="primary" id="add-set" type="button">New set</button>
    </div>
    <p class="muted">Group gear into rigs: a pedalboard, a gig rig, a recording chain. Gear can live in several sets or none.</p>
    <div class="stack" id="sets-list">
      ${sets.length ? sets.map((s) => `
        <div class="set-card">
          <div class="row set-card-head">
            <strong>${esc(s.name)}</strong>
            <span class="row">
              <button class="small ghost" data-editset="${s.id}" type="button">Edit</button>
              <button class="small ghost danger" data-delset="${s.id}" type="button">Delete</button>
            </span>
          </div>
          ${s.notes ? `<p class="muted set-notes" style="margin:6px 0 0">${esc(s.notes)}</p>` : ""}
          <div class="set-members">
            ${s.items.map((i) => `
              <a class="set-member" href="#/gear/${i.id}" title="${esc(i.name)}">
                <span class="thumb">${i.cover ? `<img src="${i.cover}" alt="" />` : TYPE_ICON[i.type]}</span>
                <span class="set-member-name">${esc(i.name)}</span>
              </a>`).join("") || `<span class="muted">Empty set.</span>`}
          </div>
        </div>`).join("") : `<p class="empty">No sets yet. Make one for a board or rig.</p>`}
    </div>`;
  document.getElementById("add-set").addEventListener("click", () => setForm());
  view.querySelectorAll("[data-editset]").forEach((b) => b.addEventListener("click", async () => {
    const s = await api(`/api/sets/${b.dataset.editset}`);
    setForm(s);
  }));
  view.querySelectorAll("[data-delset]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/sets/${b.dataset.delset}`, { method: "DELETE" });
    toast("Set deleted");
    setsView();
  }));
}

async function setForm(existing = null) {
  const gear = await api("/api/gear");
  const chosen = new Set(existing ? existing.items.map((i) => i.id) : []);
  openSheet(`
    <h2>${existing ? "Edit" : "New"} set</h2>
    <form id="set-form" class="stack">
      <div><label for="sf-name">Name</label><input id="sf-name" required maxlength="80" value="${esc(existing?.name || "")}" placeholder="e.g. Big board, Gig rig" /></div>
      <div><label for="sf-notes">Notes</label><input id="sf-notes" maxlength="1000" value="${esc(existing?.notes || "")}" placeholder="Optional" /></div>
      <div>
        <label>Gear in this set</label>
        <div class="member-pick">
          ${gear.map((g) => `
            <label class="toggle row" style="justify-content:space-between">
              <span class="row">${TYPE_ICON[g.type]} ${esc(g.name)} <span class="muted">${esc([g.make, g.model].filter(Boolean).join(" · "))}</span></span>
              <input type="checkbox" data-member="${g.id}" ${chosen.has(g.id) ? "checked" : ""} />
            </label>`).join("")}
        </div>
      </div>
      <p class="error" id="sf-error"></p>
      <div class="sheet-actions">
        <button type="button" id="sf-cancel">Cancel</button>
        <button class="primary" type="submit">${existing ? "Save changes" : "Create set"}</button>
      </div>
    </form>`);
  document.getElementById("sf-cancel").addEventListener("click", closeSheet);
  document.getElementById("set-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const item_ids = [...sheetEl.querySelectorAll("[data-member]:checked")].map((i) => Number(i.dataset.member));
    const body = {
      name: document.getElementById("sf-name").value,
      notes: document.getElementById("sf-notes").value,
      item_ids,
    };
    try {
      if (existing) {
        await api(`/api/sets/${existing.id}`, { method: "PATCH", body });
      } else {
        await api("/api/sets", { method: "POST", body });
      }
      closeSheet();
      toast(existing ? "Saved" : "Set created");
      setsView();
    } catch (ex) {
      document.getElementById("sf-error").textContent = ex.message;
    }
  });
}

/* ---------------------------------------------------------------- strings due */

async function dueView() {
  const items = await api("/api/due?days=14");
  view.innerHTML = `
    <div class="pagehead"><h1>Strings</h1></div>
    <p class="muted">Guitars past their restring interval, or close to it. Log a restring to reset the counter.</p>
    <div class="due-list" id="due-list">
      ${items.length ? items.map((i) => `
        <div class="due">
          <a class="thumb" href="#/gear/${i.gear_id}">${i.cover ? `<img src="${i.cover}" alt="" />` : "🎸"}</a>
          <div class="due-main">
            <a class="due-name" href="#/gear/${i.gear_id}">${esc(i.name)}</a>
            <div class="due-meta">
              ${i.state === "overdue"
                ? `<span class="due-when overdue">overdue by ${-i.days_until_due} day${i.days_until_due !== -1 ? "s" : ""}</span>`
                : `<span class="due-when due">due in ${i.days_until_due} day${i.days_until_due !== 1 ? "s" : ""}</span>`}
              · strings: ${i.days} days · every ${i.interval_days}
            </div>
          </div>
          <button class="small primary" data-logrestring="${i.gear_id}" type="button">Log restring</button>
        </div>`).join("") : `<p class="empty">Nothing due. Fresh strings all round.</p>`}
    </div>`;
  view.querySelectorAll("[data-logrestring]").forEach((b) => b.addEventListener("click", async () => {
    const g = await api(`/api/gear/${b.dataset.logrestring}`);
    restringForm(g);
    sheetEl.querySelector("#rs-form").addEventListener("submit", () => setTimeout(dueView, 300));
  }));
}

/* ---------------------------------------------------------------- settings */

async function settingsView() {
  const [settings, notify, tokens, users] = await Promise.all([
    api("/api/settings"),
    state.me.is_admin ? api("/api/notifications") : Promise.resolve(null),
    api("/api/tokens"),
    state.me.is_admin ? api("/api/users") : Promise.resolve(null),
  ]);
  state.settings = settings;
  const isAdmin = state.me.is_admin;
  view.innerHTML = `
    <h1>Settings</h1>
    <div class="card settings-section stack">
      <h2 style="margin-top:0">General</h2>
      <form id="set-general" class="row">
        <div style="flex:1;min-width:200px"><label for="st-name">App name</label>
        <input id="st-name" maxlength="60" value="${esc(settings.app_name)}" ${isAdmin ? "" : "disabled"} /></div>
        ${isAdmin ? `<button class="small" type="submit" style="align-self:end">Save</button>` : ""}
      </form>
    </div>
    <div class="card settings-section stack">
      <h2 style="margin-top:0">Features</h2>
      <p class="hint">Hide the sections you don't use. Hidden sections keep their data; they just leave the interface.</p>
      ${[
        ["feature_guitars", "Guitars"], ["feature_amps", "Amps"], ["feature_pedals", "Pedals"],
        ["feature_picks", "Picks"], ["feature_sets", "Sets (rigs and boards)"],
        ["feature_maintenance", "Maintenance (restring tracking)"],
      ].map(([key, label]) => `
        <label class="toggle"><input type="checkbox" data-feature="${key}" ${settings[key] ? "checked" : ""} ${isAdmin ? "" : "disabled"} /> ${label}</label>`).join("")}
    </div>
    ${isAdmin ? `
    <div class="card settings-section stack">
      <h2 style="margin-top:0">Notifications</h2>
      <p class="hint">Apprise URLs (one per line or space-separated), e.g. ntfy://, pover://, tgram://. Sends when a guitar's strings pass their interval.</p>
      <form id="set-notify" class="stack">
        <div><label for="nt-urls">Notification URLs</label><textarea id="nt-urls">${esc(notify.notify_urls)}</textarea></div>
        <div class="form-grid">
          <div><label for="nt-mode">Send as</label>
            <select id="nt-mode">
              <option value="digest" ${notify.notify_mode === "digest" ? "selected" : ""}>One digest</option>
              <option value="each" ${notify.notify_mode === "each" ? "selected" : ""}>One per guitar</option>
            </select>
          </div>
          <div><label for="nt-hour">Send from hour</label><input id="nt-hour" type="number" min="0" max="23" value="${notify.notify_hour}" /></div>
          <div><label for="nt-qs">Quiet hours start</label><input id="nt-qs" type="number" min="0" max="23" value="${notify.quiet_start}" /></div>
          <div><label for="nt-qe">Quiet hours end</label><input id="nt-qe" type="number" min="0" max="23" value="${notify.quiet_end}" /></div>
          <div><label for="nt-repeat">Repeat overdue every (days, 0 = never)</label><input id="nt-repeat" type="number" min="0" max="30" value="${notify.overdue_repeat_days}" /></div>
          <div><label for="nt-public">Public app address (for links)</label><input id="nt-public" value="${esc(notify.public_url)}" placeholder="https://..." /></div>
        </div>
        <p class="error" id="nt-error"></p>
        <div class="row">
          <button class="small primary" type="submit">Save notifications</button>
          <button class="small" type="button" id="nt-test">Send a test</button>
        </div>
      </form>
    </div>` : ""}
    <div class="card settings-section stack">
      <h2 style="margin-top:0">API tokens</h2>
      <p class="hint">Tokens act as you over the <a href="/api/docs" target="_blank" rel="noopener">token API</a>. Treat them like passwords; anyone with a token can read and change your gear.</p>
      <div id="token-list" class="stack">
        ${tokens.length ? tokens.map((t) => `
          <div class="list-row">
            <span class="grow"><strong>${esc(t.name)}</strong> <span class="muted">${esc(t.prefix)}... · ${esc(t.created_by || "")}${t.last_used_at ? " · last used " + fmtDate(t.last_used_at.slice(0, 10)) : ""}</span></span>
            <button class="small ghost danger" data-deltoken="${t.id}" type="button">Revoke</button>
          </div>`).join("") : `<p class="muted">No tokens yet.</p>`}
      </div>
      <form id="add-token" class="row">
        <input id="tk-name" placeholder="Token name, e.g. my phone" required maxlength="60" style="flex:1" />
        <button class="small primary" type="submit">Create token</button>
      </form>
      <div id="token-result">${state.newToken ? `
        <div class="stack" style="margin-top:12px">
          <p class="hint wrap-any" style="margin:0">New token <strong>${esc(state.newToken.name)}</strong>. Copy it now; it won't be shown again.</p>
          <div class="token-new" id="token-value" style="user-select:all">${esc(state.newToken.token)}</div>
          <div class="row">
            <button class="small primary" id="token-copy" type="button">Copy token</button>
            <button class="small ghost" id="token-done" type="button">Done</button>
          </div>
        </div>` : ""}</div>
    </div>
    ${isAdmin ? `
    <div class="card settings-section stack">
      <h2 style="margin-top:0">Users</h2>
      <div class="stack">
        ${users.map((u) => `
          <div class="list-row">
            <span class="grow"><strong>${esc(u.username)}</strong> ${u.is_admin ? `<span class="badge">admin</span>` : ""} ${u.active ? "" : `<span class="badge">inactive</span>`}</span>
            ${u.id !== state.me.id ? `
              <button class="small ghost" data-resetpw="${u.id}" type="button">Reset password</button>
              <button class="small ghost" data-toggleactive="${u.id}" data-active="${u.active ? 1 : 0}" type="button">${u.active ? "Deactivate" : "Reactivate"}</button>` : `<span class="muted">you</span>`}
          </div>`).join("")}
      </div>
      <form id="add-user" class="form-grid">
        <div><label for="us-name">Username</label><input id="us-name" required /></div>
        <div><label for="us-pw">Password</label><input id="us-pw" type="password" required autocomplete="new-password" /></div>
        <div class="full row">
          <label class="toggle" style="flex:1"><input type="checkbox" id="us-admin" /> Administrator</label>
          <button class="small primary" type="submit">Add user</button>
        </div>
      </form>
      <p class="error" id="us-error"></p>
    </div>` : ""}
    <div class="card settings-section">
      <h2 style="margin-top:0">About</h2>
      <p class="muted">Gearsmith v0.1.0 (beta) · self-hosted, one container, your data stays on your box.</p>
    </div>`;

  if (isAdmin) {
    document.getElementById("set-general").addEventListener("submit", async (e) => {
      e.preventDefault();
      state.settings = await api("/api/settings", { method: "PUT", body: { app_name: document.getElementById("st-name").value } });
      state.appName = state.settings.app_name;
      document.getElementById("app-name").textContent = state.appName;
      toast("Saved");
    });
    view.querySelectorAll("[data-feature]").forEach((box) => box.addEventListener("change", async () => {
      state.settings = await api("/api/settings", { method: "PUT", body: { [box.dataset.feature]: box.checked } });
      toast(box.checked ? "Section shown" : "Section hidden");
      route();
    }));
    document.getElementById("set-notify").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/notifications", {
          method: "PUT",
          body: {
            notify_urls: document.getElementById("nt-urls").value,
            notify_mode: document.getElementById("nt-mode").value,
            notify_hour: Number(document.getElementById("nt-hour").value),
            quiet_start: Number(document.getElementById("nt-qs").value),
            quiet_end: Number(document.getElementById("nt-qe").value),
            overdue_repeat_days: Number(document.getElementById("nt-repeat").value),
            public_url: document.getElementById("nt-public").value,
          },
        });
        toast("Notifications saved");
      } catch (ex) { document.getElementById("nt-error").textContent = ex.message; }
    });
    document.getElementById("nt-test").addEventListener("click", async () => {
      try {
        await api("/api/notifications/test", { method: "POST" });
        toast("Test sent");
      } catch (ex) { toast(ex.message); }
    });
  }

  // The new token lives in state.newToken, so it survives every re-render of this view
  // (list refresh, revoking another token) until the user taps Done or leaves Settings.
  document.getElementById("add-token").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.submitter || e.target.querySelector("button[type=submit]");
    if (btn) btn.disabled = true;
    try {
      const t = await api("/api/tokens", { method: "POST", body: { name: document.getElementById("tk-name").value } });
      state.newToken = { id: t.id, name: t.name, token: t.token };
      await settingsView();
      document.getElementById("token-result").scrollIntoView({ block: "nearest" });
    } catch (ex) {
      toast(ex.message);
      if (btn) btn.disabled = false;
    }
  });
  const copyBtn = document.getElementById("token-copy");
  if (copyBtn) copyBtn.addEventListener("click", async () => {
    const value = state.newToken ? state.newToken.token : "";
    let ok = false;
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(value); ok = true; }
    } catch (ex) { ok = false; }
    if (!ok) {
      // Plain http (common on a LAN) has no clipboard API: fall back to a hidden textarea
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (ex) { ok = false; }
      ta.remove();
    }
    if (ok) { copyBtn.textContent = "Copied"; toast("Token copied"); }
    else {
      const range = document.createRange();
      range.selectNodeContents(document.getElementById("token-value"));
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      toast("Selected; copy it manually");
    }
  });
  const doneBtn = document.getElementById("token-done");
  if (doneBtn) doneBtn.addEventListener("click", () => {
    state.newToken = null;
    document.getElementById("token-result").innerHTML = "";
  });
  view.querySelectorAll("[data-deltoken]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Revoke this token? Anything using it stops working right away.")) return;
    await api(`/api/tokens/${b.dataset.deltoken}`, { method: "DELETE" });
    if (state.newToken && String(state.newToken.id) === b.dataset.deltoken) state.newToken = null;
    toast("Token revoked");
    await settingsView();
  }));

  if (isAdmin) {
    document.getElementById("add-user").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/users", {
          method: "POST",
          body: {
            username: document.getElementById("us-name").value,
            password: document.getElementById("us-pw").value,
            is_admin: document.getElementById("us-admin").checked,
          },
        });
        toast("User added");
        settingsView();
      } catch (ex) { document.getElementById("us-error").textContent = ex.message; }
    });
    view.querySelectorAll("[data-resetpw]").forEach((b) => b.addEventListener("click", async () => {
      const pw = prompt("New password for this user (8+ characters):");
      if (!pw) return;
      await api(`/api/users/${b.dataset.resetpw}`, { method: "PUT", body: { password: pw } });
      toast("Password reset");
    }));
    view.querySelectorAll("[data-toggleactive]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/users/${b.dataset.toggleactive}`, { method: "PUT", body: { active: b.dataset.active !== "1" } });
      settingsView();
    }));
  }
}

/* ---------------------------------------------------------------- boot */

async function boot(skipStatus = false) {
  if (!skipStatus) {
    const st = await api("/api/status");
    state.setupRequired = st.setup_required;
    state.appName = st.app_name || "Gearsmith";
  }
  document.getElementById("app-name").textContent = state.appName;
  try {
    state.me = await api("/api/me");
    state.settings = await api("/api/settings");
  } catch {
    state.me = null;
  }
  route();
}

document.getElementById("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  state.me = null;
  location.hash = "#/";
  route();
});

boot();
