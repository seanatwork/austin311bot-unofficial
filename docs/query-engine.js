/**
 * austin311.com — Client-side query engine
 *
 * Handles the NL search bar. Queries go to the Cloudflare Worker (LLM-parsed)
 * first; local pre-computed JSON with pattern matching is the offline fallback.
 * Renders results (summary + Chart.js + table).
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
  let monthlyAggs = null;
  let districtCounts = null;
  let serviceCodes = null;
  let chartInstance = null;

  // ── Preload pre-computed data ────────────────────────────────────────────
  async function preloadPrecomputed() {
    if (precomputedLoaded) return;
    try {
      const [dcResp, rsResp, maResp, distResp] = await Promise.all([
        fetch(`${QUERYSTORE_BASE}/daily_counts.json`),
        fetch(`${QUERYSTORE_BASE}/resolution_stats.json`),
        fetch(`${QUERYSTORE_BASE}/monthly_aggregates.json`),
        fetch(`${QUERYSTORE_BASE}/district_counts.json`),
      ]);
      if (dcResp.ok) dailyCounts = await dcResp.json();
      if (rsResp.ok) resolutionStats = await rsResp.json();
      if (maResp.ok) monthlyAggs = await maResp.json();
      if (distResp.ok) districtCounts = await distResp.json();
      precomputedLoaded = true;
    } catch (e) {
      console.warn("Pre-computed data not available:", e);
    }
  }

  // ── Local query: try to answer from pre-computed data ────────────────────
  function tryLocalQuery(question) {
    if (!dailyCounts) return null;

    const q = question.toLowerCase();

    // Crime/911 topics live in Socrata, not the 311 querystore — leave them
    // to the worker rather than answering with all-category 311 data.
    if (/crime|burglar|theft|larceny|assault|robber|murder|homicide|shoot|crash|\b911\b/.test(q)) {
      return null;
    }

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

    // Pattern: "how many tickets are (currently) open (right now)?, by department?"
    // "open" phrasing varies ("tickets are open", "currently open", "open right
    // now") and "department" maps to our categories.
    if (/\bopen\b/.test(q) && /(?:tickets?|complaints?|reports?|requests?)/.test(q)) {
      const category = normalizeCategory(q);
      if (category && !/by\s+(?:department|category|type)s?/.test(q)) {
        return localOpenCount(category);
      }
      return localOpenBreakdown();
    }

    // Pattern: "how many tickets by department/category" (no status filter)
    if (/how\s+many/.test(q) && /by\s+(?:department|category|type)s?/.test(q)) {
      return localCount(null, "last_90d");
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

    // Generic resolution-time phrasing: "average graffiti resolution time",
    // "resolution time for potholes", "how long does it take to fix a pothole".
    // normalizeCategory() scans the whole question for a known category.
    if (/(?:resolution|response)\s+time|time\s+to\s+(?:fix|resolve|close)|how\s+long\s+(?:does\s+it\s+take\s+)?to\s+(?:fix|resolve|close)/.test(q)) {
      return localResolutionTime(q);
    }

    // Pattern: "[category] trend" / "trend over the last year" / "monthly volume"
    if (/trend|over\s+time|month\s+over\s+month|monthly|by\s+month/.test(q)) {
      const result = localTrend(q);
      if (result) return result;
    }

    // Pattern: "[category] in district N" / "by district"
    const districtMatch = q.match(/district\s+(\d{1,2})\b/);
    if (districtMatch) {
      const result = localDistrict(q, parseInt(districtMatch[1], 10));
      if (result) return result;
    } else if (/by\s+district|which\s+district|districts?\s+(?:has|have|with)/.test(q)) {
      const result = localDistrict(q, null);
      if (result) return result;
    }

    return null; // Can't answer locally
  }

  function normalizeCategory(raw) {
    const mapping = {
      "dead animal": "dead_animal",
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

  function localOpenBreakdown() {
    const totals = dailyCounts.totals_90d || {};
    const entries = Object.entries(totals).sort((a, b) => b[1].open - a[1].open);
    if (entries.length === 0) return null;
    const totalOpen = entries.reduce((sum, [, v]) => sum + v.open, 0);
    return {
      answer_summary: `${totalOpen} open tickets (filed in the last 90 days), by department: ${entries.slice(0, 5).map(([c, v]) => `${capitalize(c)}: ${v.open}`).join(" · ")}`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.map(([c]) => capitalize(c)),
          datasets: [{ label: "Open tickets", data: entries.map(([, v]) => v.open), backgroundColor: "#C2855C" }],
        },
      },
      table_data: entries.map(([c, v]) => ({ department: capitalize(c), open: v.open })),
      source: "precomputed",
    };
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

  function localTrend(rawQuestion) {
    if (!monthlyAggs || !monthlyAggs.months) return null;
    const months = monthlyAggs.months;
    const keys = Object.keys(months).sort();
    if (keys.length === 0) return null;

    const category = normalizeCategory(rawQuestion);
    const data = keys.map((m) => {
      const bucket = months[m] || {};
      if (category) return (bucket[category] || {}).total || 0;
      return Object.values(bucket).reduce((sum, v) => sum + (v.total || 0), 0);
    });

    const name = category ? capitalize(category) : "All categories";
    const first = data[0];
    const latest = data[data.length - 1];
    const direction = latest > first ? "up" : latest < first ? "down" : "flat";

    return {
      answer_summary: `${name} monthly volume: ${first} in ${keys[0]} → ${latest} in ${keys[keys.length - 1]} (trending ${direction}).`,
      chart_config: {
        type: "line",
        data: {
          labels: keys,
          datasets: [{ label: name, data, borderColor: "#3b82f6", tension: 0.3, fill: false }],
        },
      },
      table_data: keys.map((m, i) => ({ month: m, total: data[i] })),
      source: "precomputed",
    };
  }

  function localDistrict(rawQuestion, district) {
    if (!districtCounts || !districtCounts.windows) return null;
    const win = districtCounts.windows["90d"] || {};
    const category = normalizeCategory(rawQuestion);

    // Specific district, specific category: "potholes in district 3"
    if (district && category) {
      const d = (win[category] || {})[String(district)];
      if (!d) {
        return { answer_summary: `No ${capitalize(category)} complaints recorded in district ${district} over the last 90 days.`, source: "precomputed" };
      }
      return {
        answer_summary: `${d.total} ${capitalize(category)} complaints in district ${district} (last 90 days). ${d.open} open, ${d.closed} closed.`,
        chart_config: {
          type: "doughnut",
          data: {
            labels: ["Open", "Closed"],
            datasets: [{ label: capitalize(category), data: [d.open, d.closed], backgroundColor: ["#C2855C", "#7A9A6D"] }],
          },
        },
        table_data: [{ status: "Open", count: d.open }, { status: "Closed", count: d.closed }],
        source: "precomputed",
      };
    }

    // Specific district, all categories
    if (district && !category) {
      const entries = Object.entries(win)
        .map(([cat, districts]) => [cat, districts[String(district)]])
        .filter(([, d]) => d)
        .sort((a, b) => b[1].total - a[1].total);
      if (entries.length === 0) return null;
      const total = entries.reduce((sum, [, d]) => sum + d.total, 0);
      return {
        answer_summary: `${total} complaints in district ${district} (last 90 days). Top: ${entries.slice(0, 3).map(([c, d]) => `${capitalize(c)}: ${d.total}`).join(" · ")}`,
        chart_config: {
          type: "bar",
          data: {
            labels: entries.map(([c]) => capitalize(c)),
            datasets: [{ label: `District ${district}`, data: entries.map(([, d]) => d.total), backgroundColor: "#3b82f6" }],
          },
        },
        table_data: entries.map(([c, d]) => ({ category: capitalize(c), total: d.total, open: d.open })),
        source: "precomputed",
      };
    }

    // Category across all districts: "which district has the most noise complaints"
    if (!district && category) {
      const districts = win[category] || {};
      const entries = Object.entries(districts)
        .map(([d, v]) => [parseInt(d, 10), v])
        .sort((a, b) => a[0] - b[0]);
      if (entries.length === 0) return null;
      const top = [...entries].sort((a, b) => b[1].total - a[1].total)[0];
      return {
        answer_summary: `${capitalize(category)} by district (last 90 days): district ${top[0]} leads with ${top[1].total}.`,
        chart_config: {
          type: "bar",
          data: {
            labels: entries.map(([d]) => `D${d}`),
            datasets: [{ label: capitalize(category), data: entries.map(([, v]) => v.total), backgroundColor: "#3b82f6" }],
          },
        },
        table_data: entries.map(([d, v]) => ({ district: d, total: v.total, open: v.open })),
        source: "precomputed",
      };
    }

    return null;
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
        <button onclick="window.queryFilterSubmit()">Submit</button>
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

    // Make sure pre-computed data has loaded (DOMContentLoaded race)
    await preloadPrecomputed();

    // Step 1: Ask the worker (LLM parses the question into structured params)
    const result = await queryWorker(question);
    if (result && !result.fallback) {
      loadingEl.style.display = "none";
      renderResults(result);
      return;
    }

    // Step 2: Worker unavailable or couldn't parse — fall back to local
    // pre-computed data matched against common question patterns
    const local = tryLocalQuery(question);
    if (local) {
      loadingEl.style.display = "none";
      renderResults(local);
      return;
    }

    loadingEl.style.display = "none";

    if (result && result.fallback) {
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
