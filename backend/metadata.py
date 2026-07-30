"""Warstwa metadanych katalogu — Stage 1 (docs.md §5.4).

Zrodlem prawdy jest AniList GraphQL, Jikan v4 jest fallbackiem. Oba bez klucza.
Metadane anime sa niemal statyczne, wiec cache w SQLite jest agresywny.

Ta warstwa jest CELOWO niezalezna od allanime i od ani-cli. Dzieki temu katalog,
opisy i okladki dzialaja nawet wtedy, gdy zrodlo strumieni jest zablokowane —
patrz docs.md D7.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from db import now, transaction

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_URL = "https://api.jikan.moe/v4"

# Metadane anime praktycznie nie zmieniaja sie po emisji. Trzymamy dlugo, zeby
# nie bic w API bez potrzeby (AniList: 90 req/min, Jikan: 60 req/min).
ANIME_TTL_SECONDS = 30 * 24 * 3600
SEARCH_TTL_SECONDS = 24 * 3600

REQUEST_TIMEOUT = 15

# WYMAGANE. Domyslny "Python-urllib/3.13" dostaje od AniList HTTP 403
# (sprawdzone 2026-07-30 — dowolny wlasny UA przechodzi). Niezaleznie od tego
# AniList prosi w dokumentacji o UA identyfikujacy aplikacje.
USER_AGENT = "Ani-Web/0.1 (+https://github.com/FriendsOfOrobas/Ani-web)"

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": USER_AGENT,
}

SEARCH_QUERY = """
query ($q: String, $perPage: Int) {
  Page(perPage: $perPage) {
    media(search: $q, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      synonyms
      description(asHtml: false)
      episodes
      status
      seasonYear
      format
      averageScore
      coverImage { large medium }
      bannerImage
    }
  }
}
"""

DETAILS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    synonyms
    description(asHtml: false)
    episodes
    status
    seasonYear
    format
    averageScore
    coverImage { large medium }
    bannerImage
  }
}
"""

_TAG_RE = re.compile(r"<[^>]+>")
_LATIN_RE = re.compile(r"^[\x20-\x7e]+$")


class MetadataError(RuntimeError):
    """Katalog niedostepny. Komunikat idzie do UI."""


@dataclass
class AnimeMeta:
    anilist_id: int
    romaji: str = ""
    english: str = ""
    native: str = ""
    synonyms: list = field(default_factory=list)
    description: str = ""
    episodes: int = 0
    status: str = ""
    year: int = 0
    format: str = ""
    score: int = 0
    cover: str = ""
    banner: str = ""

    @property
    def title(self):
        return self.romaji or self.english or self.native or f"#{self.anilist_id}"

    def as_dict(self):
        payload = asdict(self)
        payload["title"] = self.title
        return payload


def strip_html(text):
    """AniList zwraca <br> i <i> nawet przy asHtml:false."""
    if not text:
        return ""
    cleaned = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def candidate_titles(meta):
    """Kandydaci na zapytanie do allanime, w kolejnosci prob (docs.md D7).

    Romaji pierwsze, bo allanime katalogujeje glownie po transkrypcji. Synonimy
    filtrujemy do alfabetu lacinskiego — lista AniList zawiera tytuly koreanskie,
    hebrajskie czy tajskie, ktorych allanime nie zna, a kazda proba to osobne
    uruchomienie ani-cli.
    """
    ordered = [meta.romaji, meta.english]
    ordered += [s for s in meta.synonyms if s and _LATIN_RE.match(s)]

    seen = set()
    result = []
    for title in ordered:
        title = (title or "").strip()
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            result.append(title)
    return result


# --- zrodla ---------------------------------------------------------------


def _post_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={**_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url):
    request = urllib.request.Request(url, headers=dict(_HEADERS))
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _meta_from_anilist(node):
    title = node.get("title") or {}
    cover = node.get("coverImage") or {}
    return AnimeMeta(
        anilist_id=node["id"],
        romaji=title.get("romaji") or "",
        english=title.get("english") or "",
        native=title.get("native") or "",
        synonyms=list(node.get("synonyms") or []),
        description=strip_html(node.get("description")),
        episodes=node.get("episodes") or 0,
        status=node.get("status") or "",
        year=node.get("seasonYear") or 0,
        format=node.get("format") or "",
        score=node.get("averageScore") or 0,
        cover=cover.get("large") or cover.get("medium") or "",
        banner=node.get("bannerImage") or "",
    )


class AniListProvider:
    name = "anilist"

    def search(self, query, limit=20):
        try:
            payload = _post_json(
                ANILIST_URL, {"query": SEARCH_QUERY, "variables": {"q": query, "perPage": limit}}
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"AniList nie odpowiada: {exc}") from exc

        if payload.get("errors"):
            raise MetadataError(f"AniList: {payload['errors'][0].get('message', 'blad')}")

        nodes = (((payload.get("data") or {}).get("Page") or {}).get("media")) or []
        return [_meta_from_anilist(node) for node in nodes]

    def details(self, anilist_id):
        try:
            payload = _post_json(
                ANILIST_URL, {"query": DETAILS_QUERY, "variables": {"id": int(anilist_id)}}
            )
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"AniList nie odpowiada: {exc}") from exc

        node = (payload.get("data") or {}).get("Media")
        if not node:
            raise MetadataError(f"AniList nie zna anime o id {anilist_id}")
        return _meta_from_anilist(node)


