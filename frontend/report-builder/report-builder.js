"use strict";
const $ = id => document.getElementById(id);
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = String(text); return n; }
// sel: chosen data sets in the order picked; sec: settings per data set (kept when unpicked).
const S = {opts: null, sel: [], sec: {}, nvrs: new Set(), result: null, tab: null, ranSpec: "", busy: false, saved: []};
const KIND_OPS = {
  text: ["contains", "not_contains", "is", "is_not", "empty", "not_empty"],
  enum: ["is", "is_not", "empty", "not_empty"],
  number: ["gt", "ge", "lt", "le", "is", "is_not", "empty", "not_empty"],
  percent: ["lt", "le", "gt", "ge", "is", "empty", "not_empty"],
  time: ["ge", "le", "gt", "lt", "is", "contains", "empty", "not_empty"],
};
const TIME_OPS = {ge: "on or after", le: "on or before", gt: "after", lt: "before", is: "on day"};

async function api(body) {
  const res = await fetch("/api/custom-reports", {method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json", "X-NVR-Track": "custom-reports"}, body: JSON.stringify(body)});
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.error || ("Request failed (" + res.status + ")."));
  return data;
}
function ds(key) { return S.opts.datasets.find(d => d.key === key); }
function column(key, col) { return ds(key).columns.find(c => c.key === col); }
function iso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function day(text) { const [y, m, d] = text.split("-").map(Number); return new Date(y, m - 1, d); }
function shift(text, days) { const d = day(text); d.setDate(d.getDate() + days); return iso(d); }
function sec(key) {
  if (!S.sec[key]) {
    const d = ds(key);
    S.sec[key] = {cols: d.columns.filter(c => c.default).map(c => c.key), conds: [], sort: d.sort.column, desc: d.sort.desc, limit: "", open: true};
  }
  return S.sec[key];
}
function hasPeriod() { return S.sel.some(k => ds(k).mode === "range"); }
function hasDate() { return S.sel.some(k => ds(k).mode === "as_of"); }
function changed() { renderQuery(); }

// ---------- 1. data sets: click a card to add or remove it ----------
function renderDatasets() {
  const box = $("datasets");
  box.replaceChildren();
  for (const d of S.opts.datasets) {
    const at = S.sel.indexOf(d.key);
    const card = el("button", "ds");
    card.type = "button";
    card.setAttribute("aria-pressed", String(at >= 0));
    card.title = at >= 0 ? "Click to remove " + d.label : "Click to add " + d.label;
    const title = el("b");
    if (at >= 0) title.append(el("i", "order", at + 1));
    title.append(d.label);
    card.append(title, el("span", "", d.help), el("p", "tag", d.mode === "as_of" ? "on a date" : "period"));
    card.addEventListener("click", () => toggle(d.key));
    box.append(card);
  }
  $("ds-count").textContent = S.sel.length ? S.sel.length + " chosen" : "pick one or more";
}
function toggle(key) {
  const at = S.sel.indexOf(key);
  if (at >= 0) S.sel.splice(at, 1);
  else { S.sel.push(key); sec(key).open = true; }
  renderDatasets(); renderDates(); renderGroups(); renderQuery();
}

// ---------- 2. NVRs ----------
function renderNvrs() {
  const box = $("nvrs");
  box.replaceChildren();
  const all = el("label", "chip");
  const a = el("input"); a.type = "checkbox"; a.checked = S.nvrs.size === 0;
  a.addEventListener("change", () => { S.nvrs.clear(); renderNvrs(); changed(); });
  all.append(a, "All NVRs");
  box.append(all);
  for (const n of S.opts.nvrs) {
    const chip = el("label", "chip");
    const input = el("input"); input.type = "checkbox"; input.checked = S.nvrs.has(n.id);
    input.addEventListener("change", () => { input.checked ? S.nvrs.add(n.id) : S.nvrs.delete(n.id); renderNvrs(); changed(); });
    chip.title = n.name ? n.name + " (" + n.id + ")" : n.id;
    chip.append(input, n.id.replace(/^nvr-/i, "NVR "));
    if (n.name) chip.append(el("small", "", n.name.length > 14 ? n.name.slice(0, 13) + "…" : n.name));
    box.append(chip);
  }
  $("nvr-count").textContent = S.nvrs.size ? S.nvrs.size + " of " + S.opts.nvrs.length : "all " + S.opts.nvrs.length;
}

// ---------- 3. dates (shared; "on a date" data sets use the To date) ----------
function renderDates() {
  const single = S.sel.length > 0 && !hasPeriod();
  $("dates").classList.toggle("single", single);
  $("from-box").hidden = single;
  $("date-title").textContent = single ? "On date" : "Dates";
  $("to-label").textContent = single ? "State on" : "To";
  for (const id of ["date-from", "date-to"]) { $(id).max = S.opts.today; if (S.opts.oldest) $(id).min = S.opts.oldest; }
  $("oldest").textContent = S.opts.oldest ? "reports since " + S.opts.oldest : "";
  const mixed = hasPeriod() && hasDate();
  $("date-hint").hidden = !mixed;
  if (mixed) $("date-hint").textContent = S.sel.filter(k => ds(k).mode === "as_of").map(k => ds(k).label).join(" and ") + " show the state on the To date.";
  const t = S.opts.today, first = t.slice(0, 8) + "01", lastEnd = shift(first, -1), lastFirst = lastEnd.slice(0, 8) + "01";
  const choices = single
    ? [["Today", t, t], ["Yesterday", shift(t, -1), shift(t, -1)], ["End of last month", lastEnd, lastEnd], ["7 days ago", shift(t, -7), shift(t, -7)]]
    : [["Today", t, t], ["Yesterday", shift(t, -1), shift(t, -1)], ["Last 7 days", shift(t, -6), t], ["Last 30 days", shift(t, -29), t],
       ["This month", first, t], ["Last month", lastFirst, lastEnd]];
  const box = $("quick");
  box.replaceChildren();
  for (const [label, from, to] of choices) {
    const b = el("button", "", label); b.type = "button";
    b.addEventListener("click", () => { $("date-from").value = from; $("date-to").value = to; changed(); });
    box.append(b);
  }
}

// ---------- 4. one group per chosen data set ----------
function opLabel(kind, op) { return kind === "time" && TIME_OPS[op] ? TIME_OPS[op] : S.opts.ops[op]; }
function groupSummary(key) {
  const s = sec(key), n = s.conds.length;
  return s.cols.length + (s.cols.length === 1 ? " column" : " columns") + (n ? " · " + n + (n === 1 ? " condition" : " conditions") : "")
    + " · by " + column(key, s.sort).label + (s.desc ? " ↓" : " ↑") + (s.limit ? " · max " + s.limit : "");
}
function renderGroups() {
  const box = $("groups");
  box.replaceChildren();
  if (!S.sel.length) { box.append(el("p", "hint", "Pick one or more data sets in step 1; each gets its own columns, conditions and sort here.")); return; }
  S.sel.forEach((key, index) => {
    const d = ds(key), s = sec(key);
    const group = el("details", "group");
    group.open = s.open;
    group.addEventListener("toggle", () => { s.open = group.open; });
    const summary = el("summary");
    const sum = el("span", "gsum", groupSummary(key));
    summary.append(el("i", "order", index + 1), el("span", "gname", d.label), sum);
    group.append(summary);
    const body = el("div", "gbody");
    const refresh = () => { sum.textContent = groupSummary(key); changed(); };

    // Columns
    const ch = el("div", "sub-h"); ch.append(el("span", "", "Columns"));
    const links = el("span", "links");
    const def = el("button", "linkbtn", "Default"); def.type = "button";
    const all = el("button", "linkbtn", "All"); all.type = "button";
    links.append(def, all); ch.append(links);
    const chips = el("div", "chips");
    const drawChips = () => {
      chips.replaceChildren();
      for (const c of d.columns) {
        const chip = el("label", "chip");
        const input = el("input"); input.type = "checkbox"; input.checked = s.cols.includes(c.key);
        input.addEventListener("change", () => {
          const order = d.columns.map(x => x.key);
          s.cols = input.checked ? order.filter(k => k === c.key || s.cols.includes(k)) : s.cols.filter(k => k !== c.key);
          refresh();
        });
        chip.append(input, c.label);
        chips.append(chip);
      }
    };
    def.addEventListener("click", () => { s.cols = d.columns.filter(c => c.default).map(c => c.key); drawChips(); refresh(); });
    all.addEventListener("click", () => { s.cols = d.columns.map(c => c.key); drawChips(); refresh(); });
    drawChips();
    body.append(ch, chips);

    // Conditions
    body.append(el("div", "sub-h", "Only rows where"));
    const conds = el("div");
    const add = el("button", "linkbtn", "+ Add condition"); add.type = "button";
    const hint = el("p", "hint");
    const drawConds = () => {
      conds.replaceChildren();
      s.conds.forEach((cond, i) => conds.append(conditionRow(key, cond, () => { s.conds.splice(i, 1); drawConds(); refresh(); }, () => { drawConds(); refresh(); }, refresh)));
      add.hidden = s.conds.length >= S.opts.limits.max_conditions;
      hint.textContent = s.conds.length ? "All conditions must match (AND). A date alone means the whole day." : "No conditions: every row is included.";
    };
    add.addEventListener("click", () => {
      const first = d.columns.find(c => c.kind === "enum") || d.columns[0];
      s.conds.push({column: first.key, op: KIND_OPS[first.kind][0], value: ""});
      drawConds(); refresh();
    });
    drawConds();
    body.append(conds, add, hint);

    // Sort and limit
    body.append(el("div", "sub-h", "Sort and limit"));
    const row = el("div", "row2");
    const sortSel = el("select", "control"); sortSel.setAttribute("aria-label", d.label + ": sort by");
    for (const c of d.columns) { const o = el("option", "", c.label); o.value = c.key; o.selected = c.key === s.sort; sortSel.append(o); }
    const dirSel = el("select", "control"); dirSel.setAttribute("aria-label", d.label + ": sort direction");
    for (const [v, t] of [["0", "Ascending"], ["1", "Descending"]]) { const o = el("option", "", t); o.value = v; o.selected = (v === "1") === s.desc; dirSel.append(o); }
    const limit = el("input", "control"); limit.type = "number"; limit.min = "1"; limit.step = "1"; limit.placeholder = "All rows";
    limit.value = s.limit; limit.setAttribute("aria-label", d.label + ": row limit");
    sortSel.addEventListener("change", () => { s.sort = sortSel.value; refresh(); });
    dirSel.addEventListener("change", () => { s.desc = dirSel.value === "1"; refresh(); });
    limit.addEventListener("input", () => { s.limit = limit.value; refresh(); });
    row.append(sortSel, dirSel, limit);
    body.append(row);
    group.append(body);
    box.append(group);
  });
}
function conditionRow(key, cond, remove, redraw, refresh) {
  const row = el("div", "cond");
  const cols = ds(key).columns;
  const colSel = el("select", "control"); colSel.setAttribute("aria-label", "Column");
  for (const c of cols) { const o = el("option", "", c.label); o.value = c.key; o.selected = c.key === cond.column; colSel.append(o); }
  const kind = column(key, cond.column).kind;
  if (!KIND_OPS[kind].includes(cond.op)) cond.op = KIND_OPS[kind][0];
  const opSel = el("select", "control"); opSel.setAttribute("aria-label", "Comparison");
  for (const op of KIND_OPS[kind]) { const o = el("option", "", opLabel(kind, op)); o.value = op; o.selected = op === cond.op; opSel.append(o); }
  let value;
  const noValue = cond.op === "empty" || cond.op === "not_empty";
  if (kind === "enum" && !noValue) {
    value = el("select", "control");
    const values = column(key, cond.column).values;
    if (!values.includes(cond.value)) cond.value = values[0] || "";
    for (const v of values) { const o = el("option", "", v); o.value = v; o.selected = v === cond.value; value.append(o); }
  } else {
    value = el("input", "control");
    value.type = (kind === "number" || kind === "percent") ? "number" : "text";
    if (value.type === "number") value.step = "any";
    value.placeholder = noValue ? "—" : kind === "time" ? "2026-11-05" : kind === "percent" ? "e.g. 95" : "value";
    value.value = noValue ? "" : cond.value;
    value.disabled = noValue;
  }
  value.setAttribute("aria-label", "Value");
  const x = el("button", "x", "×"); x.type = "button"; x.title = "Remove condition"; x.setAttribute("aria-label", "Remove condition");
  colSel.addEventListener("change", () => { cond.column = colSel.value; cond.value = ""; redraw(); });
  opSel.addEventListener("change", () => { cond.op = opSel.value; redraw(); });
  value.addEventListener("input", () => { cond.value = value.value; refresh(); });
  value.addEventListener("change", () => { cond.value = value.value; refresh(); });
  x.addEventListener("click", remove);
  row.append(colSel, opSel, value, x);
  return row;
}

// ---------- the query ----------
function spec() {
  return {nvrs: [...S.nvrs].sort(), date_from: hasPeriod() ? $("date-from").value : $("date-to").value, date_to: $("date-to").value,
          sections: S.sel.map(key => {
            const s = sec(key), limit = parseInt(s.limit, 10);
            return {dataset: key, columns: [...s.cols], conditions: s.conds.map(c => ({...c})),
                    sort: {column: s.sort, desc: s.desc}, limit: Number.isFinite(limit) && limit > 0 ? limit : null};
          })};
}
function renderQuery() {
  const q = spec(), pre = $("query");
  pre.replaceChildren();
  const k = t => pre.append(el("span", "k", t));
  const str = t => pre.append(el("span", "s", t));
  const t = x => pre.append(document.createTextNode(x));
  if (!q.sections.length) { t("-- Pick what to report on in step 1."); }
  q.sections.forEach((s, i) => {
    const d = ds(s.dataset);
    if (i) t(";\n\n");
    t("-- " + (i + 1) + ". " + d.label + "\n");
    k("SELECT "); t(s.columns.length ? s.columns.map(key => column(s.dataset, key).label).join(", ") : "(no columns)");
    t("\n"); k("FROM "); t(d.label);
    t("\n"); if (d.mode === "as_of") { k("ON "); str(q.date_to || "?"); } else { k("FROM "); str(q.date_from || "?"); k(" TO "); str(q.date_to || "?"); }
    const where = [];
    if (q.nvrs.length) where.push(["NVR", "IN", "(" + q.nvrs.join(", ") + ")"]);
    for (const c of s.conditions) {
      const col = column(s.dataset, c.column);
      const plain = col.kind === "number" || col.kind === "percent";
      where.push([col.label, opLabel(col.kind, c.op), (c.op === "empty" || c.op === "not_empty") ? "" : plain ? c.value : '"' + c.value + '"']);
    }
    where.forEach((w, j) => { t("\n"); k(j ? "  AND " : "WHERE "); t(w[0] + " " + w[1] + " "); str(w[2]); });
    t("\n"); k("ORDER BY "); t(column(s.dataset, s.sort.column).label + " " + (s.sort.desc ? "DESC" : "ASC"));
    if (s.limit) { t("\n"); k("LIMIT "); t(String(s.limit)); }
  });
  $("run").disabled = S.busy || !q.sections.length;
  if (S.result && JSON.stringify(q) !== S.ranSpec) $("preview-meta").textContent = "The query changed — run it again to update the preview.";
}

// ---------- results: one tab per data set ----------
const BAD = new Set(["Offline", "Stopped recording", "No footage 30+ days", "Critical", "No footage in window", "Yes", "error", "smartfailed", "offline", "unformatted"]);
const WARN = new Set(["Warning", "Check failed", "Gaps", "Not verified", "Unknown", "Picture problem", "Below required days", "Offline, still recording"]);
const GOOD = new Set(["Online", "OK", "Recorded", "Healthy", "No", "ok"]);
function renderCell(td, kind, value) {
  if (value === null || value === undefined || value === "") { td.textContent = "—"; td.className = "blank"; return; }
  if (kind === "number" || kind === "percent") td.className = "num";
  if (kind === "percent") { td.textContent = Number(value).toFixed(1) + "%"; return; }
  if (kind === "enum") {
    const cls = BAD.has(value) ? "bad" : WARN.has(value) ? "warn" : GOOD.has(value) ? "good" : "";
    td.append(el("span", "pill " + cls, value)); return;
  }
  td.textContent = String(value);
  if (String(value).length > 40) td.title = String(value);
}
function renderResult() {
  const box = $("result");
  box.replaceChildren();
  if (!S.result) return;
  const sections = S.result;
  if (!sections.some(x => x.dataset === S.tab)) S.tab = sections[0].dataset;
  const total = sections.reduce((n, x) => n + x.total, 0);
  $("preview-meta").textContent = sections.length + (sections.length === 1 ? " data set · " : " data sets · ") + total + (total === 1 ? " row" : " rows");
  const tabs = el("div", "tabs"); tabs.setAttribute("role", "tablist");
  sections.forEach((x, i) => {
    const tab = el("button", "tab"); tab.type = "button"; tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(x.dataset === S.tab));
    tab.append(el("i", "order", i + 1), x.label, el("span", "c", x.total));
    tab.addEventListener("click", () => { S.tab = x.dataset; renderResult(); });
    tabs.append(tab);
  });
  box.append(tabs);
  const r = sections.find(x => x.dataset === S.tab);
  const meta = [r.total + (r.total === 1 ? " row" : " rows") + (r.truncated ? " (limited)" : "")];
  if (r.rows.length < r.total && !r.truncated) meta.push("showing first " + r.rows.length);
  meta.push(r.mode === "as_of" ? "on " + r.date_to
    : r.meta && r.meta.first ? r.meta.first + " → " + r.meta.last : r.date_from + " → " + r.date_to);
  if (r.meta && r.meta.source) meta.push(r.meta.source);
  box.append(el("p", "secmeta", meta.join(" · ")));
  if (!r.total) {
    const e = el("div", "empty"); e.append(el("b", "", "No rows match"), "Try more NVRs, other dates or fewer conditions for " + r.label + "."); box.append(e); return;
  }
  const s = sec(r.dataset);
  const wrap = el("div", "tablewrap"), table = el("table"), thead = el("thead"), tr = el("tr");
  for (const c of r.columns) {
    const th = el("th", (c.kind === "number" || c.kind === "percent") ? "num" : "", c.label);
    th.scope = "col"; th.title = "Sort " + r.label + " by " + c.label;
    if (c.key === s.sort) th.append(el("span", "dir", s.desc ? "▼" : "▲"));
    th.addEventListener("click", () => {
      if (s.sort === c.key) s.desc = !s.desc;
      else { s.sort = c.key; s.desc = c.kind === "number" || c.kind === "percent" || c.kind === "time"; }
      renderGroups(); renderQuery(); run();
    });
    tr.append(th);
  }
  thead.append(tr);
  const tbody = el("tbody");
  for (const row of r.rows) {
    const line = el("tr");
    r.columns.forEach((c, i) => { const td = el("td"); renderCell(td, c.kind, row[i]); line.append(td); });
    tbody.append(line);
  }
  table.append(thead, tbody); wrap.append(table); box.append(wrap);
}
function say(text, kind) { const m = $("msg"); m.textContent = text || ""; m.className = "msg" + (kind ? " " + kind : ""); }
function busy(button, on, label) {
  S.busy = on; button.disabled = on;
  if (on) { button.dataset.label = button.innerHTML; button.replaceChildren(el("span", "spinner"), label); }
  else if (button.dataset.label) { button.innerHTML = button.dataset.label; }
}
async function run() {
  if (S.busy || !S.sel.length) return;
  say("");
  const q = spec();
  busy($("run"), true, "Running…");
  $("preview-meta").textContent = "Reading saved reports… (a long period takes a while the first time)";
  try {
    const r = await api({action: "preview", spec: q});
    S.result = r.sections; S.ranSpec = JSON.stringify(q);
    renderResult();
    if (!$("save-name").value) $("save-name").placeholder = suggestName();
  } catch (e) { say(e.message, "err"); $("preview-meta").textContent = "Not run."; }
  finally { busy($("run"), false); renderQuery(); }
}
function suggestName() {
  const q = spec();
  return S.sel.map(k => ds(k).label).join(" + ") + (q.nvrs.length ? " – " + q.nvrs.join(", ") : "")
    + " – " + (hasPeriod() ? q.date_from + " to " + q.date_to : q.date_to);
}
async function save() {
  if (S.busy || !S.sel.length) return;
  const name = $("save-name").value.trim() || suggestName();
  busy($("save"), true, "Saving…");
  say("");
  try {
    const r = await api({action: "save", name, spec: spec()});
    renderSaved(r.saved);
    say("Saved “" + r.item.name + "” (" + r.item.rows + " rows). Download it any time from Saved reports.", "ok");
    $("save-name").value = "";
  } catch (e) { say(e.message, "err"); }
  finally { busy($("save"), false); }
}

