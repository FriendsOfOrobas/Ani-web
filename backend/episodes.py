"""Lista odcinkow do wyboru w UI (lista rozwijana zamiast klikania numeru).

Dzis to same numery: AniList nie daje stabilnie tytulow ani opisow odcinkow, a
zrodlo (ani-cli) zna tylko numery. Ksztalt elementu jest jednak od razu taki, jak
bedzie potrzebny pozniej — kazdy odcinek to slownik, w ktorym `number` jest
jedynym wymaganym kluczem, a reszta jest opcjonalna:

    {"number": 3, "title": "...", "description": "...", "thumbnail": "https://...",
     "air_date": "2002-10-17"}

Front pokazuje to, co jest (tytul obok numeru), i ignoruje reszte. Zeby dodac
bogatsze dane, wystarczy przekazac `extra` (numer -> slownik) z nowego dostawcy;
nic poza tym modulem nie musi sie zmieniac. To MIEJSCE NA ROZBUDOWE, nie funkcja:
nic tu nie pobiera tytulow z sieci.
"""

# Gorny bezpieczny limit: najdluzsze serie maja ~1200 odcinkow, a <option> na
# telefonie kosztuje. Ponad limit front dostaje pole liczbowe (pusta lista).
MAX_EPISODES = 3000


def episode_list(meta, extra=None):
    """[{"number": n, ...}] dla wydanych odcinkow serii; [] gdy liczby nie znamy."""
    total = getattr(meta, "aired", 0) or getattr(meta, "episodes", 0) or 0
    if total <= 0 or total > MAX_EPISODES:
        return []

    extra = extra or {}
    items = []
    for number in range(1, int(total) + 1):
        item = {"number": number}
        details = extra.get(number)
        if isinstance(details, dict):
            # `number` zawsze nasze — dostawca nie moze go nadpisac.
            item.update({k: v for k, v in details.items() if k != "number"})
        items.append(item)
    return items
