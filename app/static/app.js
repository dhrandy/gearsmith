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
  document.getElementById("tab-songs").hidden = !featureOn("feature_songs");
  document.getElementById("tab-due").hidden = !featureOn("feature_maintenance");
  const hash = location.hash || "#/";
  const parts = hash.slice(2).split("/").filter(Boolean);
  let tab = "gear";
  if (parts[0] !== "settings") state.newToken = null;
  if (parts[0] === "gear" && parts[1]) { gearDetailView(Number(parts[1])); }
  else if (parts[0] === "want" || parts[0] === "sold") {
    featureOn("feature_" + parts[0]) ? gearListView(parts[0]) : (location.hash = "#/");
  }
  else if (parts[0] === "songs") {
    tab = "songs";
    if (!featureOn("feature_songs")) location.hash = "#/";
    else if (parts[1] === "new") songEditorView(null);
    else if (parts[1] && parts[2] === "edit") songEditorView(Number(parts[1]));
    else if (parts[1]) songDetailView(Number(parts[1]));
    else songsView();
  }
  else if (parts[0] === "sets") { tab = "sets"; featureOn("feature_sets") ? setsView() : (location.hash = "#/"); }
  else if (parts[0] === "due") { tab = "due"; featureOn("feature_maintenance") ? dueView() : (location.hash = "#/"); }
  else if (parts[0] === "settings") { tab = "settings"; settingsView(); }
  else { gearListView(); }
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));
}

window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------- favorites */

const FAV_ONLY_KEY = "gearsmith.favoritesOnly";

function favOnly() {
  try { return localStorage.getItem(FAV_ONLY_KEY) === "1"; } catch { return false; }
}

function setFavOnly(on) {
  try { localStorage.setItem(FAV_ONLY_KEY, on ? "1" : "0"); } catch { /* private mode: just don't remember */ }
}

// Same order the server uses: favorites first, then by name.
function byFavoriteThenName(a, b) {
  if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
  return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
}

function starButton(g, cls = "star", text = "") {
  const label = g.favorite ? `Remove ${g.name} from favorites` : `Add ${g.name} to favorites`;
  return `<button class="${cls}${g.favorite ? " on" : ""}" type="button" data-fav="${g.id}"
    aria-pressed="${g.favorite}" aria-label="${esc(label)}" title="${g.favorite ? "Favorite" : "Mark as favorite"}">
    <span aria-hidden="true">${g.favorite ? "★" : "☆"}</span>${text ? ` ${esc(text)}` : ""}</button>`;
}

async function toggleFavorite(g) {
  const updated = await api(`/api/gear/${g.id}`, { method: "PATCH", body: { favorite: !g.favorite } });
  g.favorite = updated.favorite;
  return g;
}

/* ---------------------------------------------------------------- gear list */

const LIFECYCLE_VIEWS = [
  ["owned", "Owned", "#/"],
  ["want", "Want", "#/want"],
  ["sold", "Sold", "#/sold"],
];

function lifecycleNav(current) {
  const shown = LIFECYCLE_VIEWS.filter(([key]) => key === "owned" || featureOn("feature_" + key));
  if (shown.length < 2) return "";
  return `<nav class="seg" aria-label="Owned, want and sold">
    ${shown.map(([key, label, href]) => `<a href="${href}" class="${key === current ? "on" : ""}" ${key === current ? 'aria-current="page"' : ""}>${label}</a>`).join("")}
  </nav>`;
}

function lifecycleBadge(g) {
  if (g.lifecycle === "want") {
    return `<span class="badge want">Want${g.want_price != null ? " · " + esc(fmtPrice(g.want_price)) : ""}</span>`;
  }
  if (g.lifecycle === "sold") {
    const bits = ["Sold", g.sold_date ? fmtDate(g.sold_date) : "", g.sold_price != null ? fmtPrice(g.sold_price) : ""].filter(Boolean);
    return `<span class="badge sold">${esc(bits.join(" · "))}</span>`;
  }
  return "";
}

