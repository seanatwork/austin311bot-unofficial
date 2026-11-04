/**
 * Category → service codes mapping used by the LLM prompt and query router.
 *
 * Maps user-facing category names to Open311 service codes.
 * Also provides the reverse mapping (service code → category + name).
 */

export const CATEGORY_CODES = {
  homeless: ["PRGRDISS", "ATCOCIRW", "OBSTMIDB", "SBDEBROW", "DRCHANEL"],
  parking: ["PARKINGV"],
  noise: ["APDNONNO", "DSOUCVMC", "AFDFIREW"],
  animal: ["ACLONAG", "ACLOANIM", "ACBITE2", "COAACDD", "ACPROPER", "WILDEXPO", "ACINFORM", "COAACINJ"],
  graffiti: ["HHSGRAFF"],
  parks: ["PRGRDISS", "PRGRDPLB", "PRGRDELC", "PRBLDPLB", "PRBLDISS", "PRBLDACH", "PRBLDELE", "COMPARLN", "PRCEMET1"],
  storm: ["SWSSTORM", "DRCHANEL", "DRILID", "DRFLOODG", "DRSSPIPE", "DRFLOODR", "ZZEROSIO", "DRDITCH"],
  traffic: ["SBPOTREP", "TRASIGMA", "STREETL2", "SBDEBROW", "ATTRSIMO", "SIGNSTRE", "OBSINTTR", "SBSIDERE", "SBSTRES", "OBSTMIDB", "ZZARSTSW", "DRCHANEL", "ATCOCIRW", "PWTRISRW", "SBGENRL", "SIGNNEWT", "TRASIGNE", "TPPECRNE", "SIGNPARK", "SIGNTRA1", "DOCKMOBI", "SBPAVEFA", "ROADMARK", "TRAFFICC"],
  // Canonical reporting category: PWBICYCL only. The bicycle *map* also shows
  // OBSTMIDB/SBDEBROW/ATCOCIRW/ZZARSTSW (cycling-relevant ROW codes), but those
  // over-count the category — see AGENTS.md.
  bicycle: ["PWBICYCL"],
  dead_animal: ["ZZARDEAC"],
  water: ["AWUBEEMT", "WWREPORT"],
};

export const CATEGORY_NAMES = {
  homeless: "Homeless",
  parking: "Parking",
  noise: "Noise",
  animal: "Animal Services",
  graffiti: "Graffiti",
  parks: "Parks",
  storm: "Storm & Drainage",
  traffic: "Traffic",
  bicycle: "Bicycle",
  dead_animal: "Dead Animal",
  water: "Water",
};

/**
 * Troublespot categories for the address ranking
 * (docs/querystore/address_counts.json, written by scripts/generate_address_data.py).
 *
 * Keys mirror the `by_category` keys in that file. They are NOT all Open311
 * categories: trash, code_violations and abandoned_vehicles have no Open311
 * service codes at all — that volume only exists in Socrata, which is why the
 * ranking exists. Never add them to CATEGORY_CODES.
 */
export const TROUBLESPOT_LABELS = {
  graffiti: "Graffiti",
  abandoned_vehicles: "Abandoned Vehicles",
  parking: "Parking",
  trash: "Trash & Dumping",
  code_violations: "Code Violations",
  noise: "Noise",
  animals: "Animals",
  traffic: "Traffic & Streets",
  water: "Water",
  storm: "Storm & Drainage",
  parks: "Parks",
  homeless: "Homeless Encampments",
};

/** Site category names that the ranking stores under a different key. */
export const TROUBLESPOT_ALIASES = { animal: "animals" };

/** Resolve any category name to an address-ranking key, or null. */
export function troublespotKey(category) {
  if (!category) return null;
  const key = TROUBLESPOT_ALIASES[category] || category;
  return TROUBLESPOT_LABELS[key] ? key : null;
}

/**
 * Socrata `xwdj-i9he` equivalent of each category — see CATEGORY_SOCRATA_TYPES
 * in categories.py, which owns this list. An entry ending in " - *" is a prefix
 * match; anything else is an exact `sr_type_desc`.
 *
 * `homeless` is deliberately absent: the category is a keyword filter over
 * `description` and `status_notes`, neither of which Socrata publishes.
 * `trash`, `code_violations` and `abandoned_vehicles` are Socrata-only.
 */
