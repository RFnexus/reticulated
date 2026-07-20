"use strict";

const FAULT_ACTIONS = new Set(["restart", "shutdown", "start", "disconnect", "reconnect", "partition", "recover"]);
const state = { reports: [], selected: 0, filter: "all", composition: "all", direction: "all" };

const byId = (id) => document.getElementById(id);
const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
const pct = (passed, total) => total ? (100 * passed / total) : 0;
const cap = (value) => String(value || "unknown").replace(/(^|[-_ ])([a-z])/g, (_, gap, char) => gap + char.toUpperCase());

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function measuredRtt(result) {
  if (Number.isFinite(Number(result.measured_latency_seconds))) return Number(result.measured_latency_seconds);
  if (Number.isFinite(Number(result.measured_rtt_seconds))) return Number(result.measured_rtt_seconds);
  if (result.performance && Number.isFinite(Number(result.performance.p50_seconds))) return Number(result.performance.p50_seconds);
  const evidence = [...(result.delivery_evidence || [])];
  for (const event of result.events || []) if (event && event.status) evidence.push(event.status);
  for (let index = evidence.length - 1; index >= 0; index -= 1) {
    const match = String(evidence[index]).match(/\bDELIVERED\b.*?\brtt=([0-9]+(?:\.[0-9]+)?)\b/);
    if (match) return Number(match[1]);
  }
  return null;
}

function aggregateMedia(media) {
  const total = { count: 0, tx: 0, rx: 0, dropped: 0, oversize: 0 };
  for (const value of Object.values(media || {})) {
    total.count += 1;
    const stats = value.stats || {};
    for (const key of ["tx", "rx", "dropped", "oversize"]) total[key] += number(stats[key]);
  }
  return total;
}

function mediaProfiles(report) {
  if (report.network && Array.isArray(report.network.medium_profiles)) return report.network.medium_profiles;
  const groups = new Map();
  for (const medium of Object.values(report.media || {})) {
    const p = medium.params || {};
    const key = [number(p.bitrate), number(p.mtu), number(p.propagation), number(p.loss)].join("|");
    if (!groups.has(key)) groups.set(key, { count: 0, bitrate: number(p.bitrate), mtu: number(p.mtu), propagation: number(p.propagation), loss: number(p.loss) });
    groups.get(key).count += 1;
  }
  return [...groups.values()];
}

function normalize(report, index) {
  const reportType = report.report_type || "scenario";
  let results = Array.isArray(report.results) ? report.results : [];
  if (reportType === "benchmark" && Array.isArray(report.cells)) {
    results = report.cells.flatMap((cell) => (cell.results || []).map((result) => ({
      ...result, composition: cell.composition, run: cell.run, cell_nodes: cell.nodes,
    })));
  } else if (reportType === "conformance" && Array.isArray(report.cases)) {
    results = report.cases.map((item, caseIndex) => ({
      ...item, action: "conformance", action_index: caseIndex + 1,
      target: item.case_id, duration_seconds: item.duration_seconds,
    }));
  }
  const transfers = results.filter((result) => result.transfer);
  const faults = results.filter((result) => FAULT_ACTIONS.has(result.action));
  const actionPass = results.filter((result) => result.status === "pass").length;
  const transferPass = transfers.filter((result) => result.status === "pass").length;
  const faultPass = faults.filter((result) => result.status === "pass").length;
  const media = aggregateMedia(report.media);
  const supplied = report.summary || {};
  const implementationCounts = (report.network && report.network.implementation_counts) || report.implementation_counts || null;
  return {
    raw: report,
    reportType,
    index,
    name: report.scenario || report.source_file || `Scenario ${index + 1}`,
    source: report.source_file || "embedded report",
    status: String(report.status || "unknown").toLowerCase(),
    nodes: number(report.nodes),
    duration: number(report.duration_seconds),
    generatedAt: report.generated_at || null,
    results,
    transfers,
    faults,
    implementationCounts,
    implementations: report.implementations || (implementationCounts ? Object.keys(implementationCounts) : []),
    profiles: mediaProfiles(report),
    comparisons: Array.isArray(report.comparisons) ? report.comparisons : [],
    metrics: {
      actions: { total: results.length, passed: actionPass, success: pct(actionPass, results.length) },
      transfers: { total: transfers.length, passed: transferPass, success: pct(transferPass, transfers.length) },
      faults: { total: faults.length, passed: faultPass, success: pct(faultPass, faults.length) },
      maxHops: Math.max(0, ...transfers.map((result) => number(result.hops))),
      media: { ...media, ...(supplied.media || {}) },
    },
  };
}

