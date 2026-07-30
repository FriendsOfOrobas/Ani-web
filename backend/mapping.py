"""Mostek katalog (AniList) -> zrodlo strumieni (allanime) — docs.md D7.

Mapowanie jest LENIWE: nic nie robimy przy wyszukiwaniu, dopiero przy `play`.
Dzieki temu katalog dziala nawet gdy allanime jest niedostepne.

Kolejnosc:
  1. jesli mamy potwierdzone mapowanie w cache — probujemy tylko jego,
  2. jesli nie, idziemy po kandydatach z AniList (romaji -> english -> synonimy),
  3. pierwszy, ktory zwroci adres, zapamietujemy.

Kazda proba to osobne uruchomienie ani-cli, wiec liczba kandydatow jest
ograniczona (MAX_CANDIDATES) — inaczej pudlo kosztowaloby kilkanascie sekund.
"""

from ani_runner import SourceError
from metadata import candidate_titles

MAX_CANDIDATES = 4


class LazyMapper:
    def __init__(self, source, metadata, max_candidates=MAX_CANDIDATES):
        self.source = source
        self.metadata = metadata
        self.max_candidates = max_candidates

    def candidates_for(self, meta):
        confirmed = self.metadata.cached_mapping(meta.anilist_id)
        if confirmed:
            # Potwierdzone mapowanie idzie pierwsze, ale nie jako jedyne — zrodla
            # zmieniaja tytuly, wiec przy pudle schodzimy do zwyklych kandydatow.
            rest = [t for t in candidate_titles(meta) if t.lower() != confirmed.lower()]
            return [confirmed, *rest[: self.max_candidates - 1]]
        return candidate_titles(meta)[: self.max_candidates]

    def resolve(self, anilist_id, episode=1, quality=None):
        """Zwraca (ResolvedStream, AnimeMeta). Rzuca SourceError, gdy nic nie wyszlo."""
        meta = self.metadata.details(anilist_id)
        candidates = self.candidates_for(meta)

        if not candidates:
            raise SourceError(f"Brak tytulu, po ktorym mozna szukac: {meta.title}")

        first_error = None
        for title in candidates:
            try:
                stream = self.source.resolve(title, episode, quality)
            except SourceError as exc:
                if first_error is None:
                    first_error = exc
                continue

            self.metadata.remember_mapping(anilist_id, title)
            return stream, meta

        # Potwierdzone mapowanie przestalo dzialac — nie zostawiamy go w cache,
        # zeby nastepna proba zaczela od czystych kandydatow.
        self.metadata.forget_mapping(anilist_id)

        tried = ", ".join(repr(t) for t in candidates)
        raise SourceError(
            f"Nie znaleziono zrodla dla {meta.title!r} (probowane: {tried}). "
            f"Ostatni blad: {first_error}"
        )