class JikanProvider:
    """Fallback. Nie ma id AniList, wiec uzywamy mal_id — kolizje sa mozliwe,
    ale to sciezka awaryjna, uzywana tylko gdy AniList nie odpowiada.
    """

    name = "jikan"

    def search(self, query, limit=20):
        url = f"{JIKAN_URL}/anime?q={urllib.parse.quote(query)}&limit={int(limit)}"
        try:
            payload = _get_json(url)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"Jikan nie odpowiada: {exc}") from exc
        return [self._meta(node) for node in payload.get("data") or []]

    def details(self, anilist_id):
        try:
            payload = _get_json(f"{JIKAN_URL}/anime/{int(anilist_id)}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise MetadataError(f"Jikan nie odpowiada: {exc}") from exc
        node = payload.get("data")
        if not node:
            raise MetadataError(f"Jikan nie zna anime o id {anilist_id}")
        return self._meta(node)

    @staticmethod
    def _meta(node):
        titles = {t.get("type"): t.get("title") for t in node.get("titles") or []}
        images = (node.get("images") or {}).get("jpg") or {}
        return AnimeMeta(
            anilist_id=node.get("mal_id") or 0,
            romaji=titles.get("Default") or node.get("title") or "",
            english=titles.get("English") or node.get("title_english") or "",
            native=titles.get("Japanese") or node.get("title_japanese") or "",
            synonyms=list(node.get("title_synonyms") or []),
            description=strip_html(node.get("synopsis")),
            episodes=node.get("episodes") or 0,
            status=(node.get("status") or "").upper().replace(" ", "_"),
            year=node.get("year") or 0,
            format=(node.get("type") or "").upper(),
            score=int((node.get("score") or 0) * 10),
            cover=images.get("large_image_url") or images.get("image_url") or "",
        )


# --- cache + usluga -------------------------------------------------------


class MetadataService:
    """Cache-first katalog. Siec dotykana tylko przy pudle albo przedawnieniu."""

    def __init__(self, db_path, provider=None, fallback=None):
        self.db_path = db_path
        self.provider = provider if provider is not None else AniListProvider()
        self.fallback = fallback if fallback is not None else JikanProvider()

    # --- cache ---

    def _store(self, metas):
        if not metas:
            return
        stamp = now()
        rows = [
            (m.anilist_id, json.dumps(m.as_dict(), ensure_ascii=False), stamp) for m in metas
        ]
        with transaction(self.db_path) as connection:
            connection.executemany(
                "INSERT INTO anime (anilist_id, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(anilist_id) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                rows,
            )

    def _cached_anime(self, anilist_id):
        with transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM anime WHERE anilist_id = ?", (int(anilist_id),)
            ).fetchone()
        if row is None or now() - row["fetched_at"] > ANIME_TTL_SECONDS:
            return None
        return _meta_from_payload(json.loads(row["payload"]))

    def _cached_search(self, query):
        with transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT ids, fetched_at FROM search WHERE query = ?", (query,)
            ).fetchone()
            if row is None or now() - row["fetched_at"] > SEARCH_TTL_SECONDS:
                return None
            ids = json.loads(row["ids"])
            if not ids:
                return []
            placeholders = ",".join("?" * len(ids))
            rows = connection.execute(
                f"SELECT anilist_id, payload FROM anime WHERE anilist_id IN ({placeholders})", ids
            ).fetchall()

        by_id = {r["anilist_id"]: _meta_from_payload(json.loads(r["payload"])) for r in rows}
        # Kolejnosc trafnosci z AniList jest istotna — odtwarzamy ja z listy ids.
        found = [by_id[i] for i in ids if i in by_id]
        return found if len(found) == len(ids) else None

    def _store_search(self, query, metas):
        with transaction(self.db_path) as connection:
            connection.execute(
                "INSERT INTO search (query, ids, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query) DO UPDATE SET ids=excluded.ids, fetched_at=excluded.fetched_at",
                (query, json.dumps([m.anilist_id for m in metas]), now()),
            )

    # --- api ---

    def search(self, query, limit=20):
        query = (query or "").strip()
        if not query:
            return []

        key = query.lower()
        cached = self._cached_search(key)
        if cached is not None:
            return cached

        metas = self._fetch("search", query, limit)
        self._store(metas)
        self._store_search(key, metas)
        return metas

    def details(self, anilist_id):
        cached = self._cached_anime(anilist_id)
        if cached is not None:
            return cached

        meta = self._fetch("details", anilist_id)
        self._store([meta])
        return meta

    def _fetch(self, operation, *args):
        """AniList, a przy jego awarii Jikan. Blad fallbacku nie chowa oryginalnego."""
        try:
            return getattr(self.provider, operation)(*args)
        except MetadataError as primary:
            if self.fallback is None:
                raise
            try:
                return getattr(self.fallback, operation)(*args)
            except MetadataError:
                raise primary

    # --- mapowanie (docs.md D7) ---

    def cached_mapping(self, anilist_id):
        with transaction(self.db_path) as connection:
            row = connection.execute(
                "SELECT provider_query FROM mapping WHERE anilist_id = ?", (int(anilist_id),)
            ).fetchone()
        return row["provider_query"] if row else None

    def remember_mapping(self, anilist_id, provider_query):
        with transaction(self.db_path) as connection:
            connection.execute(
                "INSERT INTO mapping (anilist_id, provider_query, confirmed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(anilist_id) DO UPDATE SET provider_query=excluded.provider_query, "
                "confirmed_at=excluded.confirmed_at",
                (int(anilist_id), provider_query, now()),
            )

    def forget_mapping(self, anilist_id):
        with transaction(self.db_path) as connection:
            connection.execute("DELETE FROM mapping WHERE anilist_id = ?", (int(anilist_id),))


def _meta_from_payload(payload):
    payload = dict(payload)
    payload.pop("title", None)  # wlasciwosc wyliczana, nie pole
    return AnimeMeta(**payload)
