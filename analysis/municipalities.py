"""Population of Catalan municipalities, used as a services proxy.

A town's size is what decides whether it has a supermarket chain, a health
centre, a school and a train — and therefore whether a flat there lets
easily and sells again without waiting a year. Mercadona, as a rule of
thumb, opens above roughly 15.000–20.000 inhabitants.

Figures are approximate (2023 padrón, rounded to the nearest thousand) and
are used for threshold comparisons only, never displayed as fact. A
municipality missing from this table is treated as unknown, and the caller
decides whether unknown means "too small" — see `population_of`.
"""

# Rounded to the thousand: precision beyond that would be false, and every
# use here is a comparison against a threshold in the tens of thousands.
POPULATION = {
    # Barcelonès
    "barcelona": 1_660_000,
    "hospitalet de llobregat": 265_000,
    "badalona": 223_000,
    "santa coloma de gramenet": 119_000,
    "sant adria de besos": 37_000,
    "montgat": 12_000,
    "tiana": 9_000,
    # Vallès Occidental
    "terrassa": 224_000,
    "sabadell": 216_000,
    "rubi": 78_000,
    "cerdanyola del valles": 57_000,
    "ripollet": 39_000,
    "montcada i reixac": 36_000,
    "barbera del valles": 33_000,
    "badia del valles": 13_000,
    "palau solita i plegamans": 15_000,
    "polinya": 8_000,
    "sentmenat": 10_000,
    "matadepera": 10_000,
    "viladecavalls": 8_000,
    "ullastrell": 2_000,
    "sant cugat del valles": 93_000,
    "castellar del valles": 24_000,
    "sant quirze del valles": 20_000,
    # Baix Llobregat
    "cornella de llobregat": 88_000,
    "sant boi de llobregat": 82_000,
    "viladecans": 66_000,
    "el prat de llobregat": 65_000,
    "castelldefels": 67_000,
    "gava": 47_000,
    "esplugues de llobregat": 46_000,
    "sant feliu de llobregat": 45_000,
    "sant vicenc dels horts": 28_000,
    "martorell": 29_000,
    "sant andreu de la barca": 28_000,
    "molins de rei": 26_000,
    "sant joan despi": 34_000,
    "sant just desvern": 20_000,
    "olesa de montserrat": 24_000,
    "esparreguera": 22_000,
    "abrera": 12_000,
    "corbera de llobregat": 15_000,
    "cervello": 9_000,
    "pallejà": 11_000,
    "collbato": 5_000,
    "sant esteve sesrovires": 8_000,
    "la palma de cervello": 3_000,
    "torrelles de llobregat": 6_000,
    "begues": 7_000,
    "vallirana": 15_000,
    "castellvi de rosanes": 2_000,
    # Maresme
    "mataro": 129_000,
    "premia de mar": 28_000,
    "el masnou": 23_000,
    "pineda de mar": 28_000,
    "calella": 19_000,
    "malgrat de mar": 18_000,
    "vilassar de mar": 21_000,
    "arenys de mar": 16_000,
    "argentona": 13_000,
    "premia de dalt": 11_000,
    "vilassar de dalt": 9_000,
    "cabrera de mar": 5_000,
    "teia": 6_000,
    "alella": 10_000,
    "canet de mar": 15_000,
    "sant pol de mar": 5_000,
    "caldes d estrac": 3_000,
    "santa susanna": 3_000,
    "tordera": 17_000,
    "sant andreu de llavaneres": 11_000,
    "mataro maresme": 129_000,
    # Vallès Oriental
    "granollers": 62_000,
    "mollet del valles": 51_000,
    "parets del valles": 19_000,
    "la garriga": 17_000,
    "cardedeu": 19_000,
    "sant celoni": 18_000,
    "canoves i samalus": 3_000,
    "llinars del valles": 10_000,
    "caldes de montbui": 18_000,
    "les franqueses del valles": 20_000,
    "santa perpetua de mogoda": 26_000,
    "montornes del valles": 17_000,
    "la roca del valles": 11_000,
    "sant fost de campsentelles": 9_000,
    # Garraf / Alt Penedès
    "vilanova i la geltru": 67_000,
    "sitges": 30_000,
    "vilafranca del penedes": 40_000,
    "cubelles": 16_000,
    # Rest of Catalonia, main towns
    "lleida": 141_000,
    "tarragona": 137_000,
    "reus": 107_000,
    "girona": 103_000,
    "manresa": 78_000,
    "figueres": 47_000,
    "vic": 46_000,
    "igualada": 41_000,
    "lloret de mar": 41_000,
    "blanes": 39_000,
    "el vendrell": 38_000,
    "olot": 36_000,
    "cambrils": 34_000,
    "salt": 32_000,
    "salou": 28_000,
    "calafell": 28_000,
    "valls": 25_000,
    "tortosa": 33_000,
    "amposta": 21_000,
    "sant feliu de guixols": 22_000,
    "palafrugell": 23_000,
    "banyoles": 20_000,
    "mollerussa": 15_000,
    "balaguer": 17_000,
    "tarrega": 17_000,
}


