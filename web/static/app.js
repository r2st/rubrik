// Transcript Intelligence — frontend SPA
// Vanilla JS + Plotly.js. No build step. All charts/tables driven by /api endpoints.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const fmt = (v, d = 2) => v == null ? "—" : Number(v).toFixed(d);

const COLORS = { external: "#2196F3", internal: "#4CAF50", support: "#FF9800" };
const PLOTLY_BASE = {
  font: { family: "-apple-system, BlinkMacSystemFont, sans-serif", size: 12 },
  margin: { l: 50, r: 16, t: 20, b: 40 },
  paper_bgcolor: "white",
  plot_bgcolor: "white",
};
const PLOTLY_CFG = { displayModeBar: false, responsive: true };

// API base. Versioned so future breaking changes can ship under /api/v2.
const API_BASE = "/api/v1";

async function api(path) {
  // Allow callers to pass either "/summary" or "/api/v1/summary" — normalize.
  const url = path.startsWith("/api/") ? path : API_BASE + path;
  const headers = {};
  // X-API-Key is read from a global if the page sets one; harmless if absent.
  if (window.API_KEY) headers["X-API-Key"] = window.API_KEY;
  const r = await fetch(url, { headers });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const body = await r.json();
      if (body?.error?.message) msg = body.error.message;
    } catch (_) { /* not json */ }
    throw new Error(`${url}: ${msg}`);
  }
  return r.json();
}

// -------- Tabs --------
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  $$(".panel").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $(`#panel-${t.dataset.tab}`).classList.add("active");
  // Plotly needs a resize after the panel becomes visible
  window.dispatchEvent(new Event("resize"));
}));

// -------- KPIs + meta --------
async function loadSummary() {
  const s = await api("/summary");
  $("#meta").textContent = `${s.n_meetings} meetings · ${s.date_range[0]} → ${s.date_range[1]} · k=${s.n_clusters}`;
  const kpis = [
    { label: "Meetings", value: s.n_meetings },
    { label: "Avg sentiment", value: fmt(s.sentiment.overall) },
    { label: "Call types", value: Object.keys(s.call_types).length },
    { label: "Products", value: Object.keys(s.products).length },
    { label: "Cluster silhouette", value: fmt(s.silhouette, 3) },
  ];
  $("#kpis").innerHTML = kpis
    .map((k) => `<div class="kpi"><div class="label">${k.label}</div><div class="value">${k.value}</div></div>`)
    .join("");
  return s;
}

