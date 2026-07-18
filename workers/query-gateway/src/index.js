/**
 * austin311.com Query Gateway — Cloudflare Worker
 *
 * Routes natural-language questions through Workers AI to extract structured
 * query params, then dispatches to pre-computed JSON, Socrata SoQL, or
 * Open311 APIs. Returns formatted results (summary + Chart.js config).
 *
 * Endpoints:
 *   POST /api/v1/query       — NL question → structured query → results
 *   GET  /api/v1/socrata/:id  — Socrata SoQL proxy
 *   GET  /api/v1/open311      — Open311 list proxy
 *   GET  /api/v1/open311/:id  — Single ticket detail
 */

import { buildLLMSystemPrompt, CATEGORY_CODES, CATEGORY_NAMES, SERVICE_CODE_NAMES, SOCRATA_DATASETS } from "./categories.js";

// ── Configuration ───────────────────────────────────────────────────────────

const SITE_BASE = "https://austin311.com";
const QUERYSTORE_BASE = `${SITE_BASE}/querystore`;
const OPEN311_BASE = "https://311.austintexas.gov/open311/v2";
const SOCRATA_BASE = "https://data.austintexas.gov/resource";

const RATE_LIMIT_WINDOW = 60; // seconds
const RATE_LIMIT_MAX = 20;    // max requests per window per IP

// In-memory rate limit store (reset on worker cold start)
const rateLimitStore = new Map();

// ── Utilities ───────────────────────────────────────────────────────────────

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "*";
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "public, max-age=60",
      ...extraHeaders,
    },
  });
}

function errorResponse(error, fallback = true, status = 400, extraHeaders = {}) {
  return jsonResponse({ error, fallback }, status, extraHeaders);
}

// ── Rate limiting ───────────────────────────────────────────────────────────

function checkRateLimit(request) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const now = Math.floor(Date.now() / 1000);
  const windowStart = now - RATE_LIMIT_WINDOW;

  const entry = rateLimitStore.get(ip) || { timestamps: [] };
  entry.timestamps = entry.timestamps.filter((t) => t > windowStart);
  entry.timestamps.push(now);
  rateLimitStore.set(ip, entry);

  return entry.timestamps.length <= RATE_LIMIT_MAX;
}

// ── Date resolution ─────────────────────────────────────────────────────────

function resolveDateRange(dateRange) {
  if (!dateRange || !dateRange.start) return null;

  const now = new Date();
  const start = dateRange.start;

  const presets = {
    today: () => {
      const d = new Date(now);
      d.setHours(0, 0, 0, 0);
      return { start: d, end: now };
    },
    last_week: () => {
      const d = new Date(now);
      d.setDate(d.getDate() - 7);
      return { start: d, end: now };
    },
    last_30d: () => {
      const d = new Date(now);
      d.setDate(d.getDate() - 30);
      return { start: d, end: now };
    },
    last_90d: () => {
      const d = new Date(now);
      d.setDate(d.getDate() - 90);
      return { start: d, end: now };
    },
    last_365d: () => {
      const d = new Date(now);
      d.setDate(d.getDate() - 365);
      return { start: d, end: now };
    },
    this_year: () => {
      const d = new Date(now.getFullYear(), 0, 1);
      return { start: d, end: now };
    },
    last_month: () => {
      const d = new Date(now);
      d.setMonth(d.getMonth() - 1);
      return { start: d, end: now };
    },
  };

  if (presets[start]) {
    return presets[start]();
  }

  // Try parsing as YYYY-MM-DD
  const parsed = new Date(start);
  if (!isNaN(parsed.getTime())) {
    return { start: parsed, end: dateRange.end ? new Date(dateRange.end) : now };
  }

  return null;
}