// ---------- saved reports ----------
function stampText(value) {
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? "" : d.toLocaleString("en-GB", {timeZone: "Asia/Phnom_Penh", day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false});
}
function renderSaved(list) {
  S.saved = list || [];
  const box = $("saved");
  box.replaceChildren();
  $("saved-meta").textContent = S.saved.length ? S.saved.length + " on the server" : "";
  if (!S.saved.length) { const li = el("li"); li.append(el("div", "empty", "No saved reports yet. Run a query, give it a name and save it.")); box.append(li); return; }
  for (const item of S.saved) {
    const li = el("li"), info = el("div");
    info.style.minWidth = "0";
    const labels = item.labels || [item.dataset_label || ""];
    const modes = item.modes || [item.mode];
    const period = modes.includes("range");
    const when = period ? item.date_from + " → " + item.date_to : "on " + item.date_to;
    info.append(el("div", "name", item.name),
      el("div", "sub", labels.join(" + ") + " · " + when + " · " + item.rows + (item.rows === 1 ? " row" : " rows") + " · saved " + stampText(item.created_at)));
    for (const text of item.queries || [item.query || ""]) { const q = el("div", "q", text); q.title = text; info.append(q); }
    const acts = el("div", "acts");
    const many = labels.length > 1;
    for (const fmt of ["csv", "pdf"]) {
      const a = el("a", "btn sm", fmt === "csv" ? (many ? "CSV (ZIP)" : "CSV") : "PDF");
      a.href = "/api/custom-reports/download?id=" + encodeURIComponent(item.id) + "&format=" + fmt;
      a.title = fmt === "csv"
        ? (many ? "ZIP with one CSV per data set, for Excel (all rows, any language)" : "Download for Excel (all rows, any language)")
        : "One PDF with a section per data set (Latin letters; first " + S.opts.limits.pdf_rows + " rows each)";
      acts.append(a);
    }
    const open = el("button", "btn sm", "Use query"); open.type = "button"; open.title = "Load this query into the builder to run it again or change it";
    open.addEventListener("click", () => loadSpec(item.spec));
    const del = el("button", "btn sm danger", "Delete"); del.type = "button";
    del.addEventListener("click", () => {
      if (del.dataset.armed) { remove(item.id); return; }
      del.dataset.armed = "1"; del.textContent = "Delete — sure?";
      setTimeout(() => { if (del.isConnected) { delete del.dataset.armed; del.textContent = "Delete"; } }, 4000);
    });
    acts.append(open, del);
    li.append(info, acts);
    box.append(li);
  }
}
async function remove(id) {
  try { const r = await api({action: "delete", id}); renderSaved(r.saved); say("Deleted.", "ok"); }
  catch (e) { say(e.message, "err"); }
}
function loadSpec(q) {
  if (!q) return;
  const sections = q.sections || (q.dataset ? [q] : []);   // Reports saved before data sets could be combined.
  S.sel = []; S.sec = {};
  for (const x of sections) {
    if (!ds(x.dataset) || S.sel.includes(x.dataset)) continue;
    S.sel.push(x.dataset);
    const d = ds(x.dataset);
    S.sec[x.dataset] = {cols: [...(x.columns || d.columns.filter(c => c.default).map(c => c.key))],
                        conds: (x.conditions || []).map(c => ({...c})), sort: (x.sort || {}).column || d.sort.column,
                        desc: x.sort ? Boolean(x.sort.desc) : d.sort.desc, limit: x.limit || "", open: true};
  }
  S.nvrs = new Set(q.nvrs || []);
  $("date-from").value = q.date_from || S.opts.today; $("date-to").value = q.date_to || S.opts.today;
  renderDatasets(); renderNvrs(); renderDates(); renderGroups(); renderQuery();
  window.scrollTo({top: 0, behavior: "smooth"});
  run();
}

