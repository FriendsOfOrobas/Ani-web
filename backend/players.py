"""Warstwa odtwarzania.

Jeden interfejs (`PlayerBackend`), kilka implementacji o ROZNYCH mozliwosciach —
patrz docs.md §2.1. Kazdy backend deklaruje swoje `Capabilities`, a frontend
chowa kontrolki, ktorych dana implementacja nie obsluguje. Bez tego zbudujemy
UI pod mozliwosci, ktorych na Android TV po prostu nie ma.
"""

import os
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capabilities:
    """Co dany backend faktycznie potrafi.

    `seek` jest tu tylko dlatego, ze na Linuksie (mpv + IPC) jest osiagalny.
    Na Android TV nie jest i nie bedzie — patrz docs.md D2.
    """

    play: bool = True
    pause: bool = False
    stop: bool = True
    seek: bool = False
    position: bool = False
    ended_events: bool = False


class PlayerError(RuntimeError):
    """Blad odtwarzania. Komunikat idzie do UI."""


class PlayerBackend:
    name = "abstract"
    capabilities = Capabilities()

    def play(self, stream_url, headers=None, position=0, title=""):
        raise NotImplementedError

    def pause(self):
        raise PlayerError(f"{self.name}: pauza nieobslugiwana")

    def stop(self):
        raise NotImplementedError

    def status(self):
        """Zwraca co najmniej {"state": ...}. `position` tylko gdy backend potrafi."""
        raise NotImplementedError

    def describe(self):
        return {"player": self.name, "capabilities": asdict(self.capabilities)}


class SimulatedPlayer(PlayerBackend):
    """Nic nie uruchamia. Domyslny w CI i przy braku odtwarzacza.

    Istnieje, zeby reszta aplikacji byla testowalna bez ani-cli, mpv i Androida.
    """

    name = "simulated"
    capabilities = Capabilities(pause=True, stop=True, ended_events=False)

    def __init__(self):
        self._state = "idle"

    def play(self, stream_url, headers=None, position=0, title=""):
        self._state = "playing"
        return self.status()

    def pause(self):
        if self._state == "playing":
            self._state = "paused"
        elif self._state == "paused":
            self._state = "playing"
        return self.status()

    def stop(self):
        self._state = "stopped"
        return self.status()

    def status(self):
        return {"state": self._state}


class AniCliPlayer(PlayerBackend):
    """Oddaje uruchomienie odtwarzacza samemu ani-cli.

    Najbardziej odporny wariant na zmiany scraperow, bo ani-cli samo buduje
    komende z poprawnym refererem (docs.md §7, wariant B). Cena: nie mamy
    zadnej kontroli nad odtwarzaniem — tylko start i zabicie procesu.
    """

    name = "ani-cli"
    capabilities = Capabilities(pause=False, stop=True, ended_events=True)

    def __init__(self, ani_cli_command="ani-cli"):
        self.ani_cli_command = ani_cli_command
        self.process = None

    @property
    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def play(self, stream_url, headers=None, position=0, title=""):
        if self.is_running:
            return self.status()

        self.process = subprocess.Popen(
            [self.ani_cli_command, stream_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return self.status()

    def stop(self):
        if self.is_running:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        return {"state": "stopped"}

    def status(self):
        if self.process is None:
            return {"state": "idle"}
        if self.is_running:
            return {"state": "playing", "pid": self.process.pid}
        # Proces sie zakonczyl — dla tego backendu to wiarygodny sygnal "koniec".
        return {"state": "ended", "exit_code": self.process.returncode}


class MpvIpcPlayer(PlayerBackend):
    """mpv z --input-ipc-server, sterowane po sockecie JSON IPC.

    Jedyny backend z PELNA kontrola: pauza, seek do znacznika, odczyt pozycji,
    zdarzenie konca pliku. Uzywany w devie na Linuksie.

    NIE nadaje sie na Android TV: mpv w Termuksie ma tylko wyjscia null/image,
    a wariant x11 nie ma akceleracji sprzetowej (docs.md §2).

    Stage 0 — do implementacji.
    """

    name = "mpv-ipc"
    capabilities = Capabilities(pause=True, stop=True, seek=True, position=True, ended_events=True)

    def __init__(self, mpv_command="mpv", ipc_socket=""):
        self.mpv_command = mpv_command
        self.ipc_socket = ipc_socket

    def play(self, stream_url, headers=None, position=0, title=""):
        raise NotImplementedError("MpvIpcPlayer: Stage 0 (docs.md §12)")

    def stop(self):
        raise NotImplementedError("MpvIpcPlayer: Stage 0 (docs.md §12)")

    def status(self):
        return {"state": "idle"}


class AndroidIntentPlayer(PlayerBackend):
    """Docelowy backend produkcyjny: `am start` do mpv-android / VLC.

    Fire-and-forget — intent NIE tworzy procesu potomnego Termuksa, wiec nie ma
    czego wait()-owac (docs.md D3). Pauza i stan wymagaja osobnego kanalu
    self-adb, a seek do znacznika jest niedostepny i nie bedzie wystawiony.

    Wymaga na urzadzeniu: "Draw over other apps" dla Termuksa (inaczej `am start`
    z tla nie przejdzie) — docs.md §10.

    Stage 0 (start) + Stage 2 (kontrola przez adb) — do implementacji.
    """

    name = "android-intent"
    capabilities = Capabilities(pause=True, stop=True, seek=False, position=False, ended_events=False)

    def __init__(self, package="is.xyz.mpv", adb_command="adb"):
        self.package = package
        self.adb_command = adb_command

    def play(self, stream_url, headers=None, position=0, title=""):
        raise NotImplementedError("AndroidIntentPlayer: Stage 0 (docs.md §12)")

    def pause(self):
        raise NotImplementedError("AndroidIntentPlayer: Stage 2 (docs.md §12)")

    def stop(self):
        raise NotImplementedError("AndroidIntentPlayer: Stage 2 (docs.md §12)")

    def status(self):
        return {"state": "unknown"}


BACKENDS = {
    SimulatedPlayer.name: SimulatedPlayer,
    AniCliPlayer.name: AniCliPlayer,
    MpvIpcPlayer.name: MpvIpcPlayer,
    AndroidIntentPlayer.name: AndroidIntentPlayer,
}


def build_backend(name, config):
    if name == AniCliPlayer.name:
        return AniCliPlayer(ani_cli_command=config.get("ani_cli_command", "ani-cli"))
    if name == MpvIpcPlayer.name:
        return MpvIpcPlayer(
            mpv_command=config.get("mpv_command", "mpv"),
            ipc_socket=config.get("mpv_ipc_socket", ""),
        )
    if name == AndroidIntentPlayer.name:
        return AndroidIntentPlayer(
            package=config.get("android_player_package", "is.xyz.mpv"),
            adb_command=config.get("adb_command", "adb"),
        )
    return SimulatedPlayer()


def select_backend(config, which=None):
    """Wybiera backend po nazwie albo automatycznie.

    "auto" nie zgaduje Androida — na tym etapie schodzi do ani-cli, gdy jest
    dostepne, a w przeciwnym razie do symulacji. Backend produkcyjny wskazuje
    sie jawnie w config.local.json.
    """
    which = which or config.get("player", "auto")

    if which != "auto":
        if which not in BACKENDS:
            raise PlayerError(
                f"Nieznany odtwarzacz {which!r}; dostepne: {', '.join(sorted(BACKENDS))}"
            )
        return build_backend(which, config)

    if shutil.which(config.get("ani_cli_command", "ani-cli")):
        return build_backend(AniCliPlayer.name, config)
    return SimulatedPlayer()
