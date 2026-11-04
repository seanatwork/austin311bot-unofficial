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

import { buildLLMSystemPrompt, CATEGORY_CODES, CATEGORY_NAMES, SERVICE_CODE_NAMES, SOCRATA_DATASETS, TROUBLESPOT_LABELS, troublespotKey, socrataTypePredicate, isEncampmentReport } from "./categories.js";

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

// Socrata timestamps are naive wall-clock Central time; Open311 returns UTC
// with a Z. Formatting a Socrata window boundary in UTC would shift every
// 30/90/365-day bucket by 5-6 hours and quietly disagree with the cached
// trend pages.
function formatDateCentral(d) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Chicago",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

// ── Socrata dataset shapes ──────────────────────────────────────────────────

// Austin 311 Public Data: every 311 request (ARR/DSD/APD included), not just
// the Open311 subset. This is the default source for 311 counting.
const SOCRATA_311 = "xwdj-i9he";

// Categories that live in other Socrata datasets, never in the 311 one.
const NON_311_CATEGORIES = new Set(["crime", "911", "restaurants"]);

const OPEN_STATUSES = ["Open", "Duplicate (open)", "New", "Pending", "Work In Progress"];
const CLOSED_STATUSES = ["Closed", "Duplicate (closed)", "Closed -Incomplete"];

function statusPredicate(status) {
  const list = status === "open" ? OPEN_STATUSES : CLOSED_STATUSES;
  return `sr_status_desc in(${list.map((s) => `'${s}'`).join(",")})`;
}

function dateField(dataset) {
  if (dataset === SOCRATA_311) return "sr_created_date";
  if (dataset === "fdj4-gpfu") return "occ_date";
  if (dataset === "22de-7rzg") return "response_datetime";
  return "date";
}

