"""Dozorca stanu odtwarzania.

Powod istnienia: `publish()` bylo wolane tylko z play/pause/stop, czyli wylacznie
w reakcji na klikniecie. Kiedy odtwarzacz konczyl odcinek albo zostal zamkniety
z pilota, NIKT o tym nie wiedzial — UI wisial na "odtwarzanie". Ten watek
odpytuje odtwarzacz cyklicznie i pushuje zmiany przez SSE.

Przy okazji daje dwie rzeczy:
  - plynny podglad pozycji dla backendow, ktore ja raportuja (mpv-ipc),
  - wykrycie konca odcinka, na ktorym UI buduje pytanie "nastepny odcinek?"
    (tak jak menu ani-cli po odcinku).

Polling, a nie zdarzenia, bo na Androidzie nie ma czego nasluchiwac — intent nie
tworzy procesu potomnego (docs.md D3), a stan czyta sie z `dumpsys`.
"""

import threading

# Kompromis: dosc czesto, zeby koniec odcinka byl wykryty od razu, i dosc rzadko,
# zeby nie zarzynac urzadzenia TV odpytywaniem przez adb.
DEFAULT_INTERVAL = 2.0

# Stany, w ktorych cos jeszcze leci. Wyjscie z nich to "przestalo grac".
ACTIVE_STATES = {"playing", "paused"}

# Stany oznaczajace, ze odtwarzacz sam skonczyl albo zostal zamkniety.
FINISHED_STATES = {"ended", "idle", "stopped"}


class StateWatcher:
    def __init__(self, state, interval=DEFAULT_INTERVAL, on_finished=None):
        self.state = state
        self.interval = interval
        # Wolane raz, przy przejsciu z grania do konca. Sluzy do zapisu historii.
        self.on_finished = on_finished
        self._previous = None
        self._thread = None
        self._stop = threading.Event()

    # --- jeden przebieg (testowalny bez watkow) ---------------------------

    def tick(self):
        """Jedno odpytanie. Zwraca snapshot, gdy cos sie zmienilo, inaczej None."""
        try:
            snapshot = self.state.as_dict()
        except Exception as exc:  # noqa: BLE001
            # Odtwarzacz moze zniknac w dowolnym momencie (zabity socket, adb).
            # Dozorca nie ma prawa przez to umrzec — inaczej stan zamarza na dobre.
            self.state.last_error = f"Dozorca: {exc}"
            return None

        previous = self._previous
        self._previous = snapshot

        if previous is None:
            return None
        if not self._differs(previous, snapshot):
            return None

        was_active = previous.get("state") in ACTIVE_STATES
        is_finished = snapshot.get("state") in FINISHED_STATES

        if was_active and is_finished and self.on_finished is not None:
            try:
                # Snapshot SPRZED zmiany: tam jest jeszcze odcinek i pozycja.
                self.on_finished(previous, snapshot)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"Dozorca (koniec odcinka): {exc}"

        self.state.publish()
        return snapshot

    @staticmethod
    def _differs(previous, snapshot):
        """Czy zmiana jest warta pushnięcia.

        Porownujemy tylko pola, ktore widzi uzytkownik. Bez tego `position`
        rosnace co tick generowaloby push za kazdym razem takze wtedy, gdy
        backend pozycji nie raportuje i pole po prostu nie istnieje.
        """
        keys = ("state", "position", "duration", "last_error")
        if any(previous.get(key) != snapshot.get(key) for key in keys):
            return True
        return _episode_of(previous) != _episode_of(snapshot)

    # --- watek ------------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ani-web-watcher", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
            self._thread = None

    def _loop(self):
        # Event.wait zamiast sleep, zeby stop() dzialal natychmiast.
        while not self._stop.wait(self.interval):
            self.tick()


def _episode_of(snapshot):
    current = snapshot.get("current") or {}
    return (current.get("anilist_id"), current.get("episode"))