# ── Rent-capped municipalities ───────────────────────────────────────────
#
# Catalonia declared these "zones de mercat residencial tensionat" under Ley
# 12/2023, in force since March 2024. In them a new rental contract cannot
# exceed the Generalitat's reference index, and that ceiling is frequently
# below the market average.
#
# This matters more than it looks: rent here is estimated from market
# averages, so in a capped municipality the yield shown is an upper bound
# the buyer may not legally be allowed to charge. The whole investment case
# rests on a rent that might not be lawful.
#
# The declaration is renewed and extended periodically — treat this as a
# flag meaning "check the index before committing", not as legal advice.
TENSIONED = {
    # Barcelonès
    "barcelona", "hospitalet de llobregat", "badalona",
    "santa coloma de gramenet", "sant adria de besos",
    # Baix Llobregat
    "cornella de llobregat", "sant boi de llobregat", "el prat de llobregat",
    "viladecans", "gava", "castelldefels", "esplugues de llobregat",
    "sant feliu de llobregat", "sant joan despi", "sant just desvern",
    "molins de rei", "sant vicenc dels horts", "martorell",
    "sant andreu de la barca", "olesa de montserrat", "esparreguera",
    "pallejà", "corbera de llobregat", "vallirana", "abrera",
    # Vallès Occidental
    "terrassa", "sabadell", "rubi", "cerdanyola del valles", "ripollet",
    "montcada i reixac", "barbera del valles", "sant cugat del valles",
    "castellar del valles", "sant quirze del valles", "badia del valles",
    "santa perpetua de mogoda", "palau solita i plegamans",
    # Vallès Oriental
    "granollers", "mollet del valles", "parets del valles", "cardedeu",
    "la garriga", "montornes del valles", "les franqueses del valles",
    "caldes de montbui", "la roca del valles", "sant celoni",
    # Maresme
    "mataro", "premia de mar", "el masnou", "vilassar de mar", "premia de dalt",
    "arenys de mar", "pineda de mar", "calella", "canet de mar", "malgrat de mar",
    "montgat", "tiana", "alella", "argentona", "vilassar de dalt",
    "sant andreu de llavaneres",
    # Garraf / Penedès
    "vilanova i la geltru", "sitges", "vilafranca del penedes", "cubelles",
    # Other provinces
    "girona", "salt", "figueres", "olot", "blanes", "lloret de mar",
    "banyoles", "palafrugell", "sant feliu de guixols",
    "tarragona", "reus", "cambrils", "salou", "el vendrell", "calafell",
    "valls", "tortosa", "amposta",
    "lleida", "manresa", "vic", "igualada", "balaguer", "tarrega", "mollerussa",
}


def is_tensioned(city: str | None) -> bool:
    """Whether a municipality has a legal cap on new rental contracts."""
    from analysis.profiles import normalise

    if not city:
        return False

    key = normalise(city)
    if key in TENSIONED:
        return True
    return any(name in key or key in name for name in TENSIONED)


def population_of(city: str | None) -> int | None:
    """Population of a municipality, or None when it is not in the table.

    None means unknown, not small — the two must stay distinguishable so a
    town missing from the table is not silently treated as a hamlet.
    """
    from analysis.profiles import normalise

    if not city:
        return None

    key = normalise(city)
    if key in POPULATION:
        return POPULATION[key]

    # Portals write "Hospitalet de Llobregat (L')", "Premià de Mar" and other
    # variants, so fall back to a containment match both ways.
    for name, pop in POPULATION.items():
        if name in key or key in name:
            return pop
    return None