async function gearListView(lifecycle = "owned") {
  const gear = await api(`/api/gear?lifecycle=${lifecycle}`);
  const visibleTypes = TYPE_ORDER.filter((t) => featureOn(FEATURE_FOR_TYPE[t]));
  const sections = visibleTypes.map((t) => ({ type: t, items: gear.filter((g) => g.type === t) }));
  const owned = lifecycle === "owned";
  const heading = { owned: "Gear", want: "Want list", sold: "Sold" }[lifecycle];
  const addLabel = { owned: "Add gear", want: "Add to want list", sold: "" }[lifecycle];
  view.innerHTML = `
    <div class="pagehead">
      <h1>${heading}</h1>
      ${addLabel ? `<button class="primary" id="add-gear" type="button">${addLabel}</button>` : ""}
    </div>
    ${lifecycleNav(lifecycle)}
    ${lifecycle === "want" ? `<p class="muted">Gear you're after. When you buy one, edit it and switch it to Owned.</p>` : ""}
    ${lifecycle === "sold" ? `<p class="muted">Gear you've sold, kept as history. It stays out of the strings list and the main gear page.</p>` : ""}
    <div class="toolbar">
      <input type="search" id="gear-search" placeholder="Filter by name, make, model..." />
      ${owned ? `<button class="fav-filter" id="fav-only" type="button" aria-pressed="false"><span aria-hidden="true">☆</span> Favorites only</button>` : ""}
    </div>
    <div id="gear-sections"></div>`;
  const container = document.getElementById("gear-sections");
  const search = document.getElementById("gear-search");
  const favBtn = document.getElementById("fav-only");
  let onlyFavs = owned && favOnly();
  function render() {
    const needle = search.value.trim().toLowerCase();
    if (favBtn) {
      favBtn.setAttribute("aria-pressed", String(onlyFavs));
      favBtn.classList.toggle("on", onlyFavs);
      favBtn.querySelector("span").textContent = onlyFavs ? "★" : "☆";
    }
    const html = sections.map(({ type, items }) => {
      const shown = items
        .filter((g) => !onlyFavs || g.favorite)
        .filter((g) => !needle || `${g.name} ${g.make} ${g.model}`.toLowerCase().includes(needle))
        .sort(byFavoriteThenName);
      if (!shown.length) return "";
      return `
        <h2 class="section-title">${esc(TYPE_LABEL[type])} <span class="count">${shown.length}</span></h2>
        <div class="grid">
          ${shown.map((g) => `
            <div class="gear-cell${g.lifecycle === "sold" ? " sold" : ""}${g.lifecycle === "sold" ? " no-star" : ""}">
            <a class="gear-card" href="#/gear/${g.id}">
              <span class="thumb">${g.cover ? `<img src="${g.cover}" alt="" />` : TYPE_ICON[g.type]}</span>
              <span class="gc-body">
                <span class="gc-name" title="${esc(g.name)}">${esc(g.name)}</span>
                <span class="gc-meta" title="${esc([g.make, g.model].filter(Boolean).join(" · "))}">${esc([g.make, g.model].filter(Boolean).join(" · ")) || "&nbsp;"}</span>
                <span class="gc-foot">
                  ${g.type === "guitar" ? stringsChip(g.strings) : ""}
                  ${lifecycleBadge(g)}
                  ${owned && g.status && g.status !== "home" ? `<span class="badge ${esc(g.status)}">${esc(g.status_label)}</span>` : ""}
                  ${g.sets.length ? `<span class="badge">${g.sets.length} set${g.sets.length > 1 ? "s" : ""}</span>` : ""}
                </span>
              </span>
            </a>
            ${g.lifecycle === "sold" ? "" : starButton(g)}
            </div>`).join("")}
        </div>`;
    }).join("");
    let empty = { owned: "No gear yet. Add your first piece.", want: "Nothing on your want list yet.", sold: "Nothing sold yet. Sold gear shows up here, kept as history." }[lifecycle];
    if (gear.length && onlyFavs && !gear.some((g) => g.favorite)) empty = "No favorites yet. Tap the star on any card to add one.";
    else if (gear.length) empty = "Nothing matches.";
    container.innerHTML = html || `<p class="empty">${esc(empty)}</p>`;
  }
  render();
  search.addEventListener("input", render);
  if (favBtn) favBtn.addEventListener("click", () => {
    onlyFavs = !onlyFavs;
    setFavOnly(onlyFavs);
    render();
  });
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-fav]");
    if (!btn) return;
    e.preventDefault();
    const g = gear.find((x) => x.id === Number(btn.dataset.fav));
    if (!g || btn.disabled) return;
    btn.disabled = true;
    try {
      await toggleFavorite(g);
      render();
      const again = container.querySelector(`[data-fav="${g.id}"]`);
      if (again) again.focus({ preventScroll: true });
    } catch (ex) {
      btn.disabled = false;
      toast(ex.message);
    }
  });
  const addBtn = document.getElementById("add-gear");
  if (addBtn) addBtn.addEventListener("click", () => gearForm(null, lifecycle));
}

/* ---------------------------------------------------------------- gear form */

function numOrNull(id) {
  const v = document.getElementById(id).value;
  return v === "" ? null : Number(v);
}

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

function gearForm(existing = null, lifecycle = "owned") {
  const g = existing || { type: "guitar", specs: {}, status: lifecycle === "owned" ? "home" : "", lifecycle };
  const life = g.lifecycle || "owned";
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
        <div><label for="gf-life">Owned, want or sold</label>
          <select id="gf-life">
            ${[["owned", "Owned"], ["want", "Want (not bought yet)"], ["sold", "Sold"]].map(([k, l]) => `<option value="${k}" ${life === k ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <div data-life="want"><label for="gf-wprice">Want price</label><input id="gf-wprice" type="number" min="0" step="0.01" value="${g.want_price ?? ""}" placeholder="What you'd pay" /></div>
        <div data-life="sold"><label for="gf-sdate">Sold on</label><input id="gf-sdate" type="date" value="${esc(g.sold_date || "")}" /></div>
        <div data-life="sold"><label for="gf-sprice">Sale price</label><input id="gf-sprice" type="number" min="0" step="0.01" value="${g.sold_price ?? ""}" /></div>
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
  const lifeSel = document.getElementById("gf-life");
  function renderLife() {
    sheetEl.querySelectorAll("[data-life]").forEach((el) => { el.hidden = el.dataset.life !== lifeSel.value; });
  }
  renderLife();
  lifeSel.addEventListener("change", renderLife);
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
      lifecycle: lifeSel.value,
      want_price: numOrNull("gf-wprice"),
      sold_date: document.getElementById("gf-sdate").value || null,
      sold_price: numOrNull("gf-sprice"),
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
        <span id="gd-fav-wrap">${starButton(g, "small star-btn", "Favorite")}</span>
        <button class="small" id="gd-edit" type="button">Edit</button>
        <button class="small danger" id="gd-delete" type="button">Delete</button>
      </div>
    </div>
    <p class="muted wrap-any">${esc(g.type_label.slice(0, -1))}${g.sets.length ? " · in " + g.sets.map((s) => esc(s.name)).join(", ") : ""}</p>
    ${g.lifecycle === "want" ? `<div class="life-banner want">On your want list${g.want_price != null ? ` · want price ${esc(fmtPrice(g.want_price))}` : ""}</div>` : ""}
    ${g.lifecycle === "sold" ? `<div class="life-banner sold">Sold${g.sold_date ? ` on ${esc(fmtDate(g.sold_date))}` : ""}${g.sold_price != null ? ` for ${esc(fmtPrice(g.sold_price))}` : ""} · kept as history</div>` : ""}
    <div class="hero">
      <div class="hero-photo">${g.cover ? `<img src="${g.cover}" alt="" />` : TYPE_ICON[g.type]}</div>
      <div>
        ${g.type === "guitar" && maintenance && g.strings ? `<p>${stringsChip(g.strings, true)}</p>` : ""}
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
    ${g.type === "guitar" && maintenance && g.strings ? `
    <h2>Strings</h2>
    <div class="card" id="gd-strings">
      <div class="row" style="justify-content:space-between">
        <div>${stringsChip(g.strings, true)}</div>
        <button class="primary small" id="log-restring" type="button">Log a restring</button>
      </div>
      <p class="hint wrap-any">Changed every ${g.strings.interval_days} days${g.strings.last_date ? ` · last: ${esc(g.strings.last_brand || "unknown")} ${esc(g.strings.last_gauge || "")} on ${fmtDate(g.strings.last_date)}` : ""}.</p>
      <div id="restring-history" class="stack" style="margin-top:10px"></div>
    </div>` : ""}
    ${CONTROL_TYPES.includes(g.type) ? `
    <h2>Controls</h2>
    <div class="card" id="gd-controls">
      <p class="hint" style="margin:0 0 10px">The knobs and switches on this ${esc(g.type)}, in panel order. Songs use them so you only type the settings.</p>
      <div class="set-chips">
        ${g.controls.length ? g.controls.map((ct) => `<span class="set-chip">${esc(ct.name)}${ct.kind === "switch" ? ` <span class="muted">switch</span>` : ""}</span>`).join("") : `<span class="muted">No controls listed yet.</span>`}
      </div>
      <div class="row" style="margin-top:12px">
        <button class="small" id="gd-edit-controls" type="button">Edit controls</button>
        ${g.type === "pedal" ? `<label class="toggle"><input type="checkbox" id="gd-modeler" ${g.modeler ? "checked" : ""} /> Modeler / multi-FX (has patches and scenes)</label>` : ""}
      </div>
    </div>` : ""}
    ${featureOn("feature_songs") ? `<h2>Songs</h2><div class="card" id="gd-songs"><span class="muted">Loading...</span></div>` : ""}
    <h2>Share</h2>
    <div class="card" id="gd-share"></div>
    ${featureOn("feature_sets") ? `
    <h2>Sets</h2>
    <div class="card">
      <div class="set-chips" id="gd-sets">
        ${g.sets.map((s) => `<span class="set-chip" title="${esc(s.name)}">${esc(s.name)}</span>`).join("") || `<span class="muted">Not in any set.</span>`}
      </div>
    </div>` : ""}`;

  const favWrap = document.getElementById("gd-fav-wrap");
  favWrap.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-fav]");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    try {
      await toggleFavorite(g);
      favWrap.innerHTML = starButton(g, "small star-btn", "Favorite");
      favWrap.querySelector("button").focus({ preventScroll: true });
    } catch (ex) {
      btn.disabled = false;
      toast(ex.message);
    }
  });
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

  if (g.type === "guitar" && maintenance && g.strings) {
    loadRestringHistory(g);
    document.getElementById("log-restring").addEventListener("click", () => restringForm(g));
  }
  if (CONTROL_TYPES.includes(g.type)) {
    document.getElementById("gd-edit-controls").addEventListener("click", () => controlsForm(g));
    const mod = document.getElementById("gd-modeler");
    if (mod) mod.addEventListener("change", async () => {
      try {
        await api(`/api/gear/${g.id}`, { method: "PATCH", body: { specs: { modeler: mod.checked } } });
        toast(mod.checked ? "Marked as a modeler" : "No longer a modeler");
      } catch (ex) { mod.checked = !mod.checked; toast(ex.message); }
    });
  }
  mountShare(document.getElementById("gd-share"), "gear", g.id, g.share);
  if (featureOn("feature_songs")) loadGearSongs(g);
}

async function loadGearSongs(g) {
  const songs = await api(`/api/songs?gear_id=${g.id}`);
  const el = document.getElementById("gd-songs");
  if (!el) return;
  el.innerHTML = songs.length
    ? `<div class="set-chips">${songs.map((s) => `<a class="set-chip link-chip" href="#/songs/${s.id}" title="${esc(s.title)}">${esc(s.title)}${s.artist ? ` <span class="muted">${esc(s.artist)}</span>` : ""}</a>`).join("")}</div>`
    : `<span class="muted">Not used in any song yet.</span>`;
}

/* ---------------------------------------------------------------- controls */

const CONTROL_TYPES = ["guitar", "amp", "pedal"];
const CONTROL_HINT = {
  guitar: "e.g. Volume, Tone, Pickup selector",
  amp: "e.g. Gain, Bass, Mid, Treble, Master",
  pedal: "e.g. Gain, Tone, Level",
};

function controlsForm(g) {
  let rows = g.controls.map((c) => ({ ...c }));
  if (!rows.length) rows.push({ name: "", kind: "knob" });
  function render() {
    openSheet(`
      <h2>Controls - ${esc(g.name)}</h2>
      <p class="hint">${esc(CONTROL_HINT[g.type])}. Order them like the panel; songs list them in this order.</p>
      <form id="ct-form" class="stack">
        <div class="stack" id="ct-rows">
          ${rows.map((r, i) => `
            <div class="ct-row">
              <input data-ct-name="${i}" maxlength="40" value="${esc(r.name)}" placeholder="Control name" aria-label="Control ${i + 1} name" />
              <select data-ct-kind="${i}" aria-label="Control ${i + 1} kind">
                <option value="knob" ${r.kind !== "switch" ? "selected" : ""}>Knob</option>
                <option value="switch" ${r.kind === "switch" ? "selected" : ""}>Switch</option>
              </select>
              <button class="small ghost" type="button" data-ct-up="${i}" aria-label="Move up" ${i === 0 ? "disabled" : ""}>↑</button>
              <button class="small ghost danger" type="button" data-ct-del="${i}" aria-label="Remove control ${i + 1}">✕</button>
            </div>`).join("")}
        </div>
        <button class="small" type="button" id="ct-add">Add a control</button>
        <p class="error" id="ct-error"></p>
        <div class="sheet-actions">
          <button type="button" id="ct-cancel">Cancel</button>
          <button class="primary" type="submit">Save controls</button>
        </div>
      </form>`);
    sheetEl.querySelectorAll("[data-ct-name]").forEach((el) => el.addEventListener("input", () => { rows[el.dataset.ctName].name = el.value; }));
    sheetEl.querySelectorAll("[data-ct-kind]").forEach((el) => el.addEventListener("change", () => { rows[el.dataset.ctKind].kind = el.value; }));
    sheetEl.querySelectorAll("[data-ct-del]").forEach((el) => el.addEventListener("click", () => { rows.splice(Number(el.dataset.ctDel), 1); render(); }));
    sheetEl.querySelectorAll("[data-ct-up]").forEach((el) => el.addEventListener("click", () => {
      const i = Number(el.dataset.ctUp);
      [rows[i - 1], rows[i]] = [rows[i], rows[i - 1]];
      render();
    }));
    document.getElementById("ct-add").addEventListener("click", () => {
      rows.push({ name: "", kind: "knob" });
      render();
      const inputs = sheetEl.querySelectorAll("[data-ct-name]");
      inputs[inputs.length - 1].focus();
    });
    document.getElementById("ct-cancel").addEventListener("click", closeSheet);
    document.getElementById("ct-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const controls = rows.filter((r) => r.name.trim()).map((r) => ({ name: r.name.trim(), kind: r.kind }));
      try {
        await api(`/api/gear/${g.id}`, { method: "PATCH", body: { specs: { controls } } });
        closeSheet();
        toast("Controls saved");
        gearDetailView(g.id);
      } catch (ex) { document.getElementById("ct-error").textContent = ex.message; }
    });
  }
  render();
}

/* ---------------------------------------------------------------- share links */

const EXPIRY_CHOICES = [["", "Never expires"], ["7", "7 days"], ["30", "30 days"], ["90", "90 days"]];

function fullShareUrl(share) {
  return location.origin + share.url;
}

async function copyText(value) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(value); return true; }
  } catch { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = value;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

// A share box for one piece of gear or one set: create, copy, set expiry, regenerate, turn off.
function mountShare(el, kind, id, share) {
  const base = kind === "gear" ? `/api/gear/${id}/share` : `/api/sets/${id}/share`;
  const noun = kind === "gear" ? "this item" : "this set";
  function expiryText(s) {
    if (!s.expires_at) return "Never expires.";
    return `Expires ${new Date(s.expires_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}.`;
  }
  function render() {
    const live = share && !share.expired;
    el.innerHTML = live ? `
      <p class="hint" style="margin:0 0 8px">Anyone with this link can view ${noun}, read-only. It isn't listed anywhere and search engines are told to skip it.</p>
      <div class="share-url" data-share-url>${esc(fullShareUrl(share))}</div>
      <p class="hint">${esc(expiryText(share))}</p>
      <div class="row" style="margin-top:8px">
        <button class="small primary" type="button" data-share-copy>Copy link</button>
        <a class="btn small" href="${esc(share.url)}" target="_blank" rel="noopener noreferrer">Open</a>
        <select class="share-expiry" data-share-expiry aria-label="Change expiry">
          <option value="keep" selected>Change expiry...</option>
          ${EXPIRY_CHOICES.map(([v, l]) => `<option value="${v}">${v ? "Expire in " + l : l}</option>`).join("")}
        </select>
        <button class="small" type="button" data-share-new>New link</button>
        <button class="small danger" type="button" data-share-off>Turn off</button>
      </div>` : `
      <p class="hint" style="margin:0 0 8px">${share && share.expired ? "The last link expired. " : ""}Make a private link to show ${noun} to someone: read-only, not listed or searchable, and you can turn it off any time.</p>
      <div class="row">
        <select class="share-expiry" data-share-expiry-new aria-label="Link expiry">
          ${EXPIRY_CHOICES.map(([v, l]) => `<option value="${v}" ${v === "30" ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <button class="small primary" type="button" data-share-create>Create link</button>
      </div>`;
    const q = (sel) => el.querySelector(sel);
    const days = (v) => (v ? Number(v) : null);
    if (q("[data-share-create]")) q("[data-share-create]").addEventListener("click", async () => {
      try {
        share = (await api(base, { method: "POST", body: { expires_in_days: days(q("[data-share-expiry-new]").value), regenerate: true } })).share;
        render();
        toast("Share link created");
      } catch (ex) { toast(ex.message); }
    });
    if (q("[data-share-copy]")) q("[data-share-copy]").addEventListener("click", async () => {
      if (await copyText(fullShareUrl(share))) toast("Link copied");
      else {
        const range = document.createRange();
        range.selectNodeContents(q("[data-share-url]"));
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
        toast("Selected; copy it manually");
      }
    });
    if (q("[data-share-expiry]")) q("[data-share-expiry]").addEventListener("change", async (e) => {
      if (e.target.value === "keep") return;
      try {
        share = (await api(base, { method: "POST", body: { expires_in_days: days(e.target.value) } })).share;
        render();
        toast("Expiry updated");
      } catch (ex) { toast(ex.message); }
    });
    if (q("[data-share-new]")) q("[data-share-new]").addEventListener("click", async () => {
      if (!confirm("Make a new link? The current link stops working.")) return;
      try {
        const keepDays = share.expires_at ? Math.max(1, Math.round((new Date(share.expires_at) - Date.now()) / 864e5)) : null;
        share = (await api(base, { method: "POST", body: { regenerate: true, expires_in_days: keepDays } })).share;
        render();
        toast("New link made; the old one is off");
      } catch (ex) { toast(ex.message); }
    });
    if (q("[data-share-off]")) q("[data-share-off]").addEventListener("click", async () => {
      if (!confirm("Turn off this link? Anyone who has it loses access.")) return;
      try {
        await api(base, { method: "DELETE" });
        share = null;
        render();
        toast("Link turned off");
      } catch (ex) { toast(ex.message); }
    });
  }
  render();
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

/* ---------------------------------------------------------------- songs */

const ENGAGED_LABEL = { on: "On", off: "Off", toggle: "Toggle" };
let tuningSuggestions = null;

function songChips(s) {
  return [
    s.tuning ? `<span class="chip">${esc(s.tuning)}</span>` : "",
    s.capo ? `<span class="chip">Capo ${esc(s.capo)}</span>` : "",
    s.key ? `<span class="chip">Key ${esc(s.key)}</span>` : "",
    s.bpm ? `<span class="chip">${esc(s.bpm)} bpm</span>` : "",
  ].join("");
}

async function songsView() {
  const songs = await api("/api/songs");
  view.innerHTML = `
    <div class="pagehead">
      <h1>Songs</h1>
      <a class="btn primary" href="#/songs/new">Add song</a>
    </div>
    <p class="muted">Your rig and tone for each song: which guitar and amp, where every knob sits, and which patch and scene on a modeler.</p>
    <div class="toolbar"><input type="search" id="song-search" placeholder="Filter by title or artist..." /></div>
    <div class="grid" id="song-list"></div>`;
  const list = document.getElementById("song-list");
  const search = document.getElementById("song-search");
  function render() {
    const needle = search.value.trim().toLowerCase();
    const shown = songs.filter((s) => !needle || `${s.title} ${s.artist}`.toLowerCase().includes(needle));
    list.innerHTML = shown.map((s) => `
      <a class="gear-card song-card" href="#/songs/${s.id}">
        <span class="thumb">${s.cover ? `<img src="${s.cover}" alt="" />` : "♪"}</span>
        <span class="gc-body">
          <span class="gc-name" title="${esc(s.title)}">${esc(s.title)}</span>
          <span class="gc-meta" title="${esc(s.artist)}">${esc(s.artist) || "&nbsp;"}</span>
          <span class="gc-foot">${songChips(s)}${s.guitar_name ? `<span class="badge">🎸 ${esc(s.guitar_name)}</span>` : ""}</span>
        </span>
      </a>`).join("") || `<p class="empty">${songs.length ? "Nothing matches." : "No songs yet. Add one to save its rig and settings."}</p>`;
  }
  render();
  search.addEventListener("input", render);
}

function knobText(knobs) {
  return knobs.filter((k) => k.name).map((k) => `<span class="knob"><span>${esc(k.name)}</span>${esc(k.value || "-")}</span>`).join("");
}

async function songDetailView(id) {
  const s = await api(`/api/songs/${id}`);
  const gearLink = (gid, name) => (gid ? `<a href="#/gear/${gid}">${esc(name)}</a>` : esc(name));
  const rigFacts = [
    ["Guitar", s.guitar_name ? gearLink(s.guitar_id, s.guitar_name) : ""],
    ["Amp", s.amp_name ? gearLink(s.amp_id, s.amp_name) : ""],
    ["Set", s.set_name && featureOn("feature_sets") ? esc(s.set_name) : ""],
  ].filter(([, v]) => v);
  view.innerHTML = `
    <div class="pagehead">
      <h1>${esc(s.title)}</h1>
      <div class="row">
        <a class="btn small" href="#/songs/${s.id}/edit">Edit</a>
        <button class="small danger" id="sd-delete" type="button">Delete</button>
      </div>
    </div>
    ${s.artist ? `<p class="muted wrap-any" style="margin-top:0">${esc(s.artist)}</p>` : ""}
    <div class="chips">${songChips(s)}</div>
    ${rigFacts.length ? `<div class="facts recall-facts">${rigFacts.map(([k, v]) => `<div class="fact"><span>${k}</span>${v}</div>`).join("")}</div>` : ""}
    <h2>Signal chain</h2>
    ${s.rig.length ? `<ol class="chain">
      ${s.rig.map((r) => `
        <li class="chain-item${r.engaged === "off" ? " off" : ""}">
          <div class="chain-head">
            <strong class="wrap-any">${gearLink(r.gear_id, r.gear_name)}</strong>
            <span class="engaged eng-${esc(r.engaged)}">${ENGAGED_LABEL[r.engaged] || esc(r.engaged)}</span>
          </div>
          ${r.knobs.length ? `<div class="knobs">${knobText(r.knobs)}</div>` : ""}
          ${r.note ? `<p class="notes muted">${esc(r.note)}</p>` : ""}
        </li>`).join("")}
    </ol>` : `<p class="muted">No gear settings saved for this song.</p>`}
    ${s.patches.length ? `
    <h2>Patches</h2>
    <div class="stack">
      ${s.patches.map((p) => `
        <div class="card patch-card">
          <div class="chain-head">
            <span class="wrap-any"><strong>${gearLink(p.gear_id, p.gear_name)}</strong></span>
            ${p.patch_ref ? `<span class="patch-ref">${esc(p.patch_ref)}</span>` : ""}
          </div>
          ${p.patch_name ? `<p class="wrap-any" style="margin:6px 0 0">${esc(p.patch_name)}</p>` : ""}
          ${p.scenes.length ? `<div class="chips" style="margin-top:8px">${p.scenes.map((sc) => `<span class="chip">${esc(sc)}</span>`).join("")}</div>` : ""}
          ${p.blocks.length ? `<div class="blocks">${p.blocks.map((b) => `
            <div class="block${b.enabled ? "" : " off"}">
              <div class="wrap-any"><strong>${esc(b.block_type || "Block")}</strong>${b.model ? ` <span class="muted">${esc(b.model)}</span>` : ""}${b.enabled ? "" : ` <span class="engaged eng-off">Off</span>`}</div>
              ${b.params.length ? `<div class="knobs">${knobText(b.params)}</div>` : ""}
            </div>`).join("")}</div>` : ""}
          ${p.note ? `<p class="notes muted">${esc(p.note)}</p>` : ""}
        </div>`).join("")}
    </div>` : ""}
    ${s.notes ? `<h2>Notes</h2><p class="notes wrap-any">${esc(s.notes)}</p>` : ""}
    <h2>Photos</h2>
    <div class="card">
      <div class="photo-strip">
        ${s.photos.map((p, i) => `
          <div class="photo-item">
            <img src="${p.url}" alt="" />
            <div class="row">
              ${i > 0 ? `<button class="small ghost" data-scover="${p.id}" type="button">Cover</button>` : `<span class="badge">Cover</span>`}
              <button class="small ghost danger" data-sdelphoto="${p.id}" type="button">Delete</button>
            </div>
          </div>`).join("")}
      </div>
      <form id="song-photo-form" class="row">
        <input type="file" id="song-photo-file" accept="image/*" required />
        <button class="small" type="submit">Upload</button>
      </form>
      <p class="hint">Handy for a photo of the board or the amp's knobs.</p>
    </div>`;
  document.getElementById("sd-delete").addEventListener("click", () => {
    openSheet(`
      <h2>Delete ${esc(s.title)}?</h2>
      <p class="muted">This removes the song, its settings and photos. The gear stays.</p>
      <div class="sheet-actions">
        <button type="button" id="del-cancel">Cancel</button>
        <button class="primary danger" id="del-confirm" type="button">Delete</button>
      </div>`);
    document.getElementById("del-cancel").addEventListener("click", closeSheet);
    document.getElementById("del-confirm").addEventListener("click", async () => {
      await api(`/api/songs/${id}`, { method: "DELETE" });
      closeSheet();
      toast("Song deleted");
      location.hash = "#/songs";
    });
  });
  document.getElementById("song-photo-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = document.getElementById("song-photo-file").files[0];
    if (!file) return;
    const data = new FormData();
    data.append("photo", file);
    try {
      await api(`/api/songs/${id}/photos`, { method: "POST", body: data });
      toast("Photo added");
      songDetailView(id);
    } catch (ex) { toast(ex.message); }
  });
  view.querySelectorAll("[data-scover]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/song-photos/${b.dataset.scover}/cover`, { method: "POST" });
    songDetailView(id);
  }));
  view.querySelectorAll("[data-sdelphoto]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/song-photos/${b.dataset.sdelphoto}`, { method: "DELETE" });
    songDetailView(id);
  }));
}