// -------- Overview --------
async function loadOverview(summary) {
  // Boxplot of raw scores per call type
  const scores = await api("/sentiment/scores");
  const boxData = Object.entries(scores).map(([ct, vals]) => ({
    y: vals, name: ct, type: "box", boxpoints: "all", jitter: 0.4,
    marker: { color: COLORS[ct] || "#777", size: 5 },
    line: { color: COLORS[ct] || "#777" },
  }));
  Plotly.newPlot("chart-sentiment-calltype", boxData, {
    ...PLOTLY_BASE, yaxis: { title: "Score (1–5)", range: [1, 5] },
    showlegend: false,
    shapes: [{ type: "line", x0: -0.5, x1: boxData.length - 0.5, y0: 3, y1: 3,
               xref: "x", yref: "y", line: { color: "red", dash: "dash", width: 1 }}],
  }, PLOTLY_CFG);

  // Sentiment by purpose
  const byPurpose = await api("/sentiment/by-purpose");
  Plotly.newPlot("chart-sentiment-purpose", [{
    x: byPurpose.map(p => p.mean), y: byPurpose.map(p => p.group),
    type: "bar", orientation: "h",
    marker: {
      color: byPurpose.map(p => p.mean < 3 ? "#f44336" : p.mean < 3.5 ? "#FF9800" : "#4CAF50"),
    },
    text: byPurpose.map(p => `n=${p.count}`), textposition: "outside",
  }], {
    ...PLOTLY_BASE, xaxis: { title: "Mean sentiment", range: [1, 5.3] },
    yaxis: { automargin: true },
    margin: { ...PLOTLY_BASE.margin, l: 150 },
    shapes: [{ type: "line", x0: 3, x1: 3, y0: -0.5, y1: byPurpose.length - 0.5,
               yref: "y", xref: "x", line: { color: "gray", dash: "dash", width: 1 }}],
  }, PLOTLY_CFG);

  // Weekly trend
  const weekly = await api("/sentiment/weekly");
  const series = {};
  weekly.forEach(p => {
    series[p.call_type] ??= { x: [], y: [] };
    series[p.call_type].x.push(p.week);
    series[p.call_type].y.push(p.sentiment_score);
  });
  Plotly.newPlot("chart-weekly", Object.entries(series).map(([ct, s]) => ({
    x: s.x, y: s.y, name: ct, mode: "lines+markers",
    line: { color: COLORS[ct], width: 2 }, marker: { size: 7 },
  })), {
    ...PLOTLY_BASE, xaxis: { title: "ISO Week (2026)" },
    yaxis: { title: "Mean sentiment", range: [1.5, 5] },
    shapes: [
      { type: "rect", x0: 10, x1: 12, y0: 1.5, y1: 5, xref: "x", yref: "y",
        fillcolor: "red", opacity: 0.1, line: { width: 0 }},
      { type: "line", x0: weekly[0]?.week ?? 5, x1: weekly[weekly.length - 1]?.week ?? 17,
        y0: 3, y1: 3, line: { color: "gray", dash: "dash", width: 1 }},
    ],
    legend: { orientation: "h", y: -0.2 },
  }, PLOTLY_CFG);

  // Distributions
  const ctData = Object.entries(summary.call_types);
  Plotly.newPlot("chart-call-type-dist", [{
    x: ctData.map(([k]) => k), y: ctData.map(([, v]) => v), type: "bar",
    marker: { color: ctData.map(([k]) => COLORS[k] || "#777") },
  }], { ...PLOTLY_BASE, yaxis: { title: "Count" } }, PLOTLY_CFG);

  const prodData = Object.entries(summary.products).sort((a, b) => b[1] - a[1]);
  Plotly.newPlot("chart-product-dist", [{
    x: prodData.map(([k]) => k), y: prodData.map(([, v]) => v),
    type: "bar", marker: { color: "#2196F3" },
  }], { ...PLOTLY_BASE, yaxis: { title: "Mentions" } }, PLOTLY_CFG);
}

// -------- Customers --------
async function loadCustomers() {
  const customers = await api("/insights/customer-health");
  const tierClass = (t) => t.includes("high") ? "tier-high" : t.includes("medium") ? "tier-medium" : "tier-low";
  $("#table-customers").innerHTML = `
    <thead><tr><th>Customer</th><th>Tier</th><th>Risk</th><th>Avg sent.</th>
      <th>Min sent.</th><th>Meetings</th><th>Churn signals</th></tr></thead>
    <tbody>${customers.map(c => `
      <tr>
        <td>${c.customer}</td>
        <td><span class="tier ${tierClass(c.risk_tier)}">${c.risk_tier}</span></td>
        <td>${fmt(c.risk_score, 3)}</td>
        <td>${fmt(c.avg_sentiment)}</td>
        <td>${fmt(c.min_sentiment)}</td>
        <td>${c.num_meetings}</td>
        <td>${c.churn_signals}</td>
      </tr>`).join("")}</tbody>`;

  $("#customer-select").innerHTML =
    `<option value="">Pick a customer…</option>` +
    customers.map(c => `<option value="${c.customer}">${c.customer} (${c.risk_tier})</option>`).join("");

  $("#customer-select").addEventListener("change", async (e) => {
    const name = e.target.value;
    if (!name) { $("#customer-detail").innerHTML = ""; return; }
    const detail = await api(`/insights/customer/${encodeURIComponent(name)}`);
    $("#customer-detail").innerHTML = `
      <div class="mini-kpis">
        <div class="mini-kpi"><div class="label">Tier</div><div class="value">${detail.risk_tier}</div></div>
        <div class="mini-kpi"><div class="label">Risk score</div><div class="value">${fmt(detail.risk_score, 3)}</div></div>
        <div class="mini-kpi"><div class="label">Meetings</div><div class="value">${detail.meetings.length}</div></div>
      </div>
      <table class="data">
        <thead><tr><th>Date</th><th>Title</th><th>Purpose</th><th>Sentiment</th><th>Max drop</th></tr></thead>
        <tbody>${detail.meetings.map(m => `
          <tr><td>${m.start_time.split("T")[0]}</td><td>${m.title}</td>
              <td>${m.meeting_purpose}</td><td>${fmt(m.sentiment_score)}</td>
              <td>${fmt(m.max_drop)}</td></tr>`).join("")}
        </tbody>
      </table>`;
  });
}