export const CATEGORY_SOCRATA_TYPES = {
  parking: ["TPW - Parking Violation Enforcement"],
  noise: [
    "DSD - Outdoor Commercial Venue Music Complaint",
    "AFD - Fireworks Complaint",
    "ZZ - AFD - Fireworks Complaint",
  ],
  animal: [
    "Animal Protection - *",
    "Animal Services - *",
    "Pet Resource Center - *",
    "ZZ - Pet Resource Center - *",
  ],
  graffiti: [
    "APH - Graffiti Abatement - Public Property",
    "DSD - Graffiti Abatement - Private Property",
  ],
  parks: ["Park Maintenance - *", "PARD - *", "APR (PARD) - *"],
  storm: ["WPD - *"],
  traffic: ["TPW - *"],
  bicycle: ["TPW - Bicycle Issues"],
  dead_animal: ["ARR - Dead Animal Collection"],
  water: ["AW - *"],
  trash: [
    "ARR - Garbage",
    "ARR - Bulk",
    "ARR - Compost",
    "ARR - Recycling",
    "ARR - General",
    "ARR - Brush",
    "ARR - Household Hazardous Waste",
  ],
  code_violations: [
    "DSD - Request Code Officer",
    "DSD - Follow-Up",
    "DSD - Short Term Rental Complaint",
    "DSD - Billboard Complaint",
  ],
  abandoned_vehicles: [
    "APD - Vehicle Abatement Report",
    "APD - Vehicle Abatement Callback Request",
  ],
};

/** Types a category's prefix sweep should not include (see categories.py). */
export const CATEGORY_SOCRATA_EXCLUDE = {
  traffic: ["TPW - Parking Violation Enforcement", "TPW - Bicycle Issues"],
};

/**
 * The `homeless` category is not a set of service codes — it is a keyword
 * filter over the ticket's own text, applied by the map, the weekly digest and
 * the Open311 cache. `homeless/homeless_bot.py` owns these lists; this copy is
 * checked by `scripts/check_taxonomy_sync.py`.
 */
export const ENCAMPMENT_KEYWORDS = [
  "encampment",
  "homelessness",
  "homeless camp",
  "homeless",
  "camp",
  "tent",
  "transient",
  "vagrant",
];

/** Fires on the city's `status_notes` when a report was routed to the HSO. */
export const HSO_KEYWORDS = ["homeless strategy", "hso"];

/** Trash/debris only counts alongside an explicit homeless keyword. */
export const TRASH_KEYWORDS = ["trash", "debris", "garbage"];

