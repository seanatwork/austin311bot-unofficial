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
  bicycle: ["PWBICYCL", "OBSTMIDB", "SBDEBROW", "ATCOCIRW", "ZZARSTSW"],
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
  water_storm: "Water & Drainage",
  parks: "Parks",
  homeless: "Homeless Encampments",
};

/** Site category names that the ranking stores under a different key. */
export const TROUBLESPOT_ALIASES = { animal: "animals", storm: "water_storm" };

/** Resolve any category name to an address-ranking key, or null. */
export function troublespotKey(category) {
  if (!category) return null;
  const key = TROUBLESPOT_ALIASES[category] || category;
  return TROUBLESPOT_LABELS[key] ? key : null;
}

export const SERVICE_CODE_NAMES = {
  // Homeless
  PRGRDISS: "Park Maintenance",
  ATCOCIRW: "Construction in ROW",
  OBSTMIDB: "Obstruction in ROW",
  SBDEBROW: "Debris in Street",
  DRCHANEL: "Channels/Drainage",
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
  PRBLDPLB: "Park Plumbing",
  PRBLDISS: "Park Building",
  PRBLDACH: "Park ADA",
  PRBLDELE: "Park Electric",
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
  ATTRSIMO: "Traffic Sign",
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
  ROADMARK: "Road Markings/Striping",
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
  "intent": "count" | "trend" | "hotspots" | "rank" | "compare" | "resolution_time" | "lookup",
  "source": "open311" | "socrata" | "precomputed",
  "category": "graffiti" | "parking" | "noise" | "homeless" | "animal" | "traffic" | "parks" | "storm" | "bicycle" | "dead_animal" | "water" | "trash" | "code_violations" | "abandoned_vehicles" | "crime" | "911" | "restaurants" | null,
  "service_codes": ["HHSGRAFF"] or null,
  "socrata_dataset": "fdj4-gpfu" or null,
  "date_range": {
    "start": "last_30d" | "last_90d" | "last_365d" | "this_year" | "last_month" | "last_week" | "today" | "YYYY-MM-DD",
    "end": "YYYY-MM-DD" | null
  },
  "district": 1-10 or null,
  "crime_type": string or null,  // only for fdj4-gpfu: e.g. "burglary", "theft", "assault", "robbery"
  "status": "open" | "closed" | null,
  "group_by": "day" | "month" | "category" | "district" | "status" | "address" | null,
  "limit": number or null,
  "address": string or null,
  "ticket_id": string or null
}

AVAILABLE CATEGORIES AND SERVICE CODES:
${categoryEntries}

SOCRATA DATASETS:
- fdj4-gpfu: APD Crime Reports (for crime questions)
- 22de-7rzg: 911 Dispatch (for 911 response time questions)
- ecmv-9xxi: Restaurant Inspections (for restaurant health score questions)
- t99n-5ib4: Hate Crime Incidents
- tyfh-5r8s: MetroBike Trips
- y2wy-tgr5: Crash Reports
- dx9v-zd7x: Real-Time Traffic Incidents

DATE RANGES: Use symbolic names ("last_30d", "last_90d", "last_365d", "this_year", "last_month", "last_week", "today") unless the question specifies an exact date.

ADDRESS RANKINGS: for "where", "which addresses", "top locations" or "troublespots" questions, return
"source": "precomputed" with "group_by": "address". The category may be any listed type — trash,
code_violations and abandoned_vehicles exist ONLY here (they have no Open311 codes). Omit category
for all 311 activity together. Only 90-day and 365-day windows exist; anything shorter becomes 90 days.

EXAMPLES:
Q: "How many graffiti complaints in the last 30 days?"
A: {"intent":"count","source":"open311","category":"graffiti","service_codes":["HHSGRAFF"],"date_range":{"start":"last_30d"}}

Q: "Which council district has the most open potholes?"
A: {"intent":"hotspots","source":"open311","category":"traffic","service_codes":["SBPOTREP"],"date_range":{"start":"last_90d"},"group_by":"district","status":"open"}

Q: "Show me the trend of violent crime over the past year"
A: {"intent":"trend","source":"socrata","socrata_dataset":"fdj4-gpfu","date_range":{"start":"last_365d"},"group_by":"month"}

Q: "Burglary trends in district 10"
A: {"intent":"trend","source":"socrata","socrata_dataset":"fdj4-gpfu","crime_type":"burglary","district":10,"date_range":{"start":"last_365d"},"group_by":"month"}

Q: "What's the status of ticket 26-00229538?"
A: {"intent":"lookup","source":"open311","ticket_id":"26-00229538"}

Q: "How fast are potholes getting fixed?"
A: {"intent":"resolution_time","source":"open311","category":"traffic","service_codes":["SBPOTREP"]}

Q: "Are noise complaints going up or down compared to last summer?"
A: {"intent":"trend","source":"open311","category":"noise","service_codes":["APDNONNO","DSOUCVMC","AFDFIREW"],"date_range":{"start":"last_365d"},"group_by":"month"}

Q: "Compare parking enforcement in district 5 vs district 7"
A: {"intent":"compare","source":"open311","category":"parking","service_codes":["PARKINGV"],"date_range":{"start":"last_90d"},"group_by":"district"}

Q: "How many tickets are currently open right now, by department?"
A: {"intent":"count","source":"precomputed","category":null,"group_by":"category","status":"open"}

Q: "How many open pothole tickets are there?"
A: {"intent":"count","source":"precomputed","category":"traffic","service_codes":["SBPOTREP"],"status":"open"}

Q: "How many tickets have been open for more than 30 days, by type?"
A: {"intent":"count","source":"precomputed","category":null,"group_by":"category","status":"open"}

Q: "Which agency closes tickets the fastest?"
A: {"intent":"resolution_time","source":"precomputed","category":null}

Q: "Where are the 311 troublespots?" / "What addresses get the most complaints?"
A: {"intent":"rank","source":"precomputed","group_by":"address","date_range":{"start":"last_365d"}}

Q: "Which addresses have the most graffiti complaints?"
A: {"intent":"rank","source":"precomputed","category":"graffiti","group_by":"address","date_range":{"start":"last_90d"}}

Q: "Top 10 addresses for trash complaints this year"
A: {"intent":"rank","source":"precomputed","category":"trash","group_by":"address","date_range":{"start":"last_365d"}}

Return ONLY the JSON object, no markdown, no explanation.`;
}
