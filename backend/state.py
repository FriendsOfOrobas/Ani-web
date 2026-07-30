"""Stan aplikacji i magazyn ustawien.

AppState spina warstwe zrodel (ani_runner) z warstwa odtwarzania (players) i
rozglasza zmiany stanu do subskrybentow — z tego karmiony jest strumien SSE.
"""

import json
import queue
import threading

from ani_runner import SourceError
from players import PlayerError


class SettingsStore:
    """Ustawienia uzytkownika w pliku JSON, per uid z ciasteczka."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def _read_all(self):
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            # Uszkodzony plik ustawien nie moze wywalic serwera.
            return {}

    def get(self, uid):
        with self._lock:
            return self._read_all().get(uid, {})

    def update(self, uid, values):
        with self._lock:
            everything = self._read_all()
            merged = dict(everything.get(uid, {}))
            merged.update(values)
            everything[uid] = merged
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(everything, fh, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
            return merged


class AppState:
    def __init__(self, player, source=None, mapper=None):
        self.player = player
        self.source = source
        # Mostek katalog -> zrodlo (docs.md D7). Opcjonalny: bez niego dziala
        # tylko odtwarzanie po tytule wprost.
        self.mapper = mapper
        self.current = None
        self.last_error = None
        self._subscribers = []
        self._lock = threading.Lock()

    # --- rozglaszanie ------------------------------------------------------

    def subscribe(self):
        q = queue.Queue(maxsize=16)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self):
        snapshot = self.as_dict()
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(snapshot)
            except queue.Full:
                # Wolny klient nie moze blokowac reszty — gubimy mu klatke.
                pass
        return snapshot

    # --- stan -------------------------------------------------------------

    def as_dict(self):
        payload = {
            "current": self.current,
            "last_error": self.last_error,
        }
        payload.update(self.player.describe())
        payload.update(self.player.status())
        return payload

    # --- akcje ------------------------------------------------------------

    def play(self, query=None, anilist_id=None, episode=1, quality=None, position=0):
        """Rozwiazuje adres just-in-time i uruchamia odtwarzacz.

        Kolejnosc jest istotna: adresy sa tokenizowane i krotko zyjace, wiec
        resolve() musi byc tuz przed play(), nigdy z wyprzedzeniem.

        Dwa tryby wywolania:
          - `query` — tytul wprost, bez katalogu,
          - `anilist_id` — mapowanie tytulu na zrodlo robi LazyMapper (docs.md D7).
        """
        self.last_error = None
        meta = None

        try:
            if anilist_id is not None:
                if self.mapper is None:
                    raise SourceError("Katalog nie jest skonfigurowany")
                stream, meta = self.mapper.resolve(anilist_id, episode, quality)
                query = meta.title
            else:
                if self.source is None:
                    raise SourceError("Brak skonfigurowanego zrodla")
                if not query:
                    raise SourceError("Brak tytulu do wyszukania")
                stream = self.source.resolve(query, episode, quality)
        except SourceError as exc:
            self.last_error = str(exc)
            self.publish()
            raise

        self.current = {
            "query": query,
            "episode": episode,
            "quality": quality,
            "stream_url": stream.stream_url,
            "anilist_id": meta.anilist_id if meta else anilist_id,
            "title": meta.title if meta else query,
            "cover": meta.cover if meta else "",
            "total_episodes": meta.episodes if meta else 0,
        }

        try:
            self.player.play(
                stream.stream_url,
                headers=stream.headers,
                position=position,
                title=f"{query} — odc. {episode}",
            )
        except (PlayerError, NotImplementedError) as exc:
            self.last_error = str(exc)
            self.publish()
            raise

        return self.publish()

    def pause(self):
        if not self.player.capabilities.pause:
            raise PlayerError(f"{self.player.name}: pauza nieobslugiwana")
        self.player.pause()
        return self.publish()

    def stop(self):
        self.player.stop()
        self.current = None
        return self.publish()