function knobsFromControls(g) {
  const controls = (g && g.controls) || [];
  if (!controls.length) return [{ name: "", value: "", kind: "knob" }];
  return controls.map((c) => ({ name: c.name, value: "", kind: c.kind }));
}

async function songEditorView(id) {
  const [song, gear, sets, opts] = await Promise.all([
    id ? api(`/api/songs/${id}`) : Promise.resolve(null),
    api("/api/gear?lifecycle=owned"),
    featureOn("feature_sets") ? api("/api/sets") : Promise.resolve([]),
    tuningSuggestions ? Promise.resolve({ tunings: tuningSuggestions }) : api("/api/song-options"),
  ]);
  tuningSuggestions = opts.tunings;
  const byId = new Map(gear.map((g) => [g.id, g]));
  const kindOf = (gid, name) => {
    const g = byId.get(gid);
    const c = g && g.controls.find((x) => x.name.toLowerCase() === String(name).toLowerCase());
    return c ? c.kind : "knob";
  };
  const draft = song ? {
    title: song.title, artist: song.artist, tuning: song.tuning, capo: song.capo ?? "", key: song.key,
    bpm: song.bpm ?? "", guitar_id: song.guitar_id, amp_id: song.amp_id, set_id: song.set_id, notes: song.notes,
    rig: song.rig.map((r) => ({
      gear_id: r.gear_id, gear_name: r.gear_name, engaged: r.engaged, note: r.note,
      knobs: r.knobs.map((k) => ({ ...k, kind: kindOf(r.gear_id, k.name) })),
    })),
    patches: song.patches.map((p) => ({
      gear_id: p.gear_id, gear_name: p.gear_name, patch_ref: p.patch_ref, patch_name: p.patch_name,
      scenes: p.scenes.join(", "), note: p.note, midi: p.midi,
      blocks: p.blocks.map((b) => ({ ...b, params: b.params.map((x) => ({ ...x })) })),
      showBlocks: p.blocks.length > 0,
    })),
  } : {
    title: "", artist: "", tuning: "", capo: "", key: "", bpm: "", guitar_id: null, amp_id: null, set_id: null,
    notes: "", rig: [], patches: [],
  };
  const guitars = gear.filter((g) => g.type === "guitar");
  const amps = gear.filter((g) => g.type === "amp");
  const rigGear = gear.filter((g) => CONTROL_TYPES.includes(g.type));
  const modelers = gear.filter((g) => g.type === "pedal" && g.modeler);
  const gearOptions = (list, selected, keepName) => {
    let html = list.map((g) => `<option value="${g.id}" ${g.id === selected ? "selected" : ""}>${esc(g.name)}</option>`).join("");
    if (selected && !byId.has(selected)) html += `<option value="${selected}" selected>${esc(keepName || "Gear #" + selected)}</option>`;
    return html;
  };

  function addToRig(g, atStart = false) {
    if (draft.rig.some((r) => r.gear_id === g.id)) return;
    const row = { gear_id: g.id, gear_name: g.name, engaged: "on", note: "", knobs: knobsFromControls(g) };
    if (atStart) draft.rig.unshift(row); else draft.rig.push(row);
  }

  function knobRows(list, prefix, idx) {
    return list.map((k, j) => `
      <div class="knob-row">
        <input data-${prefix}-kname="${idx}:${j}" maxlength="40" value="${esc(k.name)}" placeholder="${prefix === "b" ? "Param" : "Control"}" aria-label="Control name" />
        <input data-${prefix}-kval="${idx}:${j}" maxlength="40" value="${esc(k.value)}" placeholder="${k.kind === "switch" ? "e.g. Bright" : "e.g. 2:00, noon, 7"}" aria-label="${esc(k.name || "Control")} setting" />
        <button class="small ghost danger" type="button" data-${prefix}-kdel="${idx}:${j}" aria-label="Remove ${esc(k.name || "control")}">✕</button>
      </div>`).join("");
  }

  function render() {
    view.innerHTML = `
      <div class="pagehead"><h1>${song ? "Edit song" : "Add song"}</h1></div>
      <form id="song-form" class="stack">
        <div class="card stack">
          <div class="form-grid">
            <div><label for="sg-title">Title</label><input id="sg-title" data-f="title" required maxlength="120" value="${esc(draft.title)}" /></div>
            <div><label for="sg-artist">Artist</label><input id="sg-artist" data-f="artist" maxlength="120" value="${esc(draft.artist)}" /></div>
            <div><label for="sg-tuning">Tuning</label><input id="sg-tuning" data-f="tuning" maxlength="40" list="tuning-list" value="${esc(draft.tuning)}" placeholder="E Std" />
              <datalist id="tuning-list">${tuningSuggestions.map((t) => `<option value="${esc(t)}"></option>`).join("")}</datalist></div>
            <div class="form-grid tight">
              <div><label for="sg-capo">Capo</label><input id="sg-capo" data-f="capo" type="number" min="0" max="24" value="${esc(draft.capo)}" placeholder="None" /></div>
              <div><label for="sg-key">Key</label><input id="sg-key" data-f="key" maxlength="20" value="${esc(draft.key)}" placeholder="e.g. Am" /></div>
              <div><label for="sg-bpm">BPM</label><input id="sg-bpm" data-f="bpm" type="number" min="1" max="400" value="${esc(draft.bpm)}" /></div>
            </div>
            <div><label for="sg-guitar">Guitar</label>
              <select id="sg-guitar"><option value="">None</option>${gearOptions(guitars, draft.guitar_id, song && song.guitar_name)}</select></div>
            <div><label for="sg-amp">Amp</label>
              <select id="sg-amp"><option value="">None</option>${gearOptions(amps, draft.amp_id, song && song.amp_name)}</select></div>
            ${featureOn("feature_sets") ? `<div class="full"><label for="sg-set">Set (board or rig)</label>
              <div class="row set-pick">
                <select id="sg-set"><option value="">None</option>${sets.map((st) => `<option value="${st.id}" ${st.id === draft.set_id ? "selected" : ""}>${esc(st.name)}</option>`).join("")}</select>
                <button class="small" type="button" id="sg-set-load" ${draft.set_id ? "" : "disabled"}>Add its gear to the chain</button>
              </div></div>` : ""}
          </div>
        </div>

        <h2>Signal chain</h2>
        <p class="hint" style="margin-top:-6px">Each piece of gear in the order the signal runs, with its settings for this song. Knob names come from the gear's controls; type the positions.</p>
        <div class="stack" id="rig-rows">
          ${draft.rig.map((r, i) => `
            <div class="card rig-row">
              <div class="rig-head">
                <strong class="wrap-any">${i + 1}. ${esc(r.gear_name)}</strong>
                <span class="row nowrap">
                  <button class="small ghost" type="button" data-rig-up="${i}" aria-label="Move ${esc(r.gear_name)} earlier" ${i === 0 ? "disabled" : ""}>↑</button>
                  <button class="small ghost" type="button" data-rig-down="${i}" aria-label="Move ${esc(r.gear_name)} later" ${i === draft.rig.length - 1 ? "disabled" : ""}>↓</button>
                  <button class="small ghost danger" type="button" data-rig-del="${i}" aria-label="Remove ${esc(r.gear_name)}">✕</button>
                </span>
              </div>
              <div class="seg small-seg" role="radiogroup" aria-label="${esc(r.gear_name)} on or off">
                ${["on", "off", "toggle"].map((e) => `<button type="button" role="radio" aria-checked="${r.engaged === e}" class="${r.engaged === e ? "on" : ""}" data-rig-eng="${i}:${e}">${ENGAGED_LABEL[e]}</button>`).join("")}
              </div>
              <div class="knob-list">${knobRows(r.knobs, "r", i)}</div>
              <div class="row">
                <button class="small ghost" type="button" data-rig-kadd="${i}">Add a knob</button>
              </div>
              <input data-rig-note="${i}" maxlength="1000" value="${esc(r.note)}" placeholder="Note (optional), e.g. kick on for the solo" aria-label="${esc(r.gear_name)} note" />
            </div>`).join("") || `<p class="muted">Nothing in the chain yet.</p>`}
        </div>
        <div class="row nowrap">
          <select id="rig-pick" aria-label="Gear to add">
            <option value="">Add gear to the chain...</option>
            ${["guitar", "pedal", "amp"].map((t) => {
              const items = rigGear.filter((g) => g.type === t && !draft.rig.some((r) => r.gear_id === g.id));
              return items.length ? `<optgroup label="${TYPE_LABEL[t]}">${items.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}</optgroup>` : "";
            }).join("")}
          </select>
        </div>

        <h2>Modeler patches</h2>
        <p class="hint" style="margin-top:-6px">For multi-FX units: the patch as the device shows it, and the scenes or snapshots you use.</p>
        <div class="stack" id="patch-rows">
          ${draft.patches.map((p, i) => `
            <div class="card rig-row">
              <div class="rig-head">
                <strong class="wrap-any">${esc(p.gear_name)}</strong>
                <button class="small ghost danger" type="button" data-p-del="${i}" aria-label="Remove ${esc(p.gear_name)} patch">✕</button>
              </div>
              <div class="form-grid">
                <div><label for="p-ref-${i}">Patch</label><input id="p-ref-${i}" data-p-f="${i}:patch_ref" maxlength="40" value="${esc(p.patch_ref)}" placeholder="e.g. 12B or Bank 3 / Patch 2" /></div>
                <div><label for="p-name-${i}">Patch name</label><input id="p-name-${i}" data-p-f="${i}:patch_name" maxlength="80" value="${esc(p.patch_name)}" /></div>
                <div><label for="p-scenes-${i}">Scenes / snapshots</label><input id="p-scenes-${i}" data-p-f="${i}:scenes" maxlength="400" value="${esc(p.scenes)}" placeholder="verse, chorus, solo" /></div>
                <div><label for="p-note-${i}">Note</label><input id="p-note-${i}" data-p-f="${i}:note" maxlength="1000" value="${esc(p.note)}" /></div>
              </div>
              <details data-p-blocks="${i}" ${p.showBlocks ? "open" : ""}>
                <summary>Effect blocks (optional)</summary>
                <div class="stack" style="margin-top:10px">
                  ${p.blocks.map((b, j) => `
                    <div class="block-edit">
                      <div class="block-fields">
                        <input data-b-f="${i}:${j}:block_type" maxlength="40" value="${esc(b.block_type)}" placeholder="Type, e.g. Drive" aria-label="Block type" />
                        <input data-b-f="${i}:${j}:model" maxlength="80" value="${esc(b.model)}" placeholder="Model" aria-label="Block model" />
                        <label class="toggle compact"><input type="checkbox" data-b-on="${i}:${j}" ${b.enabled ? "checked" : ""} /> On</label>
                        <button class="small ghost danger" type="button" data-b-del="${i}:${j}" aria-label="Remove block">✕</button>
                      </div>
                      <div class="knob-list">${knobRows(b.params, "b", `${i}:${j}`)}</div>
                      <button class="small ghost" type="button" data-b-kadd="${i}:${j}">Add a setting</button>
                    </div>`).join("")}
                  <button class="small" type="button" data-b-add="${i}">Add a block</button>
                </div>
              </details>
            </div>`).join("")}
        </div>
        ${modelers.length ? `
        <div class="row nowrap">
          <select id="patch-pick" aria-label="Modeler to add a patch for">
            <option value="">Add a patch for...</option>
            ${modelers.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}
          </select>
        </div>` : `<p class="hint">No modelers yet. Open a multi-FX pedal's gear page and tick "Modeler" to add patches here.</p>`}

        <div class="card"><label for="sg-notes">Notes</label><textarea id="sg-notes" data-f="notes" maxlength="4000">${esc(draft.notes)}</textarea></div>
        <p class="error" id="sg-error"></p>
        <div class="sheet-actions sticky-actions">
          <a class="btn" href="${song ? `#/songs/${song.id}` : "#/songs"}">Cancel</a>
          <button class="primary" type="submit">${song ? "Save song" : "Add song"}</button>
        </div>
      </form>`;
    bind();
  }

  const pair = (v) => v.split(":").map(Number);
  function bind() {
    const $ = (sel) => view.querySelectorAll(sel);
    $("[data-f]").forEach((el) => el.addEventListener("input", () => { draft[el.dataset.f] = el.value; }));
    document.getElementById("sg-guitar").addEventListener("change", (e) => {
      draft.guitar_id = e.target.value ? Number(e.target.value) : null;
      const g = byId.get(draft.guitar_id);
      if (g) { addToRig(g, true); render(); }
    });
    document.getElementById("sg-amp").addEventListener("change", (e) => {
      draft.amp_id = e.target.value ? Number(e.target.value) : null;
      const g = byId.get(draft.amp_id);
      if (g) { addToRig(g); render(); }
    });
    const setSel = document.getElementById("sg-set");
    if (setSel) {
      setSel.addEventListener("change", (e) => {
        draft.set_id = e.target.value ? Number(e.target.value) : null;
        document.getElementById("sg-set-load").disabled = !draft.set_id;
      });
      document.getElementById("sg-set-load").addEventListener("click", () => {
        const st = sets.find((x) => x.id === draft.set_id);
        if (!st) return;
        st.items.forEach((it) => { const g = byId.get(it.id); if (g && CONTROL_TYPES.includes(g.type)) addToRig(g); });
        render();
      });
    }
    $("[data-rig-up]").forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.dataset.rigUp);
      [draft.rig[i - 1], draft.rig[i]] = [draft.rig[i], draft.rig[i - 1]];
      render();
    }));
    $("[data-rig-down]").forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.dataset.rigDown);
      [draft.rig[i + 1], draft.rig[i]] = [draft.rig[i], draft.rig[i + 1]];
      render();
    }));
    $("[data-rig-del]").forEach((b) => b.addEventListener("click", () => { draft.rig.splice(Number(b.dataset.rigDel), 1); render(); }));
    $("[data-rig-eng]").forEach((b) => b.addEventListener("click", () => {
      const [i, e] = b.dataset.rigEng.split(":");
      draft.rig[Number(i)].engaged = e;
      render();
    }));
    $("[data-rig-note]").forEach((el) => el.addEventListener("input", () => { draft.rig[Number(el.dataset.rigNote)].note = el.value; }));
    $("[data-r-kname]").forEach((el) => el.addEventListener("input", () => { const [i, j] = pair(el.dataset.rKname); draft.rig[i].knobs[j].name = el.value; }));
    $("[data-r-kval]").forEach((el) => el.addEventListener("input", () => { const [i, j] = pair(el.dataset.rKval); draft.rig[i].knobs[j].value = el.value; }));
    $("[data-r-kdel]").forEach((b) => b.addEventListener("click", () => { const [i, j] = pair(b.dataset.rKdel); draft.rig[i].knobs.splice(j, 1); render(); }));
    $("[data-rig-kadd]").forEach((b) => b.addEventListener("click", () => {
      const i = Number(b.dataset.rigKadd);
      draft.rig[i].knobs.push({ name: "", value: "", kind: "knob" });
      render();
      const names = view.querySelectorAll(`[data-r-kname^="${i}:"]`);
      names[names.length - 1].focus();
    }));
    const rigPick = document.getElementById("rig-pick");
    rigPick.addEventListener("change", () => {
      const g = byId.get(Number(rigPick.value));
      if (g) { addToRig(g); render(); }
    });
    $("[data-p-del]").forEach((b) => b.addEventListener("click", () => { draft.patches.splice(Number(b.dataset.pDel), 1); render(); }));
    $("[data-p-f]").forEach((el) => el.addEventListener("input", () => {
      const [i, f] = el.dataset.pF.split(":");
      draft.patches[Number(i)][f] = el.value;
    }));
    $("[data-p-blocks]").forEach((d) => d.addEventListener("toggle", () => { draft.patches[Number(d.dataset.pBlocks)].showBlocks = d.open; }));
    $("[data-b-add]").forEach((b) => b.addEventListener("click", () => {
      draft.patches[Number(b.dataset.bAdd)].blocks.push({ slot: "", block_type: "", model: "", enabled: true, params: [] });
      render();
    }));
    $("[data-b-f]").forEach((el) => el.addEventListener("input", () => {
      const [i, j, f] = el.dataset.bF.split(":");
      draft.patches[Number(i)].blocks[Number(j)][f] = el.value;
    }));
    $("[data-b-on]").forEach((el) => el.addEventListener("change", () => { const [i, j] = pair(el.dataset.bOn); draft.patches[i].blocks[j].enabled = el.checked; }));
    $("[data-b-del]").forEach((b) => b.addEventListener("click", () => { const [i, j] = pair(b.dataset.bDel); draft.patches[i].blocks.splice(j, 1); render(); }));
    $("[data-b-kadd]").forEach((b) => b.addEventListener("click", () => { const [i, j] = pair(b.dataset.bKadd); draft.patches[i].blocks[j].params.push({ name: "", value: "" }); render(); }));
    $("[data-b-kname]").forEach((el) => el.addEventListener("input", () => { const [i, j, k] = pair(el.dataset.bKname); draft.patches[i].blocks[j].params[k].name = el.value; }));
    $("[data-b-kval]").forEach((el) => el.addEventListener("input", () => { const [i, j, k] = pair(el.dataset.bKval); draft.patches[i].blocks[j].params[k].value = el.value; }));
    $("[data-b-kdel]").forEach((b) => b.addEventListener("click", () => { const [i, j, k] = pair(b.dataset.bKdel); draft.patches[i].blocks[j].params.splice(k, 1); render(); }));
    const patchPick = document.getElementById("patch-pick");
    if (patchPick) patchPick.addEventListener("change", () => {
      const g = byId.get(Number(patchPick.value));
      if (!g) return;
      draft.patches.push({ gear_id: g.id, gear_name: g.name, patch_ref: "", patch_name: "", scenes: "", note: "", blocks: [], showBlocks: false });
      render();
      document.getElementById(`p-ref-${draft.patches.length - 1}`).focus();
    });
    document.getElementById("song-form").addEventListener("submit", save);
  }

  const cleanKnobs = (list) => list.filter((k) => k.name.trim()).map((k) => ({ name: k.name.trim(), value: String(k.value).trim() }));
  async function save(e) {
    e.preventDefault();
    const err = document.getElementById("sg-error");
    err.textContent = "";
    const intOrNull = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
    const body = {
      title: draft.title, artist: draft.artist, tuning: draft.tuning, capo: intOrNull(draft.capo), key: draft.key,
      bpm: intOrNull(draft.bpm), guitar_id: draft.guitar_id, amp_id: draft.amp_id, set_id: draft.set_id, notes: draft.notes,
      rig: draft.rig.map((r) => ({
        gear_id: r.gear_id, gear_name: r.gear_name, engaged: r.engaged, note: r.note, knobs: cleanKnobs(r.knobs),
      })),
      patches: draft.patches.map((p) => ({
        gear_id: p.gear_id, gear_name: p.gear_name, patch_ref: p.patch_ref, patch_name: p.patch_name, note: p.note,
        midi: p.midi || null,
        scenes: String(p.scenes).split(",").map((x) => x.trim()).filter(Boolean),
        blocks: p.blocks.map((b) => ({
          slot: b.slot || "", block_type: b.block_type, model: b.model, enabled: b.enabled,
          params: cleanKnobs(b.params), scene_overrides: b.scene_overrides || null,
        })),
      })),
    };
    try {
      const saved = song
        ? await api(`/api/songs/${song.id}`, { method: "PATCH", body })
        : await api("/api/songs", { method: "POST", body });
      toast(song ? "Saved" : "Song added");
      location.hash = `#/songs/${saved.id}`;
    } catch (ex) {
      err.textContent = ex.message;
      err.scrollIntoView({ block: "center" });
    }
  }

  render();
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
              <button class="small ghost" data-shareset="${s.id}" type="button">${s.share && !s.share.expired ? "Shared" : "Share"}</button>
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
  view.querySelectorAll("[data-shareset]").forEach((b) => b.addEventListener("click", () => {
    const s = sets.find((x) => x.id === Number(b.dataset.shareset));
    openSheet(`<h2>Share ${esc(s.name)}</h2><div id="share-box"></div>
      <div class="sheet-actions"><button type="button" id="share-done">Done</button></div>`);
    mountShare(document.getElementById("share-box"), "set", s.id, s.share);
    document.getElementById("share-done").addEventListener("click", () => { closeSheet(); setsView(); });
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
        ["feature_songs", "Songs (rig and tone settings per song)"], ["feature_want", "Want list"],
        ["feature_sold", "Sold archive"],
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