// -------- Incident --------
async function loadIncident() {
  const inc = await api("/insights/incident-impact");
  $("#incident-kpis").innerHTML = `
    <div class="mini-kpi"><div class="label">Affected</div><div class="value">${inc.n_affected}/${inc.n_total}</div></div>
    <div class="mini-kpi"><div class="label">Affected %</div><div class="value">${fmt(inc.affected_pct, 1)}%</div></div>
    <div class="mini-kpi"><div class="label">Direct mtgs</div><div class="value">${inc.n_direct}</div></div>
    <div class="mini-kpi"><div class="label">Sentiment Δ</div><div class="value">${fmt(inc.sentiment_unaffected - inc.sentiment_affected)}</div></div>`;

  // Re-fetch all meetings + flag affected
  const all = await api("/meetings?limit=1000");
  const directIds = new Set(inc.direct_meetings.map(m => m.meeting_id));
  const traces = ["external", "internal", "support"].map(ct => ({
    x: all.filter(m => m.call_type === ct).map(m => m.start_time),
    y: all.filter(m => m.call_type === ct).map(m => m.sentiment_score),
    text: all.filter(m => m.call_type === ct).map(m => m.title),
    mode: "markers", type: "scatter", name: ct,
    marker: { color: COLORS[ct], size: 8, opacity: 0.7 },
    hovertemplate: "%{text}<br>%{x|%b %d}<br>sent: %{y}<extra></extra>",
  }));
  traces.push({
    x: inc.direct_meetings.map(m => m.start_time),
    y: inc.direct_meetings.map(m => m.sentiment_score),
    text: inc.direct_meetings.map(m => m.title),
    mode: "markers", type: "scatter", name: "incident response",
    marker: { color: "red", symbol: "x", size: 14, line: { color: "black", width: 1 }},
    hovertemplate: "<b>%{text}</b><br>%{x|%b %d}<br>sent: %{y}<extra></extra>",
  });
  Plotly.newPlot("chart-incident", traces, {
    ...PLOTLY_BASE, xaxis: { title: "Date" },
    yaxis: { title: "Sentiment", range: [1, 5.2] },
    shapes: [{ type: "line", x0: all[all.length - 1]?.start_time, x1: all[0]?.start_time,
               y0: 3, y1: 3, line: { color: "gray", dash: "dash", width: 1 }}],
    legend: { orientation: "h", y: -0.2 },
  }, PLOTLY_CFG);

  $("#table-incident").innerHTML = `
    <thead><tr><th>Date</th><th>Title</th><th>Sentiment</th><th>Action items</th></tr></thead>
    <tbody>${inc.direct_meetings.map(m => `
      <tr><td>${m.start_time.split("T")[0]} ${m.start_time.split("T")[1].slice(0, 5)}</td>
          <td>${m.title}</td><td>${fmt(m.sentiment_score)}</td>
          <td>${m.num_action_items}</td></tr>`).join("")}
    </tbody>`;
}