function statusBadge(status) {
  return element("span", `status ${status}`, status);
}

function ratioCell(passed, total) {
  const wrap = element("div", "metric-cell");
  wrap.append(`${passed}/${total}`);
  const bar = element("span", "microbar");
  const fill = element("i");
  fill.style.width = `${pct(passed, total)}%`;
  bar.append(fill);
  wrap.append(bar);
  return wrap;
}

function formatDuration(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${Number(seconds).toFixed(seconds < 10 ? 2 : 1)} s`;
  const mins = Math.floor(seconds / 60);
  return `${mins}m ${Math.round(seconds % 60)}s`;
}

function formatRtt(seconds) {
  if (seconds === null || !Number.isFinite(Number(seconds))) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  return `${seconds.toFixed(seconds < 10 ? 3 : 1)} s`;
}

function formatBitrate(bps) {
  if (bps >= 1000000) return `${(bps / 1000000).toFixed(bps % 1000000 ? 1 : 0)} Mbps`;
  if (bps >= 1000) return `${(bps / 1000).toFixed(bps % 1000 ? 1 : 0)} kbps`;
  return `${bps} bps`;
}

function toast(message, isError = false) {
  const node = byId("toast");
  node.textContent = message;
  node.className = `show${isError ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = ""; }, 2800);
}

function renderKpis(report) {
  const conformance = report.raw.summary && report.raw.summary.conformance;
  const sampleCount = report.comparisons.reduce((total, row) => total + number(row.sample_count), 0);
  const values = [
    ["Result", report.status.toUpperCase(), report.status === "pass" ? "All required actions passed" : "Review failing actions", report.status],
    ["Nodes", report.nodes.toLocaleString(), report.implementationCounts ? `${Object.entries(report.implementationCounts).map(([key, value]) => `${value} ${cap(key)}`).join(" · ")}` : report.implementations.map(cap).join(" + "), ""],
    [conformance ? "Canonical cases" : "Actions", `${report.metrics.actions.passed}/${report.metrics.actions.total}`, conformance ? `${number(conformance.skipped)} explicit skips` : `${report.metrics.actions.success.toFixed(1)}% successful`, ""],
    ["Transfers", `${report.metrics.transfers.passed}/${report.metrics.transfers.total}`, `${report.metrics.transfers.success.toFixed(1)}% delivered`, ""],
    [sampleCount ? "Samples" : "Fault recovery", sampleCount ? sampleCount.toLocaleString() : `${report.metrics.faults.passed}/${report.metrics.faults.total}`, sampleCount ? "correlated receiver observations" : (report.metrics.faults.total ? `${report.metrics.faults.success.toFixed(1)}% successful` : "No injected faults"), ""],
    ["Route depth", `${report.metrics.maxHops}`, "maximum tested hops", ""],
    ["Run time", formatDuration(report.duration), "end-to-end scenario elapsed", ""],
  ];
  const grid = byId("kpi-grid");
  grid.replaceChildren();
  for (const [label, value, note, status] of values) {
    const card = element("article", `kpi ${status}`);
    card.append(element("div", "kpi-label", label), element("div", "kpi-value", value), element("div", "kpi-note", note || "—"));
    grid.append(card);
  }
}

function renderComparison() {
  const body = byId("comparison-table").querySelector("tbody");
  body.replaceChildren();
  state.reports.forEach((report, index) => {
    const row = element("tr", `clickable${index === state.selected ? " selected" : ""}`);
    const name = element("td", "", report.name);
    name.title = report.source;
    const cells = [name, element("td"), element("td", "", report.nodes.toLocaleString()), element("td"), element("td"), element("td"), element("td", "", String(report.metrics.maxHops)), element("td", "", formatDuration(report.duration))];
    cells[1].append(statusBadge(report.status));
    cells[3].append(ratioCell(report.metrics.actions.passed, report.metrics.actions.total));
    cells[4].append(ratioCell(report.metrics.transfers.passed, report.metrics.transfers.total));
    cells[5].append(ratioCell(report.metrics.faults.passed, report.metrics.faults.total));
    row.append(...cells);
    row.addEventListener("click", () => selectReport(index));
    body.append(row);
  });
  byId("report-count").textContent = `${state.reports.length} report${state.reports.length === 1 ? "" : "s"}`;
}

