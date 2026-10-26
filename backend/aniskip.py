"""Czasy czolowki i zakonczenia z AniSkip (api.aniskip.com).

AniSkip identyfikuje serie po MAL ID, a AniList (nasz katalog) podaje je jako
`idMal` — stad `AnimeMeta.mal_id`. Dane sa spolecznosciowe i niepelne, wiec brak
wyniku to norma, nie blad: wszystko tu jest fail-soft i zwraca {} zamiast rzucac.
Tylko biblioteka standardowa (brak dodatkowych zaleznosci — docs.md D4).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

ANISKIP_URL = "https://api.aniskip.com/v1/skip-times/{mal_id}/{episode}?types=op&types=ed"
USER_AGENT = "Ani-Web/1.0 (+https://github.com/FriendsOfOrobas/Ani-web)"
DEFAULT_TIMEOUT = 4.0

# Przedzialy dluzsze niz to uznajemy za blad danych (czolowka ~1-3 min).
MAX_INTERVAL_SECONDS = 600


def parse_skip_times(payload):
    """{"op": (start, end), "ed": (start, end)} z odpowiedzi AniSkip v1.

    Pomija przedzialy nieprawidlowe (koniec <= poczatek, ujemne, absurdalnie
    dlugie) i nieznane typy (np. recap).
    """
    if not isinstance(payload, dict) or not payload.get("found"):
        return {}

    times = {}
    for item in payload.get("results") or []:
        kind = item.get("skip_type")
        interval = item.get("interval") or {}
        if kind not in ("op", "ed") or kind in times:
            continue
        try:
            start = float(interval["start_time"])
            end = float(interval["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < 0 or end <= start or end - start > MAX_INTERVAL_SECONDS:
            continue
        times[kind] = (round(start, 3), round(end, 3))
    return times


def fetch_skip_times(mal_id, episode, timeout=DEFAULT_TIMEOUT, opener=None):
    """Pobiera czasy z AniSkip; {} przy braku MAL ID, bledzie sieci albo braku danych."""
    try:
        mal_id = int(mal_id)
        episode = int(episode)
    except (TypeError, ValueError):
        return {}
    if mal_id <= 0 or episode <= 0:
        return {}

    url = ANISKIP_URL.format(mal_id=mal_id, episode=episode)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # 404 AniSkip znaczy "brak danych dla tego odcinka" — tez tu trafia.
        return {}
    return parse_skip_times(payload)


def _num(value):
    """Liczba bez zbednych zer (1280.484 zostaje, 102.000 -> 102)."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def script_opts(times, prefix="aniweb_skip"):
    """Wartosc dla mpv `script-opts` albo "" gdy nie ma czego przewijac."""
    parts = []
    for kind in ("op", "ed"):
        if kind in times:
            start, end = times[kind]
            parts.append(f"{prefix}-{kind}_start={_num(start)}")
            parts.append(f"{prefix}-{kind}_end={_num(end)}")
    return ",".join(parts)