// -------- Meeting drill-down --------
async function loadMeetingPicker() {
  const meetings = await api("/meetings?limit=1000");
  $("#meeting-select").innerHTML =
    `<option value="">Pick a meeting…</option>` +
    meetings.map(m => `<option value="${m.meeting_id}">
      ${m.start_time.split("T")[0]} · ${m.call_type.slice(0, 3)} · sent ${fmt(m.sentiment_score, 1)} · ${m.title}
    </option>`).join("");
  $("#meeting-select").addEventListener("change", async (e) => {
    const id = e.target.value;
    if (!id) { $("#meeting-detail").innerHTML = ""; return; }
    const m = await api(`/meetings/${id}`);
    let trajChart = "";
    if (m.trajectory) {
      trajChart = `<div id="meeting-trajectory" class="chart" style="height:240px"></div>`;
    }
    $("#meeting-detail").innerHTML = `
      <div class="mini-kpis">
        <div class="mini-kpi"><div class="label">Score</div><div class="value">${fmt(m.sentiment_score, 1)}</div></div>
        <div class="mini-kpi"><div class="label">Max drop</div><div class="value">${fmt(m.max_drop)}</div></div>
        <div class="mini-kpi"><div class="label">% negative</div><div class="value">${m.share_negative != null ? Math.round(m.share_negative * 100) + "%" : "—"}</div></div>
        <div class="mini-kpi"><div class="label">Action items</div><div class="value">${m.num_action_items}</div></div>
      </div>
      <p><strong>Summary:</strong> ${m.summary_text}</p>
      ${trajChart}
      <h4 class="section-title">Transcript with per-sentence sentiment</h4>
      <div id="transcript">${m.sentences.map(s => `
        <div class="transcript-row">
          <span class="sent-dot sent-${s.sentiment}"></span>
          <span class="speaker">${s.speaker}</span>
          <span class="text">${s.sentence}</span>
        </div>`).join("")}</div>`;

    if (m.trajectory) {
      Plotly.newPlot("meeting-trajectory", [{
        x: m.trajectory.map((_, i) => i),
        y: m.trajectory, mode: "lines+markers",
        line: { color: "#2196F3", width: 3 }, marker: { size: 9 },
      }], {
        ...PLOTLY_BASE, xaxis: { title: "Bucket (start → end)", dtick: 1 },
        yaxis: { title: "Mean sentence sentiment", range: [-1.1, 1.1] },
        shapes: [{ type: "line", x0: 0, x1: m.trajectory.length - 1, y0: 0, y1: 0,
                   line: { color: "gray", dash: "dash", width: 1 }}],
      }, PLOTLY_CFG);
    }
  });
}

// -------- Clusters --------
async function loadClusters() {
  const c = await api("/clusters");
  $("#cluster-caption").textContent = `k = ${c.k} chosen via silhouette score (${c.silhouette}).`;
  $("#table-clusters").innerHTML = `
    <thead><tr><th>Cluster</th><th>Size</th><th>Top terms</th><th>Dominant purpose</th><th>Avg sentiment</th></tr></thead>
    <tbody>${c.clusters.map(cl => `
      <tr><td>${cl.cluster}</td><td>${cl.size}</td>
          <td>${cl.top_terms.join(", ")}</td>
          <td>${cl.dominant_purpose}</td>
          <td>${fmt(cl.avg_sentiment)}</td></tr>`).join("")}
    </tbody>`;
}

// -------- Boot --------
(async function main() {
  try {
    const summary = await loadSummary();
    await Promise.all([
      loadOverview(summary),
      loadCustomers(),
      loadIncident(),
      loadMeetingPicker(),
      loadClusters(),
    ]);
  } catch (e) {
    console.error(e);
    $("#kpis").innerHTML = `<div class="error">Failed to load: ${e.message}</div>`;
  }
})();