function renderInterop(report) {
  const labels = ["python", "rust"];
  const matrix = byId("interop-matrix");
  matrix.replaceChildren(element("div", "matrix-cell matrix-head", "From \\ To"));
  labels.forEach((label) => matrix.append(element("div", "matrix-cell matrix-head", cap(label))));
  for (const source of labels) {
    matrix.append(element("div", "matrix-cell matrix-head", cap(source)));
    for (const destination of labels) {
      const matches = report.transfers.filter((item) => item.source_implementation === source && item.destination_implementation === destination);
      const passed = matches.filter((item) => item.status === "pass").length;
      const cell = element("div", `matrix-cell ${matches.length ? (passed === matches.length ? "covered" : "failed") : ""}`);
      cell.append(element("strong", "", matches.length ? `${passed}/${matches.length}` : "—"), element("span", "", matches.length ? "delivered" : "not exercised"));
      matrix.append(cell);
    }
  }
  const mix = byId("implementation-mix");
  mix.replaceChildren();
  if (report.implementationCounts) {
    const max = Math.max(1, ...Object.values(report.implementationCounts).map(number));
    for (const [name, count] of Object.entries(report.implementationCounts)) {
      const row = element("div", "mix-row");
      row.append(element("span", "", cap(name)));
      const track = element("div", "mix-track");
      const fill = element("i", labels.includes(name) ? name : "other");
      fill.style.width = `${100 * count / max}%`;
      track.append(fill);
      row.append(track, element("strong", "", String(count)));
      mix.append(row);
    }
  } else {
    mix.append(element("p", "panel-note", `Implementations present: ${report.implementations.map(cap).join(", ") || "not recorded"}. Exact node counts were not recorded by this legacy report.`));
  }
}

function transferLabel(result) {
  const index = result.action_index ? `#${result.action_index} · ` : "";
  return `${index}${cap(result.source_implementation || "?")} → ${cap(result.destination_implementation || "?")} · ${cap(result.transfer)}`;
}

function renderLatency(report) {
  const chart = byId("latency-chart");
  chart.replaceChildren();
  const measured = report.transfers.map((result) => ({ result, rtt: measuredRtt(result) })).filter((item) => item.rtt !== null);
  if (!measured.length) {
    chart.append(element("div", "no-data", "No measured DELIVERED RTT evidence in this report."));
  } else {
    const max = Math.max(...measured.map((item) => item.rtt), .001);
    for (const item of measured) {
      const row = element("div", "bar-row");
      const label = element("div", "bar-label", transferLabel(item.result));
      label.title = `${item.result.source || "?"} → ${item.result.destination || "?"}`;
      const track = element("div", "bar-track");
      const fill = element("i");
      fill.style.width = `${Math.max(1.5, 100 * item.rtt / max)}%`;
      track.append(fill);
      row.append(label, track, element("div", "bar-value", formatRtt(item.rtt)));
      chart.append(row);
    }
  }
  const missing = report.transfers.length - measured.length;
  byId("latency-note").textContent = missing ? `${measured.length} measured · ${missing} delivery result${missing === 1 ? "" : "s"} without sender RTT. Modeled EXPECT values are intentionally excluded.` : `${measured.length} measured delivery result${measured.length === 1 ? "" : "s"}.`;
}

function renderMedium(report) {
  const root = byId("medium-counters");
  root.replaceChildren();
  const m = report.metrics.media;
  for (const [label, value] of [["Media", m.count], ["TX", m.tx], ["RX", m.rx], ["Dropped", m.dropped], ["Oversize", m.oversize]]) {
    const card = element("div", "counter");
    card.append(element("strong", "", number(value).toLocaleString()), element("span", "", label));
    root.append(card);
  }
  const profiles = byId("medium-profiles");
  profiles.replaceChildren();
  for (const profile of report.profiles.slice(0, 8)) {
    const text = `${profile.count}× ${formatBitrate(number(profile.bitrate))} · ${number(profile.mtu)} B MTU · ${number(profile.propagation) * 1000} ms · ${(number(profile.loss) * 100).toFixed(1)}% loss`;
    const pill = element("div", "profile");
    pill.append(element("strong", "", "Profile "), text);
    profiles.append(pill);
  }
}

function actionTarget(result) {
  if (result.target) return result.target;
  if (result.transfer) return `${result.source || "?"} → ${result.destination || "?"}`;
  if (result.node && result.medium) return `${result.node} @ ${result.medium}`;
  if (result.node) return result.node;
  if (result.medium) return result.medium;
  if (Array.isArray(result.nodes)) return `${result.nodes.length} nodes`;
  if (result.action === "wait_ready") return `${number(result.running)}/${number(state.reports[state.selected].nodes)} running`;
  return "—";
}