function formatDateUTC(d) {
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

// ── LLM Parsing ─────────────────────────────────────────────────────────────

async function parseQuestion(env, question) {
  const systemPrompt = buildLLMSystemPrompt();

  const messages = [
    { role: "system", content: systemPrompt },
    { role: "user", content: question },
  ];

  try {
    const result = await env.AI.run("@cf/meta/llama-3.3-70b-instruct-fp8-fast", {
      messages,
      max_tokens: 400,
      temperature: 0.1, // Low temp for deterministic structured output
    });

    const text = extractLLMText(result).trim();
    if (!text) {
      console.error("LLM returned unexpected shape:", JSON.stringify(result).slice(0, 500));
      return null;
    }
    // Strip markdown code fences if present
    const jsonText = text.replace(/^```(?:json)?\s*/, "").replace(/\s*```$/, "");

    const params = JSON.parse(jsonText);
    return { params, rawResponse: text };
  } catch (e) {
    console.error("LLM parse error:", e);
    return null;
  }
}

// Workers AI response shapes vary by model: {response: string},
// OpenAI-style {choices: [{message: {content}}]}, or a bare string.
function extractLLMText(result) {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return "";
  if (typeof result.response === "string") return result.response;
  const content = result.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  return "";
}

function validateParams(params) {
  // Ensure required fields exist
  if (!params.intent) return false;

  // Validate source
  if (params.source && !["open311", "socrata", "precomputed"].includes(params.source)) {
    return false;
  }

  // Validate category if set
  if (params.category && !CATEGORY_CODES[params.category]) {
    // Try fuzzy matching — the LLM might output "pothole" instead of "traffic"
    for (const [cat, codes] of Object.entries(CATEGORY_CODES)) {
      if (params.category.toLowerCase().includes(cat.toLowerCase())) {
        params.category = cat;
        break;
      }
    }
    // If still invalid, clear it
    if (!CATEGORY_CODES[params.category]) {
      params.category = null;
    }
  }

  // Validate Socrata dataset
  if (params.socrata_dataset && !SOCRATA_DATASETS.includes(params.socrata_dataset)) {
    params.socrata_dataset = null;
  }

  // Validate district
  if (params.district !== null && params.district !== undefined) {
    const d = parseInt(params.district, 10);
    if (isNaN(d) || d < 1 || d > 10) {
      params.district = null;
    } else {
      params.district = d;
    }
  }

  // Validate date_range
  if (params.date_range && params.date_range.start) {
    const validStarts = [
      "today", "last_week", "last_30d", "last_90d", "last_365d",
      "this_year", "last_month",
    ];
    const isPreset = validStarts.includes(params.date_range.start);
    const isDate = /^\d{4}-\d{2}-\d{2}$/.test(params.date_range.start);
    if (!isPreset && !isDate) {
      params.date_range = null;
    }
  }

  return true;
}

// ── Socrata Query Builder ───────────────────────────────────────────────────

// Map common crime terms to fragments of APD `crime_type` values (fdj4-gpfu),
// matched case-insensitively with LIKE. "burglary" → BURG covers
// "BURGLARY OF RESIDENCE", "BURGLARY OF VEHICLE", etc.
const CRIME_ALIASES = {
  burglary: "BURG",
  burg: "BURG",
  theft: "THEFT",
  larceny: "THEFT",
  robbery: "ROBBERY",
  assault: "ASSAULT",
  vandalism: "MISCHIEF",
  "criminal mischief": "MISCHIEF",
  murder: "MURDER",
  homicide: "MURDER",
  rape: "RAPE",
  dui: "DWI",
  dwi: "DWI",
};

function crimeTypePattern(raw) {
  const key = String(raw).toLowerCase().trim();
  const fragment = CRIME_ALIASES[key] || key.toUpperCase();
  // Sanitize: only word chars, spaces, dashes — never let quotes into SoQL
  const safe = fragment.replace(/[^A-Z0-9 \-/]/g, "");
  return safe.length >= 3 ? safe : null;
}

function buildSoQL(params) {
  const { socrata_dataset, date_range, district, group_by, limit } = params;
  const queries = [];

  const resolved = resolveDateRange(date_range);

  // Build $select
  let select = "count(*)";

  if (group_by === "month") {
    const dateField = socrata_dataset === "fdj4-gpfu" ? "occ_date" :
                      socrata_dataset === "22de-7rzg" ? "response_datetime" : "date";
    select = `date_trunc_ym(${dateField}) as month, count(*)`;
  } else if (group_by === "district") {
    select = "council_district, count(*)";
  } else if (group_by === "category" || group_by === "day") {
    select = `${group_by}, count(*)`;
  }

  // Build $where
  const whereParts = [];
  if (resolved) {
    const dateField = socrata_dataset === "fdj4-gpfu" ? "occ_date" :
                      socrata_dataset === "22de-7rzg" ? "response_datetime" : "date";
    whereParts.push(`${dateField} >= '${formatDateUTC(resolved.start).slice(0, 10)}'`);
  }
  if (district) {
    whereParts.push(`council_district = '${district}'`);
  }
  if (socrata_dataset === "fdj4-gpfu" && params.crime_type) {
    const pattern = crimeTypePattern(params.crime_type);
    if (pattern) {
      whereParts.push(`upper(crime_type) like '%${pattern}%'`);
    }
  }

  const query = {};
  query.$select = select;

  if (whereParts.length > 0) {
    query.$where = whereParts.join(" AND ");
  }

  if (group_by && group_by !== "day") {
    query.$group = group_by === "month" ? "month" : group_by;
  }

  // Time series must be chronological; rankings sort by volume
  query.$order = group_by === "month" ? "month" : "count DESC";
  query.$limit = limit || 50;

  return query;
}

// ── Open311 Query ───────────────────────────────────────────────────────────

async function fetchOpen311(env, params) {
  const { service_codes, date_range, status } = params;
  const resolved = resolveDateRange(date_range);

  if (!resolved || !service_codes || service_codes.length === 0) return null;

  const code = service_codes[0]; // Single code for live queries
  const queryParams = new URLSearchParams({
    service_code: code,
    start_date: formatDateUTC(resolved.start),
    end_date: formatDateUTC(resolved.end),
    per_page: "100",
    page: "1",
    status: status === "open" ? "open" : "open,closed",
  });

  if (env.AUSTINAPIKEY) {
    queryParams.set("$$app_token", env.AUSTINAPIKEY);
  }

  const url = `${OPEN311_BASE}/requests.json?${queryParams.toString()}`;

  try {
    const resp = await fetch(url, {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (e) {
    console.error("Open311 fetch error:", e);
    return null;
  }
}

async function fetchOpen311Ticket(env, ticketId) {
  const url = new URL(`${OPEN311_BASE}/requests/${ticketId}.json`);
  if (env.AUSTINAPIKEY) {
    url.searchParams.set("$$app_token", env.AUSTINAPIKEY);
  }

  try {
    const resp = await fetch(url.toString(), {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    return Array.isArray(data) ? data[0] : data;
  } catch (e) {
    return null;
  }
}

// ── Pre-computed data ───────────────────────────────────────────────────────

async function fetchPrecomputed(env, filename) {
  const url = `${QUERYSTORE_BASE}/${filename}`;
  try {
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (e) {
    return null;
  }
}

// ── Result formatting ───────────────────────────────────────────────────────

function formatCountResult(records, params) {
  const total = Array.isArray(records) ? records.length : 0;
  const openCount = Array.isArray(records)
    ? records.filter((r) => (r.status || "").toLowerCase() === "open").length
    : 0;
  const closedCount = total - openCount;
  const category = CATEGORY_NAMES[params.category] || params.category || "311";

  return {
    answer_summary: `${total} ${category} complaints in the selected period. ${openCount} open, ${closedCount} closed.`,
    chart_config: {
      type: "doughnut",
      data: {
        labels: ["Open", "Closed"],
        datasets: [{ label: category, data: [openCount, closedCount], backgroundColor: ["#ef4444", "#22c55e"] }],
      },
    },
    table_data: [{ status: "Open", count: openCount }, { status: "Closed", count: closedCount }],
  };
}

function formatLookupResult(ticket) {
  if (!ticket) {
    return { answer_summary: "Ticket not found.", chart_config: null, table_data: null };
  }
  const id = ticket.service_request_id || "unknown";
  const status = (ticket.status || "unknown").toLowerCase();
  const desc = (ticket.description || "").slice(0, 500);
  const addr = ticket.address || "Unknown address";
  const requested = ticket.requested_datetime || "";
  return {
    answer_summary: `Ticket ${id}: **${status}** — ${desc ? desc.slice(0, 100) + "..." : "No description"}`,
    table_data: [
      { field: "Status", value: status },
      { field: "Address", value: addr },
      { field: "Requested", value: requested },
      { field: "Description", value: desc || "N/A" },
    ],
    chart_config: null,
  };
}

function formatFromPrecomputed(precomputed, params) {
  // Route to the right pre-computed dataset based on intent
  const { intent, category, date_range, district, group_by } = params;

  if (intent === "count" || intent === "rank") {
    const totals = precomputed.totals_90d || precomputed.totals_365d || {};
    const wantOpen = params.status === "open";
    if (category && totals[category]) {
      const { total, open, closed } = totals[category];
      const name = CATEGORY_NAMES[category] || category;
      if (wantOpen) {
        return {
          answer_summary: `${open} open ${name} tickets (filed in the last 90 days).`,
          chart_config: {
            type: "doughnut",
            data: {
              labels: ["Open", "Closed"],
              datasets: [{ label: name, data: [open, closed], backgroundColor: ["#ef4444", "#22c55e"] }],
            },
          },
          table_data: [{ status: "Open", count: open }, { status: "Closed", count: closed }],
        };
      }
      return {
        answer_summary: `${total} ${name} complaints in the selected period. ${open} open, ${closed} closed.`,
        chart_config: {
          type: "doughnut",
          data: {
            labels: ["Open", "Closed"],
            datasets: [{ label: name, data: [open, closed], backgroundColor: ["#ef4444", "#22c55e"] }],
          },
        },
        table_data: [{ status: "Open", count: open }, { status: "Closed", count: closed }],
      };
    }
    // All categories — optionally broken down by open status
    if (wantOpen) {
      const entries = Object.entries(totals).sort((a, b) => b[1].open - a[1].open);
      const totalOpen = entries.reduce((sum, [, v]) => sum + v.open, 0);
      return {
        answer_summary: `${totalOpen} open tickets (filed in the last 90 days), by department: ${entries.slice(0, 5).map(([cat, v]) => `${CATEGORY_NAMES[cat] || cat}: ${v.open}`).join(", ")}`,
        chart_config: {
          type: "bar",
          data: {
            labels: entries.map(([cat]) => CATEGORY_NAMES[cat] || cat),
            datasets: [{
              label: "Open tickets",
              data: entries.map(([, v]) => v.open),
              backgroundColor: "#ef4444",
            }],
          },
        },
        table_data: entries.map(([cat, v]) => ({ department: CATEGORY_NAMES[cat] || cat, open: v.open })),
      };
    }
    const entries = Object.entries(totals)
      .sort((a, b) => b[1].total - a[1].total);
    return {
      answer_summary: `Top categories by volume: ${entries.slice(0, 5).map(([cat, v]) => `${CATEGORY_NAMES[cat] || cat}: ${v.total}`).join(", ")}`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.map(([cat]) => CATEGORY_NAMES[cat] || cat),
          datasets: [{
            label: "Total",
            data: entries.map(([, v]) => v.total),
            backgroundColor: "#3b82f6",
          }],
        },
      },
      table_data: entries.map(([cat, v]) => ({ category: CATEGORY_NAMES[cat] || cat, total: v.total, open: v.open })),
    };
  }

  if (intent === "resolution_time") {
    const stats = precomputed["90d"] || {};
    if (category && stats[category] && stats[category].avg_days !== null) {
      const s = stats[category];
      const name = CATEGORY_NAMES[category] || category;
      return {
        answer_summary: `${name}: average resolution time is ${s.avg_days} days (median: ${s.median_days}, 90th percentile: ${s.p90_days} days).`,
        chart_config: {
          type: "bar",
          data: {
            labels: [name],
            datasets: [
              { label: "Avg Days", data: [s.avg_days], backgroundColor: "#3b82f6" },
              { label: "P90 Days", data: [s.p90_days], backgroundColor: "#f59e0b" },
            ],
          },
        },
        table_data: [s],
      };
    }
    // All categories for resolution stats
    const entries = Object.entries(stats).filter(([, s]) => s.avg_days !== null)
      .sort((a, b) => a[1].avg_days - b[1].avg_days);
    return {
      answer_summary: `Fastest: ${CATEGORY_NAMES[entries[0]?.[0]] || entries[0]?.[0]}: ${entries[0]?.[1]?.avg_days}d. Slowest: ${CATEGORY_NAMES[entries[entries.length - 1]?.[0]] || entries[entries.length - 1]?.[0]}: ${entries[entries.length - 1]?.[1]?.avg_days}d.`,
      chart_config: {
        type: "bar",
        data: {
          labels: entries.map(([cat]) => CATEGORY_NAMES[cat] || cat),
          datasets: [{ label: "Avg resolution days", data: entries.map(([, s]) => s.avg_days), backgroundColor: "#3b82f6" }],
        },
      },
      table_data: entries.map(([cat, s]) => ({ category: CATEGORY_NAMES[cat] || cat, ...s })),
    };
  }

  return null; // Can't handle this intent with pre-computed data
}

// ── Main Query Handler ──────────────────────────────────────────────────────

async function handleQuery(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return errorResponse("Invalid JSON body");
  }

  const question = body.q;

  // If structured params already provided, skip LLM
  let params = body.source ? body : null;

  if (!params && question) {
    // Step 1: LLM parse
    const parsed = await parseQuestion(env, question);
    if (!parsed || !parsed.params || !validateParams(parsed.params)) {
      return errorResponse("I couldn't understand that. Try the filters below, or rephrase your question.");
    }
    params = parsed.params;
  }

  if (!params) {
    return errorResponse("No question or parameters provided.");
  }

  console.log("Parsed params:", JSON.stringify(params));

  // Step 2: Route to data source
  let result = null;
  let source = params.source || "precomputed";

  try {
    // Lookup intent — always live
    if (params.intent === "lookup" && params.ticket_id) {
      const ticket = await fetchOpen311Ticket(env, params.ticket_id);
      result = formatLookupResult(ticket);
      source = "open311";
    }
    // Socrata
    else if (source === "socrata" || params.socrata_dataset) {
      const soql = buildSoQL(params);
      const dataset = params.socrata_dataset || "fdj4-gpfu";
      const queryParams = new URLSearchParams(soql);
      if (env.AUSTINAPIKEY) queryParams.set("$$app_token", env.AUSTINAPIKEY);

      const url = `${SOCRATA_BASE}/${dataset}.json?${queryParams.toString()}`;
      const resp = await fetch(url, {
        headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
      });

      if (!resp.ok) {
        return errorResponse("Socrata data is temporarily unavailable. Try a 311 question instead.");
      }

      const socrataData = await resp.json();
      const entries = Array.isArray(socrataData) ? socrataData : [];
      const rowCount = (row) => parseInt(row.count) || row.cnt || 1;
      const total = entries.reduce((sum, row) => sum + rowCount(row), 0);

      // Human-readable month labels: "2025-07-01T00:00:00.000" → "Jul 2025"
      const fmtMonth = (iso) => {
        const d = new Date(iso);
        return isNaN(d) ? iso : d.toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
      };
      const isMonthly = params.group_by === "month" && entries.length > 0 && entries[0].month;

      const scopeBits = [
        params.crime_type ? `${params.crime_type} reports` : "records",
        params.district ? `in district ${params.district}` : null,
        isMonthly && entries.length ? `since ${fmtMonth(entries[0].month)}` : null,
      ].filter(Boolean);

      const rows = entries.map((row) => ({
        ...row,
        month: row.month ? fmtMonth(row.month) : row.month,
      }));

      const counts = entries.map(rowCount);
      const peakIdx = counts.indexOf(Math.max(...counts));
      const peakLabel = isMonthly && peakIdx >= 0 ? ` Peak: ${rows[peakIdx].month} (${counts[peakIdx]}).` : "";

      result = {
        answer_summary: `Found ${total} ${scopeBits.join(" ")}.${peakLabel}`,
        table_data: rows,
        chart_config: isMonthly ? {
          type: "line",
          data: {
            labels: rows.map((row) => row.month),
            datasets: [{
              label: params.crime_type ? `${params.crime_type} reports` : "Count",
              data: counts,
              borderColor: "#3b82f6",
              tension: 0.3,
              fill: false,
            }],
          },
        } : (entries.length <= 10 ? {
          type: "bar",
          data: {
            labels: entries.map((row) => row.council_district ? `D${row.council_district}` : (row.crime_type || "Entry")),
            datasets: [{ label: "Count", data: counts, backgroundColor: "#3b82f6" }],
          },
        } : null),
      };
    }
    // Open311 live query (recent data)
    else if ((source === "open311") && params.intent === "count") {
      const records = await fetchOpen311(env, params);
      if (records === null) {
        // Fall back to pre-computed
        source = "precomputed";
      } else {
        result = formatCountResult(records, params);
      }
    }
    // Pre-computed data (historical, trends, resolution, hotspots)
    else {
      source = "precomputed";
      const filename = params.intent === "resolution_time"
        ? "resolution_stats.json"
        : "daily_counts.json";

      const precomputed = await fetchPrecomputed(env, filename);
      if (!precomputed) {
        return errorResponse("Query data is temporarily unavailable. Try again later.");
      }

      result = formatFromPrecomputed(precomputed, params);
      if (!result) {
        return errorResponse("I don't have data for that specific query. Try one of the following: counts by category, resolution times, or district breakdowns.");
      }
    }
  } catch (e) {
    console.error("Query execution error:", e);
    return errorResponse("Something went wrong processing your query. Please try again.");
  }

  return jsonResponse({
    query: question || "",
    ...result,
    source,
    freshness: "daily",
  });
}

// ── Socrata Proxy Handler ───────────────────────────────────────────────────

async function handleSocrataProxy(request, env, datasetId) {
  // Pass through all query parameters the client sent
  const url = new URL(request.url);
  const socrataUrl = `${SOCRATA_BASE}/${datasetId}.json`;
  const queryParams = new URLSearchParams(url.search);

  // Verify dataset is in our list (but allow any since user chose open pass-through)
  if (env.AUSTINAPIKEY) {
    queryParams.set("$$app_token", env.AUSTINAPIKEY);
  }

  try {
    const resp = await fetch(`${socrataUrl}?${queryParams.toString()}`, {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    const data = await resp.json();
    return jsonResponse(data, resp.status);
  } catch (e) {
    return errorResponse("Socrata proxy error", true, 502);
  }
}

// ── Open311 Proxy Handler ───────────────────────────────────────────────────

async function handleOpen311Proxy(request, env) {
  const url = new URL(request.url);
  const queryParams = new URLSearchParams(url.search);

  if (env.AUSTINAPIKEY) {
    queryParams.set("$$app_token", env.AUSTINAPIKEY);
  }

  try {
    const resp = await fetch(`${OPEN311_BASE}/requests.json?${queryParams.toString()}`, {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    const data = await resp.json();
    return jsonResponse(data, resp.status);
  } catch (e) {
    return errorResponse("Open311 proxy error", true, 502);
  }
}

// ── Router ──────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }

    const headers = corsHeaders(request);

    // Rate limiting
    if (!checkRateLimit(request)) {
      return jsonResponse({ error: "Too many requests. Please wait a moment.", fallback: true }, 429, headers);
    }

    // ── Route: POST /api/v1/query ─────────────────────────────────────
    if (path === "/api/v1/query" && request.method === "POST") {
      const response = await handleQuery(request, env);
      // Merge CORS headers
      for (const [k, v] of Object.entries(headers)) {
        response.headers.set(k, v);
      }
      return response;
    }

    // ── Route: GET /api/v1/socrata/:id ────────────────────────────────
    const socrataMatch = path.match(/^\/api\/v1\/socrata\/([a-z0-9-]+)$/);
    if (socrataMatch && request.method === "GET") {
      const response = await handleSocrataProxy(request, env, socrataMatch[1]);
      for (const [k, v] of Object.entries(headers)) {
        response.headers.set(k, v);
      }
      return response;
    }

    // ── Route: GET /api/v1/open311/:id (single ticket) ────────────────
    const ticketMatch = path.match(/^\/api\/v1\/open311\/([\w-]+)$/);
    if (ticketMatch && request.method === "GET") {
      const ticket = await fetchOpen311Ticket(env, ticketMatch[1]);
      const response = jsonResponse(formatLookupResult(ticket));
      for (const [k, v] of Object.entries(headers)) {
        response.headers.set(k, v);
      }
      return response;
    }

    // ── Route: GET /api/v1/open311 (list) ─────────────────────────────
    if (path === "/api/v1/open311" && request.method === "GET") {
      const response = await handleOpen311Proxy(request, env);
      for (const [k, v] of Object.entries(headers)) {
        response.headers.set(k, v);
      }
      return response;
    }

    // ── 404 ───────────────────────────────────────────────────────────
    return new Response("Not Found", { status: 404, headers });
  },
};