/** Column a group_by maps to, or null when the dataset has no such dimension. */
function groupColumn(dataset, groupBy) {
  if (!groupBy) return null;
  if (dataset === SOCRATA_311) {
    return (
      {
        month: "sr_created_date",
        day: "sr_created_date",
        category: "sr_type_desc",
        district: "sr_location_council_district",
        zip: "sr_location_zip_code",
        status: "sr_status_desc",
      }[groupBy] || null
    );
  }
  if (groupBy === "month" || groupBy === "day") return dateField(dataset);
  if (groupBy === "district") return "council_district";
  if (groupBy === "category") return "category";
  return groupBy;
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

  // Only these groupings map to a real column; anything else would be
  // interpolated into $select/$group unchecked.
  const GROUP_BYS = ["day", "month", "category", "district", "zip", "status", "address"];
  if (params.group_by && !GROUP_BYS.includes(params.group_by)) {
    params.group_by = null;
  }

  // Cap the row count: it lands in $limit, and an unbounded value would either
  // time out or return a 50,000-row table the page cannot render.
  if (params.limit !== null && params.limit !== undefined) {
    const n = parseInt(params.limit, 10);
    params.limit = Number.isNaN(n) ? null : Math.min(Math.max(n, 1), 500);
  }

  // "older than N days" — how much backlog has aged past a threshold.
  if (params.min_age_days !== null && params.min_age_days !== undefined) {
    const n = parseInt(params.min_age_days, 10);
    params.min_age_days = Number.isNaN(n) || n <= 0 ? null : Math.min(n, 3650);
  }

  // Validate category if set
  if (params.category && !CATEGORY_CODES[params.category] && !TROUBLESPOT_LABELS[params.category]) {
    // Try fuzzy matching — the LLM might output "pothole" instead of "traffic"
    for (const [cat, codes] of Object.entries(CATEGORY_CODES)) {
      if (params.category.toLowerCase().includes(cat.toLowerCase())) {
        params.category = cat;
        break;
      }
    }
    // If still invalid, clear it
    if (!CATEGORY_CODES[params.category] && !TROUBLESPOT_LABELS[params.category]) {
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

  // Validate ZIP — only a bare five-digit code survives, so it can be
  // interpolated into SoQL without escaping concerns.
  if (params.zip !== null && params.zip !== undefined) {
    const z = String(params.zip).trim();
    params.zip = /^\d{5}$/.test(z) ? z : null;
  }

  // Validate date_range
  if (params.date_range && params.date_range.start) {
    const validStarts = [
      "today", "last_week", "last_30d", "last_90d", "last_365d",
      "this_year", "last_month",
    ];
    const isPreset = validStarts.includes(params.date_range.start);
    const isDate = /^\d{4}-\d{2}-\d{2}$/.test(params.date_range.start);
    // Fall back to 90 days rather than dropping the filter: an unparseable
    // start used to mean "all time", i.e. a silent 12-year scan.
    if (!isPreset && !isDate) {
      params.date_range = { start: "last_90d" };
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
  const { socrata_dataset, date_range, district, zip, group_by, limit } = params;
  const is311 = socrata_dataset === SOCRATA_311;
  const dfield = dateField(socrata_dataset);
  const col = groupColumn(socrata_dataset, group_by);
  const isTimeSeries = group_by === "month" || group_by === "day";

  // Build $select
  let select = "count(*)";
  if (isTimeSeries) {
    const fn = group_by === "month" ? "date_trunc_ym" : "date_trunc_ymd";
    select = `${fn}(${dfield}) as ${group_by}, count(*)`;
  } else if (col) {
    select = `${col}, count(*)`;
  }

  // Build $where
  const resolved = resolveDateRange(date_range);
  const whereParts = [];
  if (resolved) {
    const start = is311
      ? formatDateCentral(resolved.start)
      : formatDateUTC(resolved.start).slice(0, 10);
    whereParts.push(`${dfield} >= '${start}'`);
  }
  if (district) {
    whereParts.push(
      is311
        ? `sr_location_council_district = '${district}'`
        : `council_district = '${district}'`
    );
  }
  // ZIPs only exist on the 311 dataset. Validate here as well as in the handler:
  // the value is interpolated into SoQL, and this is the last place a caller can
  // reach it.
  if (zip && /^\d{5}$/.test(String(zip))) {
    whereParts.push(`sr_location_zip_code = '${zip}'`);
  }
  // A category is expressed as a set of `sr_type_desc` values on the 311
  // dataset — Socrata carries no Open311 service codes.
  if (is311 && params.category) {
    const predicate = socrataTypePredicate(params.category);
    if (predicate) whereParts.push(predicate);
  }
  // "still open after 90 days" is a created-before bound, applied on its own
  // (no lower bound) so a years-old backlog is not silently excluded.
  if (params.min_age_days) {
    const cutoff = new Date(Date.now() - params.min_age_days * 24 * 60 * 60 * 1000);
    const value = is311
      ? formatDateCentral(cutoff)
      : formatDateUTC(cutoff).slice(0, 10);
    whereParts.push(`${dfield} <= '${value}'`);
  }
  if (is311 && params.status) {
    whereParts.push(statusPredicate(params.status));
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

  if (isTimeSeries) {
    query.$group = group_by;
  } else if (col) {
    query.$group = col;
  }

  // Time series must be chronological; rankings sort by volume
  query.$order = isTimeSeries ? group_by : "count DESC";
  query.$limit = limit || 50;

  return query;
}

// ── Open311 Query ───────────────────────────────────────────────────────────

/** One page (max 100) of Open311 records for a code and window. */
async function open311Page(env, code, start, end, status) {
  const queryParams = new URLSearchParams({
    service_code: code,
    start_date: formatDateUTC(start),
    end_date: formatDateUTC(end),
    per_page: "100",
    page: "1",
    status: status === "open" ? "open" : "open,closed",
  });
  if (env.AUSTINAPIKEY) queryParams.set("$$app_token", env.AUSTINAPIKEY);

  try {
    const resp = await fetch(`${OPEN311_BASE}/requests.json?${queryParams.toString()}`, {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    if (!resp.ok) return [];
    const data = await resp.json();
    return Array.isArray(data) ? data : [];
  } catch (e) {
    console.error("Open311 fetch error:", e);
    return [];
  }
}

/**
 * The newest `wanted` records across a category's codes.
 *
 * Two Open311 quirks shape this:
 *   * records come back *oldest first*, so taking the first pages of a 90/365-day
 *     window returns the OLDEST records — the opposite of "what are people
 *     saying lately". Walk 30-day windows backwards from today instead.
 *   * `per_page` caps at 100 and each request takes ~2s, so a 24-code category
 *     (traffic) costs 14s if swept whole. Cap the sweep to the first few codes
 *     (the lists are ordered headline-code first), run them 5 at a time, and
 *     stop as soon as enough records survive `keep`.
 */
async function fetchOpen311Recent(env, codes, wanted, { status, keep } = {}) {
  const selected = codes.slice(0, 8);
  const collected = [];
  const now = Date.now();
  const MONTH = 30 * 24 * 60 * 60 * 1000;
  const CONCURRENCY = 5;

  const kept = () => (keep ? collected.filter(keep) : collected);
  const enough = () => kept().length >= wanted;

  for (let i = 0; i < 6 && !enough(); i++) {
    const end = new Date(now - i * MONTH);
    const start = new Date(end.getTime() - MONTH);

    for (let j = 0; j < selected.length && !enough(); j += CONCURRENCY) {
      const batch = selected.slice(j, j + CONCURRENCY);
      const pages = await Promise.all(
        batch.map((code) => open311Page(env, code, start, end, status))
      );
      for (const page of pages) collected.push(...page);
    }
  }

  const records = kept();
  // Newest first — within a window the API is still oldest-first per code.
  records.sort(
    (a, b) => new Date(b.requested_datetime || 0) - new Date(a.requested_datetime || 0)
  );
  return records;
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

// ── Address ranking (live) ──────────────────────────────────────────────────

// Canonicalization mirrors scripts/generate_address_data.py, so a live answer
// and the pre-computed ranking agree on what one location is. The city stores
// the same corner both ways ("AIRPORT BLVD & E 45TH ST" and its reverse), so
// without sorting the parts the #1 and #2 troublespots are one corner.
const CITY_SUFFIX = /,\s*AUSTIN\s*,\s*TX\b\s*(?:\d{5})?\s*$/i;
const CITY_ONLY = /^AUSTIN\s*,?\s*TX\b.*$/i;
const ROAD_ABBREV = new Set(["Fm", "Rm", "Ih", "Us", "Sh", "Hwy", "Lp"]);

function canonLocation(loc) {
  let s = String(loc || "").trim();
  if (!s) return "";

  s = s
    .replace(CITY_SUFFIX, "")
    .toUpperCase()
    .replace(/\s*&\s*/g, " & ")
    .replace(/\s+/g, " ")
    .replace(/^[\s,]+|[\s,]+$/g, "");

  if (!s || CITY_ONLY.test(s)) return "";

  if (s.includes(" & ")) {
    const parts = s.split(" & ").map((p) => p.trim()).filter(Boolean).sort();
    if (parts.length >= 2) return parts.join(" & ");
  }
  return s;
}

function displayLocation(key) {
  return key
    .toLowerCase()
    .replace(/(^|\s)(\S)/g, (_, lead, ch) => lead + ch.toUpperCase())
    .replace(/\b(\d+)(st|nd|rd|th)\b/g, (_, n, suffix) => n + suffix)
    .split(" ")
    .map((w) => (ROAD_ABBREV.has(w) ? w.toUpperCase() : w))
    .join(" ");
}

/**
 * "Where are the troublespots?" — one `$group sr_location` on Socrata, merged
 * and ranked here. Returns null when Socrata cannot answer it (homeless has no
 * live text to filter on) or the request fails.
 */
async function fetchAddressRanking(env, params) {
  // `homeless` is a keyword filter over description/status_notes.
  const predicate = params.category ? socrataTypePredicate(params.category) : null;
  if (params.category && !predicate) return null;

  const resolved = resolveDateRange(params.date_range);
  const whereParts = ["sr_location IS NOT NULL"];
  if (resolved) {
    whereParts.push(`sr_created_date >= '${formatDateCentral(resolved.start)}'`);
  }
  if (predicate) whereParts.push(predicate);
  if (params.zip) whereParts.push(`sr_location_zip_code = '${params.zip}'`);
  if (params.district) {
    whereParts.push(`sr_location_council_district = '${params.district}'`);
  }

  // Ask for more rows than are shown so canonicalization can merge reversed
  // intersection pairs that both rank just below the cut.
  const queryParams = new URLSearchParams({
    $select: "sr_location, count(*) as n",
    $where: whereParts.join(" AND "),
    $group: "sr_location",
    $order: "n DESC",
    $limit: "500",
  });
  if (env.AUSTINAPIKEY) queryParams.set("$$app_token", env.AUSTINAPIKEY);

  let rows;
  try {
    const resp = await fetch(`${SOCRATA_BASE}/${SOCRATA_311}.json?${queryParams}`, {
      headers: { "Accept": "application/json", "User-Agent": "austin311bot/query-gateway" },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    rows = Array.isArray(data) ? data : [];
  } catch (e) {
    console.error("Socrata address ranking error:", e);
    return null;
  }

  const counts = new Map();
  let blank = 0;
  for (const row of rows) {
    const key = canonLocation(row.sr_location);
    const n = parseInt(row.n, 10) || 0;
    if (!key) {
      blank += n;
      continue;
    }
    counts.set(key, (counts.get(key) || 0) + n);
  }
  if (counts.size === 0) return null;

  const wanted = Math.min(Math.max(parseInt(params.limit, 10) || 25, 1), 50);
  const ranked = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, wanted)
    .map(([key, count]) => ({
      address: displayLocation(key),
      count,
      kind: key.includes(" & ") ? "intersection" : "address",
    }));

  const label = params.category
    ? `${CATEGORY_NAMES[params.category] || TROUBLESPOT_LABELS[params.category] || params.category} requests`
    : "311 requests";
  const place = params.zip
    ? ` in ZIP ${params.zip}`
    : params.district
      ? ` in district ${params.district}`
      : "";
  const top3 = ranked.slice(0, 3).map((e) => `${e.address} (${e.count})`).join(" · ");

  return {
    answer_summary:
      `Most-reported locations for ${label}${place}: ${top3}. ` +
      `Ranked from the ${rows.length} most-reported locations` +
      (blank ? `; ${blank} reports in that set had no usable address.` : "."),
    chart_config: {
      type: "bar",
      data: {
        labels: ranked.slice(0, 12).map((e) => e.address),
        datasets: [
          {
            label: "Reports",
            data: ranked.slice(0, 12).map((e) => e.count),
            backgroundColor: "#C2855C",
          },
        ],
      },
    },
    table_data: ranked,
  };
}

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

// "What are people saying?" / "How were these closed?" — the one thing only
// Open311 can answer, because it exposes `description` (reporter's words) and
// `status_notes` (the city's closing note). Socrata publishes neither.
function formatSamplesResult(records, params) {
  const wanted = params.limit || 25;
  const clip = (s, n) => {
    const text = String(s || "").replace(/\s+/g, " ").trim();
    return text.length > n ? text.slice(0, n - 1) + "…" : text;
  };
  const name = CATEGORY_NAMES[params.category] || TROUBLESPOT_LABELS[params.category] || "311";

  const rows = records.slice(0, wanted).map((r) => ({
    date: (r.requested_datetime || "").slice(0, 10) || "—",
    status: r.status || "—",
    type: r.service_name || r.service_code || "—",
    description: clip(r.description, 160) || "—",
    staff_note: clip(r.status_notes, 160) || "—",
  }));

  const withDescription = rows.filter((r) => r.description !== "—").length;
  const withNote = rows.filter((r) => r.staff_note !== "—").length;
  const verb = (n) => (n === 1 ? "includes" : "include");

  return {
    answer_summary:
      `The ${rows.length} most recent ${name} requests. ` +
      `${withDescription} ${verb(withDescription)} the reporter's own description and ` +
      `${withNote} ${verb(withNote)} a city closing note.`,
    chart_config: null,
    table_data: rows,
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

// ── Address ranking ─────────────────────────────────────────────────────────

// The address dataset carries only 90- and 365-day windows: fold every shorter
// range onto 90d and everything else onto 365d.
const SHORT_RANGES = new Set(["today", "last_week", "last_month", "last_30d", "last_90d"]);

function addressWindow(dateRange) {
  const start = dateRange && dateRange.start;
  return SHORT_RANGES.has(start) ? "90d" : "365d";
}

/**
 * Format one list from address_counts.json — the top reported addresses and
 * intersections for a window, overall or for a single troublespot category.
 *
 * Ranking is by *reported location*, not by offender: the leaders are large
 * apartment complexes and shelters, so the wording stays "most-reported".
 */
function formatAddressRanking(precomputed, params) {
  const key = troublespotKey(params.category);
  if (params.category && !key) return null; // a category this dataset can't rank

  const win = addressWindow(params.date_range);
  const block = (precomputed.windows || {})[win];
  if (!block) return null;

  const entries = (key ? (block.by_category || {})[key] : block.all) || [];
  if (entries.length === 0) return null;

  const top = entries.slice(0, 10);
  const label = key ? TROUBLESPOT_LABELS[key] : "311 requests";
  const period = win === "90d" ? "the last 90 days" : "the last 12 months";

  return {
    answer_summary: `Most-reported locations for ${label} over ${period}: ${top.slice(0, 3).map((e) => `${e.address} (${e.count})`).join(", ")}.`,
    chart_config: {
      type: "bar",
      data: {
        labels: top.map((e) => e.address),
        datasets: [{ label: "Reports", data: top.map((e) => e.count), backgroundColor: "#3b82f6" }],
      },
    },
    table_data: top.map((e) => ({ address: e.address, reports: e.count, kind: e.kind })),
  };
}

function formatFromPrecomputed(precomputed, params) {
  // Route to the right pre-computed dataset based on intent
  const { intent, category, date_range, district, group_by } = params;

  // Address ranking — its own dataset shape and its own category set, so it is
  // checked before the count/rank routing below.
  if (group_by === "address" && precomputed.windows) {
    return formatAddressRanking(precomputed, params);
  }

  if (intent === "count" || intent === "rank") {
    const totals = precomputed.totals_90d || precomputed.totals_365d || {};
    const wantOpen = params.status === "open";
    // A category we know about but have no totals for — e.g. one just added to
    // the taxonomy before the next data refresh — must not fall through to the
    // all-categories breakdown below.
    if (category && !totals[category]) return null;
    if (category) {
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

  // Structured params skip the LLM, but they must not skip validation: the
  // values end up interpolated into SoQL, and a caller can post whatever it
  // likes. `intent` defaults to a plain count.
  if (params === body) {
    params.intent = params.intent || "count";
    if (!validateParams(params)) {
      return errorResponse("Those query parameters are not supported.");
    }
  }

  // "Where are the troublespots?" is just a `$group sr_location`, answered live.
  // `homeless` is the exception: its ranking needs the encampment keyword filter
  // over ticket text, which is applied when the pre-computed file is generated.
  // Crime/911 questions keep their own datasets.
  if (params.group_by === "address" && !NON_311_CATEGORIES.has(params.category)) {
    const homelessRanking = params.category === "homeless";
    params.source = homelessRanking ? "precomputed" : "socrata";
    params.socrata_dataset = homelessRanking ? null : SOCRATA_311;
  }

  // Default a 311-shaped Socrata question onto the 311 dataset. Crime, 911 and
  // restaurant questions name their own dataset and must not be hijacked.
  if (params.source === "socrata" && !params.socrata_dataset && !NON_311_CATEGORIES.has(params.category)) {
    params.socrata_dataset = SOCRATA_311;
  }

  // `homeless` is a keyword filter over description/status_notes, and Socrata
  // publishes neither — likewise any category whose types are unknown there.
  // Those are answered from the cache, which applies the same filters the maps
  // and the weekly digest use.
  if (
    params.socrata_dataset === SOCRATA_311 &&
    params.intent !== "lookup" &&
    params.category &&
    !socrataTypePredicate(params.category)
  ) {
    params.socrata_dataset = null;
    params.source = "precomputed";
  }

  // A plain count always belongs on Socrata: Open311 caps `per_page` at 100, so
  // a live count silently reports "100" for anything bigger. Only the text
  // intents need the Open311 path.
  if (
    params.intent === "count" &&
    params.source !== "precomputed" &&
    params.source !== "socrata" &&
    params.category !== "homeless" &&
    (params.category === null || params.category === undefined || socrataTypePredicate(params.category))
  ) {
    params.source = "socrata";
    params.socrata_dataset = SOCRATA_311;
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
    // Text questions — Open311 is the only source with description/status_notes
    else if (params.intent === "samples") {
      source = "open311";
      const codes = params.service_codes && params.service_codes.length
        ? params.service_codes
        : CATEGORY_CODES[params.category] || null;
      if (!codes) {
        return errorResponse(
          "I don't have ticket text for that. Ask about a specific category, or a ticket number."
        );
      }
      const fetched = await fetchOpen311Recent(
        env,
        codes,
        params.limit || 25,
        {
          status: params.status,
          // `homeless` is a keyword filter over exactly these text fields, so the
          // unfiltered stream is full of unrelated park-maintenance reports.
          keep: params.category === "homeless" ? isEncampmentReport : null,
        }
      );
      if (fetched.length === 0) {
        return errorResponse(
          params.category === "homeless"
            ? "No homeless/encampment reports in the last 6 months. Try a ticket number instead."
            : "No 311 records matched in the last 6 months."
        );
      }

      result = formatSamplesResult(fetched, params);
    }
    // Address rankings need canonicalization the generic SoQL builder cannot
    // do (one location can arrive spelled several ways), so they get their own
    // path — with the pre-computed file as fallback.
    else if (params.group_by === "address" && source === "socrata") {
      result = await fetchAddressRanking(env, params);
      if (result) {
        source = "socrata";
      } else {
        source = "precomputed";
        const precomputed = await fetchPrecomputed(env, "address_counts.json");
        result = precomputed ? formatFromPrecomputed(precomputed, params) : null;
        if (!result) {
          return errorResponse("Query data is temporarily unavailable. Try again later.");
        }
      }
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
      // parseInt("0") is falsy, so a zero-count row must not fall through to 1.
      const rowCount = (row) => {
        const raw = row.count !== undefined ? row.count : row.cnt;
        const n = parseInt(raw, 10);
        return Number.isNaN(n) ? 1 : n;
      };
      const total = entries.reduce((sum, row) => sum + rowCount(row), 0);

      // Human-readable month labels: "2025-07-01T00:00:00.000" → "Jul 2025"
      const fmtMonth = (iso) => {
        const d = new Date(iso);
        return isNaN(d) ? iso : d.toLocaleString("en-US", { month: "short", year: "numeric", timeZone: "UTC" });
      };
      const isMonthly = params.group_by === "month" && entries.length > 0 && entries[0].month;

      // "TPW - Parking Violation Enforcement" → "Parking Violation Enforcement".
      // One type ships an HTML-escaped ampersand, so decode that too.
      const prettyType = (t) =>
        String(t).replace(/^[A-Z() ]{2,6} - /, "").replace(/&amp;/g, "&");

      const rowLabel = (row) => {
        if (row.month) return fmtMonth(row.month);
        if (row.day) return String(row.day).slice(0, 10);
        // Grouped queries can return a null key (e.g. a request with no council
        // district, or one outside city limits) — label it rather than dropping it.
        if (params.group_by === "zip") return `ZIP ${row.sr_location_zip_code || "unknown"}`;
        if (params.group_by === "district") {
          const d = row.sr_location_council_district ?? row.council_district;
          return d === null || d === undefined || d === "" ? "No district" : `District ${d}`;
        }
        if (params.group_by === "status" && row.sr_status_desc) return row.sr_status_desc;
        if (params.group_by === "category" && row.sr_type_desc) return prettyType(row.sr_type_desc);
        if (row.sr_location_zip_code) return `ZIP ${row.sr_location_zip_code}`;
        if (row.sr_location_council_district) return `District ${row.sr_location_council_district}`;
        if (row.council_district) return `District ${row.council_district}`;
        if (row.sr_type_desc) return prettyType(row.sr_type_desc);
        if (row.sr_status_desc) return row.sr_status_desc;
        return row.crime_type || "Total";
      };

      const rows = entries.map((row) => ({ label: rowLabel(row), ...row }));

      const subject = params.crime_type
        ? `${params.crime_type} reports`
        : params.category
          ? `${CATEGORY_NAMES[params.category] || TROUBLESPOT_LABELS[params.category] || params.category} requests`
          : "311 records";

      const scopeBits = [
        subject,
        params.status === "open" ? "still open" : params.status === "closed" ? "closed" : null,
        params.min_age_days ? `and older than ${params.min_age_days} days` : null,
        params.zip ? `in ZIP ${params.zip}` : null,
        params.district ? `in district ${params.district}` : null,
        isMonthly && entries.length ? `since ${fmtMonth(entries[0].month)}` : null,
      ].filter(Boolean);

      const counts = entries.map(rowCount);
      const peakIdx = counts.indexOf(Math.max(...counts));
      const peakLabel = isMonthly && peakIdx >= 0 ? ` Peak: ${rows[peakIdx].label} (${counts[peakIdx]}).` : "";

      // Ranked breakdowns get a bar chart for their top 12; a plain grouped
      // ZIP/type query can return 50 rows, which is unreadable as bars.
      const barRows = rows.slice(0, 12);

      // A capped ranking ($limit) is the top N, not the whole population — the
      // sum of those rows must not be presented as the grand total.
      const limit = params.limit || 50;
      const truncated = Boolean(params.group_by) && entries.length >= limit;
      const groupNoun = {
        zip: "ZIP codes",
        district: "districts",
        category: "request types",
        status: "statuses",
        day: "days",
        month: "months",
      }[params.group_by];

      const scopeText = scopeBits.join(" ");
      const summary = truncated
        ? `Top ${entries.length} ${groupNoun || "groups"} for ${scopeText}: ${total} requests` +
          (entries.length ? `. Leader: ${rows[0].label} (${counts[0]}).` : ".")
        : `Found ${total} ${scopeText}` +
          (!isMonthly && params.group_by && entries.length
            ? ` across ${entries.length} ${groupNoun || "groups"}`
            : "") +
          `.${peakLabel}`;

      result = {
        answer_summary: summary,
        table_data: rows,
        chart_config: isMonthly ? {
          type: "line",
          data: {
            labels: rows.map((row) => row.label),
            datasets: [{
              label: params.crime_type ? `${params.crime_type} reports` : "Count",
              data: counts,
              borderColor: "#3b82f6",
              tension: 0.3,
              fill: false,
            }],
          },
        } : (params.group_by && barRows.length > 1 ? {
          type: "bar",
          data: {
            labels: barRows.map((row) => row.label),
            datasets: [{ label: "Count", data: barRows.map(rowCount), backgroundColor: "#3b82f6" }],
          },
        } : null),
      };
    }
    // No live Open311 counting path on purpose. Open311 caps `per_page` at 100,
    // so counting its records silently answered "100" for anything bigger;
    // counts go to Socrata, and a category Socrata cannot express (homeless)
    // is force-routed to the pre-computed cache above.
    // Pre-computed data (historical, trends, resolution, hotspots)
    else {
      source = "precomputed";
      const filename = params.intent === "resolution_time"
        ? "resolution_stats.json"
        : (params.group_by === "address" ? "address_counts.json" : "daily_counts.json");

      const precomputed = await fetchPrecomputed(env, filename);
      if (!precomputed) {
        return errorResponse("Query data is temporarily unavailable. Try again later.");
      }

      result = formatFromPrecomputed(precomputed, params);
      if (!result) {
        return errorResponse("I don't have data for that specific query. Try one of the following: address rankings, counts by category, resolution times, or district breakdowns.");
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
