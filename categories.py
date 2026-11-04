"""Canonical reporting taxonomy: category -> Open311 service codes.

Single source of truth for the reporting/aggregation layer
(`scripts/generate_query_data.py`).
Individual map packages (bicycle, homeless, traffic, ...) keep their own
code lists tuned to their map's domain — those may be broader than the
reporting taxonomy. For example the bicycle *map* shows five cycling-relevant
right-of-way codes, but the "Bicycle" *category* in reports counts only
PWBICYCL so that cross-category volume comparisons stay honest.

Categories are NOT a partition of the data: the same Open311 ticket can
belong to several categories (e.g. OBSTMIDB counts toward homeless and
traffic). Some categories also apply semantic filters at aggregation time
(homeless is keyword-filtered — see homeless.homeless_bot.is_encampment_report).
"""

CATEGORY_CODES = {
    "homeless": ["PRGRDISS", "ATCOCIRW", "OBSTMIDB", "SBDEBROW", "DRCHANEL"],
    "parking":  ["PARKINGV"],
    "noise":    ["APDNONNO", "DSOUCVMC", "AFDFIREW"],
    "animal":   ["ACLONAG", "ACLOANIM", "ACBITE2", "COAACDD", "ACPROPER", "WILDEXPO", "ACINFORM", "COAACINJ"],
    "graffiti": ["HHSGRAFF"],
    "parks":    ["PRGRDISS", "PRGRDPLB", "PRGRDELC", "PRBLDPLB", "PRBLDISS", "PRBLDACH", "PRBLDELE", "COMPARLN", "PRCEMET1"],
    "storm":    ["SWSSTORM", "DRCHANEL", "DRILID", "DRFLOODG", "DRSSPIPE", "DRFLOODR", "ZZEROSIO", "DRDITCH"],
    "traffic":  ["SBPOTREP", "TRASIGMA", "STREETL2", "SBDEBROW", "ATTRSIMO", "SIGNSTRE", "OBSINTTR", "SBSIDERE", "SBSTRES", "OBSTMIDB", "ZZARSTSW", "DRCHANEL", "ATCOCIRW", "PWTRISRW", "SBGENRL", "SIGNNEWT", "TRASIGNE", "TPPECRNE",
                 "SIGNPARK", "SIGNTRA1", "DOCKMOBI", "SBPAVEFA", "ROADMARK", "TRAFFICC"],
    "bicycle":  ["PWBICYCL"],
    "dead_animal": ["ZZARDEAC"],
    # Austin Water utility reports. WWREPORT is the water-conservation violation
    # code the /water/ map already renders; AWUBEEMT is the general "water
    # related issues" bucket. Kept separate from `storm` (Watershed Protection
    # drainage/flooding) so utility reports aren't miscounted as storm damage.
    "water":    ["AWUBEEMT", "WWREPORT"],
}

# Socrata `xwdj-i9he` ("Austin 311 Public Data") equivalent of each category,
# for the query gateway's live answers. An entry ending in " - *" matches by
# prefix (`starts_with`); anything else is an exact `sr_type_desc`.
#
# Three things to know before editing:
#   * `homeless` has no entry and never will: the category is defined by a
#     keyword filter over `description`/`status_notes`, and Socrata publishes
#     neither. A homeless question has to go to Open311 or the cache.
#   * Totals from this mapping run higher than the Open311-derived ones in
#     `daily_counts.json` — on purpose. Socrata's type taxonomy is finer and
#     covers departments Open311 has no codes for at all (ARR trash, DSD code
#     enforcement, APD vehicle abatement). Don't narrow the prefixes to "fix"
#     that; wider is the point of preferring Socrata.
#   * `trash`, `code_violations` and `abandoned_vehicles` are Socrata-only:
#     they have no Open311 codes, which is why they are not in CATEGORY_CODES.
#     Their type lists match `scripts/generate_address_data.py`, so an ask
#     answer and the address ranking agree.
CATEGORY_SOCRATA_TYPES = {
    "parking":    ["TPW - Parking Violation Enforcement"],
    "noise":      ["DSD - Outdoor Commercial Venue Music Complaint",
                   "AFD - Fireworks Complaint", "ZZ - AFD - Fireworks Complaint"],
    "animal":     ["Animal Protection - *", "Animal Services - *",
                   "Pet Resource Center - *", "ZZ - Pet Resource Center - *"],
    "graffiti":   ["APH - Graffiti Abatement - Public Property",
                   "DSD - Graffiti Abatement - Private Property"],
    "parks":      ["Park Maintenance - *", "PARD - *", "APR (PARD) - *"],
    "storm":      ["WPD - *"],
    "traffic":    ["TPW - *"],
    "bicycle":    ["TPW - Bicycle Issues"],
    "dead_animal": ["ARR - Dead Animal Collection"],
    "water":      ["AW - *"],
    # Socrata-only categories (see note above).
    "trash":      ["ARR - Garbage", "ARR - Bulk", "ARR - Compost", "ARR - Recycling",
                   "ARR - General", "ARR - Brush", "ARR - Household Hazardous Waste"],
    "code_violations": ["DSD - Request Code Officer", "DSD - Follow-Up",
                        "DSD - Short Term Rental Complaint", "DSD - Billboard Complaint"],
    "abandoned_vehicles": ["APD - Vehicle Abatement Report",
                           "APD - Vehicle Abatement Callback Request"],
}

# The TPW prefix covers parking enforcement and bicycle issues, both of which
# are their own category. Excluding them keeps a "traffic complaints" answer in
# the same ballpark as the /traffic/ page instead of roughly doubling it.
CATEGORY_SOCRATA_EXCLUDE = {
    "traffic": ["TPW - Parking Violation Enforcement", "TPW - Bicycle Issues"],
}

CATEGORY_NAMES = {
    "homeless":    "Homeless",
    "parking":     "Parking",
    "noise":       "Noise",
    "animal":      "Animal Services",
    "graffiti":    "Graffiti",
    "parks":       "Parks",
    "storm":       "Storm & Drainage",
    "traffic":     "Traffic",
    "bicycle":     "Bicycle",
    "dead_animal": "Dead Animal",
    "water":       "Water",
}