function actionIncluded(result) {
  if (state.composition !== "all" && result.composition !== state.composition) return false;
  const direction = `${result.source_implementation || "?"}->${result.destination_implementation || "?"}`;
  if (state.direction !== "all" && direction !== state.direction) return false;
  if (state.filter === "all") return true;
  if (state.filter === "transfer") return Boolean(result.transfer);
  if (state.filter === "fault") return FAULT_ACTIONS.has(result.action);
  if (state.filter === "fail") return result.status !== "pass";
  return true;
}

function resourceGroups(report) {
  const usages = report.reportType === "benchmark"
    ? (report.raw.cells || []).map((cell) => cell.resource_usage)
    : [report.raw.resource_usage];
  const groups = {};
  for (const usage of usages) {
    for (const [name, value] of Object.entries((usage && usage.groups) || {})) {
      if (!groups[name]) groups[name] = { cpu_seconds: 0, peak_rss_bytes: 0 };
      groups[name].cpu_seconds += number(value.cpu_seconds);
      groups[name].peak_rss_bytes = Math.max(groups[name].peak_rss_bytes, number(value.peak_rss_bytes));
    }
  }
  return groups;
}

function renderLabEvidence(report) {
  const root = byId("lab-evidence");
  root.replaceChildren();
  const cards = [];
  const conformance = report.raw.summary && report.raw.summary.conformance;
  if (conformance) cards.push(["Conformance coverage", `${number(conformance.passed)}/${number(conformance.total)}`, `${number(conformance.failed)} failed · ${number(conformance.skipped)} skipped`]);
  const byComposition = new Map();
  for (const row of report.comparisons) {
    if (!byComposition.has(row.composition)) byComposition.set(row.composition, []);
    if (Number.isFinite(Number(row.goodput_bytes_per_second))) byComposition.get(row.composition).push(Number(row.goodput_bytes_per_second));
  }
  for (const [composition, values] of byComposition) {
    if (!values.length) continue;
    const average = values.reduce((sum, value) => sum + value, 0) / values.length;
    cards.push([`${cap(composition)} goodput`, `${(average / 1024).toFixed(1)} KiB/s`, `${values.length} matched result rows`]);
  }
  for (const [group, value] of Object.entries(resourceGroups(report))) {
    cards.push([`${cap(group)} resources`, `${(value.peak_rss_bytes / 1048576).toFixed(1)} MiB`, `${value.cpu_seconds.toFixed(2)} CPU seconds · peak RSS`]);
  }
  if (report.raw.baseline) {
    const baseline = report.raw.baseline;
    cards.push(["Baseline thresholds", baseline.applied ? cap(baseline.status) : "Informational", baseline.applied ? `${(baseline.failures || []).length} threshold failures` : "No compatible baseline and thresholds supplied"]);
  }
  if (!cards.length) cards.push(["Legacy report", `Schema ${report.raw.schema_version || 1}`, "No schema v3 lab evidence recorded"]);
  for (const [label, value, note] of cards) {
    const card = element("div", "evidence-card");
    card.append(element("span", "", label), element("strong", "", value), element("small", "", note));
    root.append(card);
  }
}

function renderLabFilters(report) {
  const composition = byId("composition-filter");
  const direction = byId("direction-filter");
  const compositions = [...new Set(report.results.map((item) => item.composition).filter(Boolean))].sort();
  const directions = [...new Set(report.transfers.map((item) => `${item.source_implementation || "?"}->${item.destination_implementation || "?"}`))].sort();
  composition.replaceChildren(new Option("All compositions", "all"), ...compositions.map((value) => new Option(cap(value), value)));
  direction.replaceChildren(new Option("All directions", "all"), ...directions.map((value) => new Option(value.replace("->", " → "), value)));
  state.composition = compositions.includes(state.composition) ? state.composition : "all";
  state.direction = directions.includes(state.direction) ? state.direction : "all";
  composition.value = state.composition;
  direction.value = state.direction;
}