// ---------- start ----------
$("builder").addEventListener("submit", e => { e.preventDefault(); run(); });
$("save").addEventListener("click", save);
$("save-name").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); save(); } });
for (const id of ["date-from", "date-to"]) $(id).addEventListener("input", changed);
$("reset").addEventListener("click", () => {
  S.nvrs.clear(); S.sel = [S.opts.datasets[0].key]; S.sec = {};
  $("date-to").value = S.opts.today; $("date-from").value = shift(S.opts.today, -6);
  renderDatasets(); renderNvrs(); renderDates(); renderGroups(); renderQuery();
});

const THEMES = ["auto", "light", "dark"];
function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  $("theme").title = "Theme: " + (mode === "auto" ? "follow system" : mode);
}
let theme = "auto";
try { theme = THEMES.includes(localStorage.getItem("nvrtrack.theme")) ? localStorage.getItem("nvrtrack.theme") : "auto"; } catch (e) {}
applyTheme(theme);
$("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  try { localStorage.setItem("nvrtrack.theme", theme); } catch (e) {}
});

(async function start() {
  try {
    const res = await fetch("/api/custom-reports", {credentials: "same-origin"});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not load.");
    S.opts = data;
    S.sel = [data.datasets[0].key];
    $("date-to").value = data.today; $("date-from").value = shift(data.today, -6);
    renderDatasets(); renderNvrs(); renderDates(); renderGroups(); renderQuery(); renderSaved(data.saved);
  } catch (e) {
    $("result").replaceChildren(el("div", "empty", "The report builder could not load: " + e.message));
  }
})();
