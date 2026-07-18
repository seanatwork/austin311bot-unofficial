"""Canonical reporting taxonomy: category -> Open311 service codes.

Single source of truth for the reporting/aggregation layer
(`scripts/generate_query_data.py`, `scripts/generate_card_stats.py`).
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
    "animal":   ["ACLONAG", "ACLOANIM", "ACBITE2", "COAACDD", "ACPROPER", "WILDEXPO", "ACINFORM"],
    "graffiti": ["HHSGRAFF"],
    "parks":    ["PRGRDISS", "PRGRDPLB", "PRGRDELC", "PRBLDPLB", "PRBLDISS", "PRBLDACH", "PRBLDELE", "COMPARLN", "PRCEMET1"],
    "storm":    ["SWSSTORM", "DRCHANEL", "DRILID", "DRFLOODG", "DRSSPIPE", "DRFLOODR", "ZZEROSIO", "DRDITCH"],
    "traffic":  ["SBPOTREP", "TRASIGMA", "STREETL2", "SBDEBROW", "ATTRSIMO", "SIGNSTRE", "OBSINTTR", "SBSIDERE", "SBSTRES", "OBSTMIDB", "ZZARSTSW", "DRCHANEL", "ATCOCIRW", "PWTRISRW", "SBGENRL", "SIGNNEWT", "TRASIGNE", "TPPECRNE"],
    "bicycle":  ["PWBICYCL"],
    "dead_animal": ["ZZARDEAC"],
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
}
