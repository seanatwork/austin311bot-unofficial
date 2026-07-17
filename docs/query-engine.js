/**
 * austin311.com — Client-side query engine
 *
 * Handles the NL search bar, routes queries to either local pre-computed JSON
 * or the Cloudflare Worker API, and renders results (summary + Chart.js + table).
 */

(function () {
  "use strict";

  // ── Config ──────────────────────────────────────────────────────────────
  const API_BASE =
    window.location.hostname === "localhost"
      ? "http://localhost:8787/api/v1"
      : "/api/v1";
  const QUERYSTORE_BASE = "/querystore";

  // ── DOM refs ────────────────────────────────────────────────────────────
  const input = document.getElementById("query-input");
  const loadingEl = document.getElementById("query-loading");
  const resultsEl = document.getElementById("query-results");
  const hintsEl = document.getElementById("query-hints");

  // ── State ───────────────────────────────────────────────────────────────
  let precomputedLoaded = false;
  let dailyCounts = null;
  let resolutionStats = null;
  let serviceCodes = null;
  let chartInstance = null;

  // ── Preload pre-computed data ────────────────────────────────────────────
  async function preloadPrecomputed() {
    if (precomputedLoaded) return;
    try {
      const [dcResp, rsResp] = await Promise.all([
        fetch(`${QUERYSTORE_BASE}/daily_counts.json`),
        fetch(`${QUERYSTORE_BASE}/resolution_stats.json`),
      ]);
      if (dcResp.ok) dailyCounts = await dcResp.json();
      if (rsResp.ok) resolutionStats = await rsResp.json();
      precomputedLoaded = true;
    } catch (e) {
      console.warn("Pre-computed data not available:", e);
    }
  }

  // ── Local query: try to answer from pre-computed data ────────────────────
  function tryLocalQuery(question) {
    if (!dailyCounts) return null;

    const q = question.toLowerCase();

    // Pattern: "how many [category] [timeframe]"
    const countMatch = q.match(
      /how\s+many\s+(\w+(?:\s+\w+)*?)\s+(?:complaints?|reports?|requests?|tickets?)\s+(?:in\s+(?:the\s+)?)?(last\s+(?:\d+\s+)?(?:day|week|month|year)s?|this\s+(?:month|year)|today|the\s+past\s+(?:\d+\s+)?(?:day|week|month|year)s?)/
    );
    if (countMatch) {
      const category = normalizeCategory(countMatch[1]);
      const timeframe = normalizeDateRange(countMatch[2]);
      return localCount(category, timeframe);
    }

    // Pattern: "how many open [category]"
    const openMatch = q.match(
      /how\s+many\s+open\s+(\w+(?:\s+\w+)*?)\s+(?:complaints?|reports?|requests?|tickets?)/
    );
    if (openMatch) {
      const category = normalizeCategory(openMatch[1]);
      return localOpenCount(category);
    }

    // Pattern: "top [n] [metric]"
    const topMatch = q.match(/top\s+(\d+)\s+(\w+(?:\s+\w+)*?)\s*(?:complaints?|reports?|types?|issues?)/);
    if (topMatch) {
      const n = parseInt(topMatch[1], 10);
      return localTopCategories(n);
    }

    // Pattern: "how fast [category]"
    const speedMatch = q.match(/how\s+fast\s+(?:are|is)\s+(\w+(?:\s+\w+)*?)\s+(?:getting\s+)?(?:fixed|resolved|handled)/);
    if (speedMatch) {
      return localResolutionTime(speedMatch[1]);
    }

    // Pattern: "average [time] for [category]"
    const avgMatch = q.match(/(?:average|mean|typical)\s+(?:resolution\s+)?time\s+(?:for|to\s+fix|to\s+resolve)\s+(\w+(?:\s+\w+)*?)/);
    if (avgMatch) {
      return localResolutionTime(avgMatch[1]);
    }

    return null; // Can't answer locally
  }

  function normalizeCategory(raw) {
    const mapping = {
      graffiti: "graffiti",
      pothole: "traffic", potholes: "traffic",
      "traffic signal": "traffic", signal: "traffic",
      "street light": "traffic", streetlight: "traffic",
      parking: "parking", "parking violation": "parking",
      noise: "noise", "noise complaint": "noise", "loud music": "noise",
      homeless: "homeless", encampment: "homeless", "homeless camp": "homeless",
      animal: "animal", dog: "animal", "loose dog": "animal", coyote: "animal",
      park: "parks", parks: "parks",
      storm: "storm", flooding: "storm", drainage: "storm", debris: "storm",
      bicycle: "bicycle", bike: "bicycle",
      "dead animal": "dead_animal",
    };

    const normalized = raw.toLowerCase().trim().replace(/\bi\s+ng\b/g, "");
    for (const [key, value] of Object.entries(mapping)) {
      if (normalized.includes(key)) return value;
    }
    // Try to match category names directly
    for (const cat of Object.keys(dailyCounts.totals_90d || {})) {
      if (normalized.includes(cat.replace("_", " "))) return cat;
    }
    return null;
  }

  function normalizeDateRange(raw) {
    const r = raw.toLowerCase().trim();
    if (r.includes("today")) return "today";
    if (r.includes("last 30") || r.includes("past 30") || r.includes("30 days") || r.includes("this month")) return "last_30d";
    if (r.includes("last 90") || r.includes("past 90") || r.includes("90 days")) return "last_90d";
    if (r.includes("last year") || r.includes("past year") || r.includes("365")) return "last_365d";
    if (r.includes("this year")) return "this_year";
    if (r.includes("last month")) return "last_month";
    if (r.includes("last week") || r.includes("past week")) return "last_week";
    return "last_30d"; // Default
  }

  function localCount(category, timeframe) {
    const totalsKey = timeframe === "last_365d" ? "totals_365d" : "totals_90d";
    const totals = dailyCounts[totalsKey] || dailyCounts.totals_90d || {};

    if (category && totals[category]) {
      const { total, open, closed } = totals[category];
      const name = capitalize(category);
      return {
        answer_summary: `${total} ${name} complaints. ${open} open, ${closed} closed.`,
        chart_config: {
          type: "doughnut",
          data: {
            labels: ["Open", "Closed"],
            datasets: [{
              label: name,
              data: [open, closed],
              backgroundColor: ["#C2855C", "#7A9A6D"],
            }],
          },
        },
        table_data: [{ status: "Open", count: open }, { status: "Closed", count: closed }],
        source: "precomputed",
      };
    }

    // All categories, sorted
    const entries = Object.entries(totals).sort((a, b) => b[1].total - a[1].total);
    return {
      answer_summary: `Top categories: ${entries.slice(0, 5).map(([c, v]) => `${capitalize(c)}: ${v.total}`).join(" · ")}`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.slice(0, 10).map(([c]) => capitalize(c)),
          datasets: [{ label: "Total", data: entries.slice(0, 10).map(([, v]) => v.total), backgroundColor: "#3b82f6" }],
        },
      },
      table_data: entries.map(([c, v]) => ({ category: capitalize(c), total: v.total, open: v.open })),
      source: "precomputed",
    };
  }

  function localOpenCount(category) {
    const totals = dailyCounts.totals_90d || {};
    if (category && totals[category]) {
      const { open } = totals[category];
      return {
        answer_summary: `${open} open ${capitalize(category)} complaints.`,
        table_data: [{ status: "Open", count: open }],
        source: "precomputed",
      };
    }
    return null;
  }

  function localTopCategories(n) {
    const totals = dailyCounts.totals_90d || {};
    const entries = Object.entries(totals).sort((a, b) => b[1].total - a[1].total).slice(0, n);
    return {
      answer_summary: `Top ${n} categories: ${entries.map(([c, v]) => `${capitalize(c)}: ${v.total}`).join(" · ")}`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.map(([c]) => capitalize(c)),
          datasets: [{ label: "Total", data: entries.map(([, v]) => v.total), backgroundColor: "#3b82f6" }],
        },
      },
      table_data: entries.map(([c, v]) => ({ category: capitalize(c), total: v.total, open: v.open })),
      source: "precomputed",
    };
  }

  function localResolutionTime(rawCategory) {
    if (!resolutionStats) return null;
    const stats = resolutionStats["90d"] || {};
    const category = normalizeCategory(rawCategory);

    if (category && stats[category] && stats[category].avg_days !== null) {
      const s = stats[category];
      return {
        answer_summary: `${capitalize(category)}: avg ${s.avg_days} days to close (median: ${s.median_days}d, 90th %ile: ${s.p90_days}d).`,
        chart_config: {
          type: "bar",
          data: {
            labels: ["Average", "Median", "90th %ile"],
            datasets: [{ label: "Days", data: [s.avg_days, s.median_days, s.p90_days], backgroundColor: "#C2855C" }],
          },
        },
        table_data: [s],
        source: "precomputed",
      };
    }

    // All categories
    const entries = Object.entries(stats).filter(([, s]) => s.avg_days !== null)
      .sort((a, b) => a[1].avg_days - b[1].avg_days);
    if (entries.length === 0) return null;
    return {
      answer_summary: `Fastest: ${capitalize(entries[0][0])} (${entries[0][1].avg_days}d). Slowest: ${capitalize(entries[entries.length - 1][0])} (${entries[entries.length - 1][1].avg_days}d).`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.map(([c]) => capitalize(c)),
          datasets: [{ label: "Avg days", data: entries.map(([, s]) => s.avg_days), backgroundColor: "#3b82f6" }],
        },
      },
      table_data: entries.map(([c, s]) => ({ category: capitalize(c), ...s })),
      source: "precomputed",
    };
  }

  function capitalize(category) {
    const names = {
      homeless: "Homeless", parking: "Parking", noise: "Noise", animal: "Animal Services",
      graffiti: "Graffiti", parks: "Parks", storm: "Storm & Drainage",
      traffic: "Traffic", bicycle: "Bicycle", dead_animal: "Dead Animal",
    };
    return names[category] || category.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // ── Query via worker API ─────────────────────────────────────────────────
  async function queryWorker(question) {
    try {
      const resp = await fetch(`${API_BASE}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: question }),
      });
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      console.error("Worker query failed:", e);
      return null;
    }
  }

  // ── Render results ───────────────────────────────────────────────────────
  function renderResults(data) {
    // Destroy existing chart
    if (chartInstance) {
      chartInstance.destroy();
      chartInstance = null;
    }

    let html = "";

    if (data.answer_summary) {
      html += `<div class="answer-summary">${data.answer_summary}</div>`;
    }

    if (data.chart_config) {
      const canvasId = `chart-${Date.now()}`;
      html += `<div class="query-chart"><canvas id="${canvasId}"></canvas></div>`;

      // Render chart after DOM update
      setTimeout(() => {
        const canvas = document.getElementById(canvasId);
        if (canvas && data.chart_config) {
          chartInstance = new Chart(canvas, {
            type: data.chart_config.type || "bar",
            data: data.chart_config.data,
            options: {
              responsive: true,
              maintainAspectRatio: true,
              plugins: {
                legend: { display: data.chart_config.type === "doughnut" },
              },
            },
          });
        }
      }, 50);
    }

    if (data.table_data && data.table_data.length > 0) {
      const keys = Object.keys(data.table_data[0]);
      html += `<table class="query-table"><thead><tr>${keys.map((k) => `<th>${k}</th>`).join("")}</tr></thead><tbody>`;
      html += data.table_data.map((row) => `<tr>${keys.map((k) => `<td>${row[k] != null ? row[k] : "—"}</td>`).join("")}</tr>`).join("");
      html += "</tbody></table>";
    }

    if (data.source) {
      html += `<div style="font-size:0.75rem;color:var(--text-muted);margin-top:0.5rem;">Source: ${data.source} · ${data.freshness || "daily"}</div>`;
    }

    // Show filter panel if the worker couldn't parse
    if (data.fallback) {
      html += buildFilterPanel();
    }

    resultsEl.innerHTML = html;
  }

  function buildFilterPanel() {
    return `
      <div class="query-filter-panel">
        <p style="margin-bottom:0.5rem;color:var(--text-tag);">Try the structured filters:</p>
        <select id="filter-category">
          <option value="">All categories</option>
          <option value="graffiti">Graffiti</option>
          <option value="parking">Parking</option>
          <option value="noise">Noise</option>
          <option value="homeless">Homeless</option>
          <option value="animal">Animal Services</option>
          <option value="traffic">Traffic</option>
          <option value="parks">Parks</option>
          <option value="storm">Storm & Drainage</option>
          <option value="bicycle">Bicycle</option>
          <option value="dead_animal">Dead Animal</option>
        </select>
        <select id="filter-timeframe">
          <option value="last_30d">Last 30 days</option>
          <option value="last_90d">Last 90 days</option>
          <option value="last_365d">Last year</option>
        </select>
        <select id="filter-metric">
          <option value="count">Total count</option>
          <option value="open">Open only</option>
          <option value="resolution">Resolution time</option>
        </select>
        <button onclick="document.querySelector('script[src*=\\'query-engine\\']').queryFilterSubmit()">Submit</button>
      </div>`;
  }

  function submitFilterQuery() {
    const category = document.getElementById("filter-category")?.value || "";
    const timeframe = document.getElementById("filter-timeframe")?.value || "last_30d";
    const metric = document.getElementById("filter-metric")?.value || "count";

    if (metric === "resolution") {
      const result = localResolutionTime(category);
      if (result) renderResults(result);
      return;
    }

    const result = localCount(category, timeframe);
    if (result) renderResults(result);
  }

  // Expose for inline onclick
  window.queryFilterSubmit = submitFilterQuery;

  // ── Main query handler ───────────────────────────────────────────────────
  async function ask(question) {
    if (!question || !question.trim()) return;

    loadingEl.style.display = "block";
    resultsEl.innerHTML = "";
    if (hintsEl) hintsEl.style.display = "none";

    // Step 1: Try local pre-computed data first
    const local = tryLocalQuery(question);
    if (local) {
      loadingEl.style.display = "none";
      renderResults(local);
      return;
    }

    // Step 2: Fall back to worker API
    const result = await queryWorker(question);

    loadingEl.style.display = "none";

    if (result && !result.fallback) {
      renderResults(result);
    } else if (result && result.fallback) {
      renderResults(result); // Shows filter panel
    } else {
      renderResults({ answer_summary: "Something went wrong. Please try again.", fallback: true });
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    // Preload pre-computed data
    preloadPrecomputed();

    // Handle Enter key
    if (input) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          ask(input.value);
        }
      });
    }
  });

  // Expose for external use
  window.ask311 = ask;
})();