function renderActions(report) {
  const body = byId("actions-table").querySelector("tbody");
  body.replaceChildren();
  report.results.forEach((result, index) => {
    if (!actionIncluded(result)) return;
    const row = element("tr", "clickable");
    const impl = result.transfer ? `${cap(result.source_implementation || "?")} → ${cap(result.destination_implementation || "?")}` : "—";
    const transfer = result.transfer ? `${cap(result.transfer)} · ${number(result.bytes).toLocaleString()} B` : "—";
    const values = [
      String(result.action_index || index + 1), cap(result.action), null, actionTarget(result), impl, transfer,
      result.transfer ? String(number(result.hops)) : "—", result.transfer ? String(number(result.attempts)) : "—",
      result.transfer ? formatRtt(measuredRtt(result)) : "—", result.duration_seconds === undefined ? "—" : formatDuration(number(result.duration_seconds)),
    ];
    values.forEach((value, cellIndex) => {
      const cell = element("td", "", value === null ? undefined : value);
      if (cellIndex === 2) cell.append(statusBadge(result.status));
      row.append(cell);
    });
    row.addEventListener("click", () => showDetails(result, index));
    body.append(row);
  });
}

function showDetails(result, index) {
  byId("detail-title").textContent = `#${result.action_index || index + 1} ${cap(result.action)}`;
  byId("detail-content").textContent = JSON.stringify(result, null, 2);
  byId("detail-dialog").showModal();
}

function renderSelected() {
  const report = state.reports[state.selected];
  if (!report) return;
  byId("report-select").value = String(state.selected);
  const details = [`<strong>${report.nodes.toLocaleString()}</strong> nodes`, `<strong>${report.metrics.media.count}</strong> media`, report.source];
  byId("selected-meta").textContent = "";
  details.forEach((detail, index) => {
    if (index) byId("selected-meta").append(" · ");
    const stripped = detail.replace(/<\/?strong>/g, "");
    byId("selected-meta").append(stripped);
  });
  renderKpis(report);
  renderComparison();
  renderInterop(report);
  renderLatency(report);
  renderMedium(report);
  renderLabEvidence(report);
  renderLabFilters(report);
  renderActions(report);
}

function selectReport(index) {
  state.selected = Math.max(0, Math.min(index, state.reports.length - 1));
  renderSelected();
}

function renderAll() {
  const hasReports = state.reports.length > 0;
  byId("dashboard").hidden = !hasReports;
  byId("empty-state").hidden = hasReports;
  byId("clear-reports").disabled = !hasReports;
  const select = byId("report-select");
  select.replaceChildren();
  state.reports.forEach((report, index) => {
    const option = element("option", "", `${report.name} · ${report.status.toUpperCase()}`);
    option.value = String(index);
    select.append(option);
  });
  if (hasReports) renderSelected();
}

async function addFiles(files) {
  let loaded = 0;
  for (const file of files) {
    try {
      const parsed = JSON.parse(await file.text());
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error("report root must be an object");
      parsed.source_file = parsed.source_file || file.name;
      state.reports.push(normalize(parsed, state.reports.length));
      loaded += 1;
    } catch (error) {
      toast(`${file.name}: ${error.message}`, true);
    }
  }
  if (loaded) {
    state.selected = state.reports.length - loaded;
    renderAll();
    toast(`Loaded ${loaded} report${loaded === 1 ? "" : "s"}`);
  }
}

function initialize() {
  state.reports = (Array.isArray(EMBEDDED_REPORTS) ? EMBEDDED_REPORTS : []).map(normalize);
  byId("report-files").addEventListener("change", (event) => { addFiles(event.target.files); event.target.value = ""; });
  byId("report-select").addEventListener("change", (event) => selectReport(number(event.target.value)));
  byId("clear-reports").addEventListener("click", () => { state.reports = []; state.selected = 0; renderAll(); });
  byId("detail-close").addEventListener("click", () => byId("detail-dialog").close());
  byId("detail-dialog").addEventListener("click", (event) => { if (event.target === byId("detail-dialog")) byId("detail-dialog").close(); });
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
    if (state.reports[state.selected]) renderActions(state.reports[state.selected]);
  }));
  byId("composition-filter").addEventListener("change", (event) => { state.composition = event.target.value; renderActions(state.reports[state.selected]); });
  byId("direction-filter").addEventListener("change", (event) => { state.direction = event.target.value; renderActions(state.reports[state.selected]); });
  const drop = byId("drop-zone");
  drop.addEventListener("click", () => byId("report-files").click());
  drop.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") byId("report-files").click(); });
  for (const eventName of ["dragenter", "dragover"]) drop.addEventListener(eventName, (event) => { event.preventDefault(); drop.classList.add("dragging"); });
  for (const eventName of ["dragleave", "drop"]) drop.addEventListener(eventName, (event) => { event.preventDefault(); drop.classList.remove("dragging"); });
  drop.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  renderAll();
}

initialize();