/** Whole-word (or phrase) match, mirroring Python's `\b...\b`. */
function wordIn(keyword, text) {
  const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\b${escaped}\\b`).test(text);
}

/**
 * Port of `homeless.homeless_bot.is_encampment_report`: does this Open311
 * record actually belong to the homeless category? Used when showing ticket
 * text, where answering with unfiltered park-maintenance posters would be
 * wrong.
 */
export function isEncampmentReport(record) {
  const citizenText = [record.description, record.address]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  const statusText = (record.status_notes || "").toLowerCase();
  const fullText = `${citizenText} ${statusText}`;

  if (!fullText.trim()) return false;
  if (ENCAMPMENT_KEYWORDS.some((kw) => wordIn(kw, fullText))) return true;
  if (HSO_KEYWORDS.some((kw) => wordIn(kw, statusText))) return true;

  // The city sometimes writes status_notes with asterisks between words
  // ("Homeless* *Strategy* *Office"), which breaks the word boundary.
  if (statusText.replace(/\*/g, " ").includes("homeless strategy")) return true;

  const hasTrash = TRASH_KEYWORDS.some((kw) => wordIn(kw, fullText));
  return hasTrash && wordIn("homeless", fullText);
}

/**
 * SoQL predicate for one category's 311 types, or null when Socrata cannot
 * answer it (only `homeless`, which has no type equivalent).
 */
export function socrataTypePredicate(category) {
  const entries = CATEGORY_SOCRATA_TYPES[category];
  if (!entries) return null;

  const quote = (s) => s.replace(/'/g, "''");

  // A trailing "*" marks a prefix match; the text before it is passed to
  // `starts_with` verbatim, trailing space included ("TPW - " not "TPW -").
  const includes = entries.map((entry) =>
    entry.endsWith("*")
      ? `starts_with(sr_type_desc,'${quote(entry.slice(0, -1))}')`
      : `sr_type_desc = '${quote(entry)}'`
  );
  const predicate = includes.length === 1 ? includes[0] : `(${includes.join(" OR ")})`;

  const excluded = CATEGORY_SOCRATA_EXCLUDE[category] || [];
  if (!excluded.length) return `(${predicate})`;

  const list = excluded.map((t) => `'${quote(t)}'`).join(",");
  return `(${predicate} AND NOT sr_type_desc in(${list}))`;
}

export const SERVICE_CODE_NAMES = {
  // Homeless
  PRGRDISS: "Park Maintenance - Grounds",
  ATCOCIRW: "Construction in Right of Way",
  OBSTMIDB: "Obstruction in Right of Way",
  SBDEBROW: "Debris in Street",
  DRCHANEL: "Channels/Creeks/Drainage",
  // Parking
  PARKINGV: "Parking Violation",
  // Noise
  APDNONNO: "Noise Complaint",
  DSOUCVMC: "Outdoor Music Venue",
  AFDFIREW: "Fireworks",
  // Animal
  ACLONAG: "Loose Dog",
  ACLOANIM: "Loose Animal",
  ACBITE2: "Animal Bite",
  COAACDD: "Vicious Dog",
  ACPROPER: "Animal Care",
  WILDEXPO: "Wildlife",
  ACINFORM: "Animal Protection",
  ACCOYTE: "Coyote Sighting",
  COAACINJ: "Injured / Sick Animal",
  // Graffiti
  HHSGRAFF: "Graffiti",
  // Parks
  PRGRDPLB: "Park Playgrounds",
  PRGRDELC: "Park Electrical",
  PRBLDPLB: "Park Building Plumbing",
  PRBLDISS: "Park Building Issue",
  PRBLDACH: "Park Building ADA",
  PRBLDELE: "Park Building Electric",
  COMPARLN: "Park Comparable",
  PRCEMET1: "Cemetery",
  // Storm
  SWSSTORM: "Storm Debris",
  DRILID: "Drainage Inlet",
  DRFLOODG: "Flooding",
  DRSSPIPE: "Storm Pipe",
  DRFLOODR: "Flood Risk",
  ZZEROSIO: "Erosion",
  DRDITCH: "Ditch",
  // Traffic
  SBPOTREP: "Pothole",
  TRASIGMA: "Traffic Signal",
  STREETL2: "Street Light",
  ATTRSIMO: "Traffic Sign Maintenance",
  SIGNSTRE: "Street Sign",
  OBSINTTR: "Traffic Obstruction",
  SBSIDERE: "Sidewalk Repair",
  SBSTRES: "Street Repair",
  ZZARSTSW: "Street Sweeping",
  PWTRISRW: "Tree in ROW",
  SBGENRL: "Street Misc",
  SIGNNEWT: "New Sign Request",
  TRASIGNE: "Traffic Signal New",
  TPPECRNE: "Traffic Calming",
  SIGNPARK: "Parking Sign Maintenance",
  SIGNTRA1: "Traffic Sign Maintenance",
  DOCKMOBI: "Shared Micromobility",
  SBPAVEFA: "Pavement Failure",
  ROADMARK: "Road Markings/Striping - Maintenance",
  TRAFFICC: "Speed Management",
  // Bicycle
  PWBICYCL: "Bicycle Issue",
  // Dead Animal
  ZZARDEAC: "Dead Animal",
  // Water
  AWUBEEMT: "Water Related Issues",
  WWREPORT: "Water Conservation Violation",
};

/** All known Socrata datasets (whitelisted for the proxy). */
export const SOCRATA_DATASETS = [
  "xwdj-i9he",   // Austin 311 Public Data (all 311 volume, not just Open311)
  "fdj4-gpfu",   // APD Crime Reports
  "t99n-5ib4",   // Hate Crime Incidents
  "i7fg-wrk5",   // NIBRS Homicides
  "ecmv-9xxi",   // Restaurant Inspections
  "tyfh-5r8s",   // MetroBike Trips
  "dx9v-zd7x",   // Real-Time Traffic Incidents
  "y2wy-tgr5",   // Crash Reports
  "b4k4-adkb",   // Traffic Cameras
  "22de-7rzg",   // 911 Dispatch
  "wpu4-x69d",   // Real-Time Fire Incidents
  "v5hh-nyr8",   // AFD Fire Incidents 2023-2025
  "3syk-w9eu",   // Building Permits
  "5bb2-gtef",   // Parking Meter Transactions
  "5tye-7ray",   // Surface Water Quality
];

/**
 * Build the LLM system prompt with the full category mapping embedded.
 */
export function buildLLMSystemPrompt() {
  const categoryEntries = Object.entries(CATEGORY_CODES)
    .map(([cat, codes]) => {
      const name = CATEGORY_NAMES[cat] || cat;
      const codeDetails = codes
        .map((c) => `      "${c}" (${SERVICE_CODE_NAMES[c] || c})`)
        .join("\n");
      return `${name} (category: "${cat}"):\n${codeDetails}`;
    })
    .join("\n");

  return `You are a query parser for austin311.com, a site that tracks Austin 311 service requests.
Given a natural language question, extract structured query parameters.
Return ONLY valid JSON with these exact fields:

{
  "intent": "count" | "trend" | "hotspots" | "rank" | "compare" | "resolution_time" | "lookup" | "samples",
  "source": "open311" | "socrata" | "precomputed",
  "category": "graffiti" | "parking" | "noise" | "homeless" | "animal" | "traffic" | "parks" | "storm" | "bicycle" | "dead_animal" | "water" | "trash" | "code_violations" | "abandoned_vehicles" | "crime" | "911" | "restaurants" | null,
  "service_codes": ["HHSGRAFF"] or null,
  "socrata_dataset": "xwdj-i9he" | "fdj4-gpfu" | ... or null,
  "date_range": {
    "start": "last_30d" | "last_90d" | "last_365d" | "this_year" | "last_month" | "last_week" | "today" | "YYYY-MM-DD",
    "end": "YYYY-MM-DD" | null
  },
  "district": 1-10 or null,
  "zip": "78704" or null,
  "crime_type": string or null,  // only for fdj4-gpfu: e.g. "burglary", "theft", "assault", "robbery"
  "status": "open" | "closed" | null,
  "group_by": "day" | "month" | "category" | "district" | "zip" | "status" | "address" | null,
  "limit": number or null,
  "address": string or null,
  "ticket_id": string or null
}

CHOOSING A SOURCE — this is the most important decision:
- "socrata" for ANY 311 question that is about counting: how many, trends over time, rankings,
  by district, by ZIP code, by type, hotspots. Use "socrata_dataset": "xwdj-i9he" and the category.
  Socrata carries all 311 volume (trash, code enforcement, abandoned vehicles included) and is fast.
- "open311" ONLY when the question is about what a report SAYS or how it was CLOSED — the
  reporter's own words (description), the city's closing notes (status_notes), "what did people
  complain about", "how was this resolved", or a specific ticket id. Socrata publishes no text.
- "open311" also for "homeless" questions: that category is a keyword filter over that same text.
- "precomputed" for resolution times and anything asking about a category the data does not cover.
  Address rankings are live Socrata too — see below.

AVAILABLE CATEGORIES AND SERVICE CODES (Open311):
${categoryEntries}

SOCRATA-ONLY CATEGORIES (no Open311 codes — use socrata or the address ranking):
- trash: Garbage, Bulk, Compost, Recycling, General, Brush, Household Hazardous Waste
- code_violations: Request Code Officer, Follow-Up, Short Term Rental, Billboard
- abandoned_vehicles: Vehicle Abatement Report / Callback Request

SOCRATA DATASETS:
- xwdj-i9he: Austin 311 Public Data (every 311 request — use this for 311 counts and trends)
- fdj4-gpfu: APD Crime Reports (for crime questions)
- 22de-7rzg: 911 Dispatch (for 911 response time questions)
- ecmv-9xxi: Restaurant Inspections (for restaurant health score questions)
- t99n-5ib4: Hate Crime Incidents
- tyfh-5r8s: MetroBike Trips
- y2wy-tgr5: Crash Reports
- dx9v-zd7x: Real-Time Traffic Incidents

DATE RANGES: Use symbolic names ("last_30d", "last_90d", "last_365d", "this_year", "last_month", "last_week", "today") unless the question specifies an exact date.

ZIP CODES: "my neighborhood", "in 78704", "by ZIP code" -> set "zip" to the code and/or "group_by": "zip". Only xwdj-i9he has ZIPs.

ADDRESS RANKINGS: for "where", "which addresses", "top locations" or "troublespots" questions, return
the category (or none for all 311 activity) with "group_by": "address" and "source": "socrata" on
"xwdj-i9he". Only "homeless" address rankings use "source": "precomputed" (they need the text filter).

EXAMPLES:
Q: "How many graffiti complaints in the last 30 days?"
A: {"intent":"count","source":"socrata","socrata_dataset":"xwdj-i9he","category":"graffiti","date_range":{"start":"last_30d"}}

Q: "How many potholes were reported in 78704 this year?"
A: {"intent":"count","source":"socrata","socrata_dataset":"xwdj-i9he","category":"traffic","zip":"78704","date_range":{"start":"this_year"}}

Q: "Which ZIP codes have the most trash complaints?"
A: {"intent":"rank","source":"socrata","socrata_dataset":"xwdj-i9he","category":"trash","group_by":"zip","date_range":{"start":"last_90d"}}

Q: "How many abandoned vehicles were reported last month?"
A: {"intent":"count","source":"socrata","socrata_dataset":"xwdj-i9he","category":"abandoned_vehicles","date_range":{"start":"last_month"}}

Q: "Which council district has the most open potholes?"
A: {"intent":"hotspots","source":"socrata","socrata_dataset":"xwdj-i9he","category":"traffic","date_range":{"start":"last_90d"},"group_by":"district","status":"open"}

Q: "What are people saying about the potholes?" / "Show me recent noise complaints"
A: {"intent":"samples","source":"open311","category":"traffic","date_range":{"start":"last_30d"},"limit":25}

Q: "How were the graffiti tickets closed?" / "What did staff write on those reports?"
A: {"intent":"samples","source":"open311","category":"graffiti","date_range":{"start":"last_90d"},"limit":25}

Q: "Show me the trend of violent crime over the past year"
A: {"intent":"trend","source":"socrata","socrata_dataset":"fdj4-gpfu","date_range":{"start":"last_365d"},"group_by":"month"}

Q: "Burglary trends in district 10"
A: {"intent":"trend","source":"socrata","socrata_dataset":"fdj4-gpfu","crime_type":"burglary","district":10,"date_range":{"start":"last_365d"},"group_by":"month"}

Q: "What's the status of ticket 26-00229538?"
A: {"intent":"lookup","source":"open311","ticket_id":"26-00229538"}

Q: "How fast are potholes getting fixed?"
A: {"intent":"resolution_time","source":"precomputed","category":"traffic"}

Q: "Are noise complaints going up or down compared to last summer?"
A: {"intent":"trend","source":"socrata","socrata_dataset":"xwdj-i9he","category":"noise","date_range":{"start":"last_365d"},"group_by":"month"}

Q: "Compare parking enforcement in district 5 vs district 7"
A: {"intent":"compare","source":"socrata","socrata_dataset":"xwdj-i9he","category":"parking","date_range":{"start":"last_90d"},"group_by":"district"}

Q: "How many tickets are currently open right now, by department?"
A: {"intent":"count","source":"socrata","socrata_dataset":"xwdj-i9he","category":null,"group_by":"category","status":"open","date_range":{"start":"last_90d"}}

Q: "How many open pothole tickets are there?"
A: {"intent":"count","source":"socrata","socrata_dataset":"xwdj-i9he","category":"traffic","status":"open","date_range":{"start":"last_90d"}}

Q: "Which agency closes tickets the fastest?"
A: {"intent":"resolution_time","source":"precomputed","category":null}

Q: "Where are the 311 troublespots?" / "What addresses get the most complaints?"
A: {"intent":"rank","source":"socrata","socrata_dataset":"xwdj-i9he","group_by":"address","date_range":{"start":"last_365d"}}

Q: "Which addresses have the most graffiti complaints?"
A: {"intent":"rank","source":"socrata","socrata_dataset":"xwdj-i9he","category":"graffiti","group_by":"address","date_range":{"start":"last_90d"}}

Q: "Top 10 addresses for trash complaints this year"
A: {"intent":"rank","source":"socrata","socrata_dataset":"xwdj-i9he","category":"trash","group_by":"address","date_range":{"start":"last_365d"}}

Q: "Where are people camping?" / "Which addresses get the most homeless reports?"
A: {"intent":"rank","source":"precomputed","category":"homeless","group_by":"address","date_range":{"start":"last_365d"}}

Return ONLY the JSON object, no markdown, no explanation.`;
}
