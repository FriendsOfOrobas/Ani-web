"""Warstwa odtwarzania.

Jeden interfejs (`PlayerBackend`), kilka implementacji o ROZNYCH mozliwosciach —
patrz docs.md §2.1. Kazdy backend deklaruje swoje `Capabilities`, a frontend
chowa kontrolki, ktorych dana implementacja nie obsluguje. Bez tego zbudujemy
UI pod mozliwosci, ktorych na Android TV po prostu nie ma.
"""

import json
import os
import re
import shutil
import signal
import socket
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from dataclasses import asdict, dataclass

from aniskip import script_opts


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
    # Przewijanie o staly krok wzgledem pozycji (+-10 s, +-60 s). To nie jest seek
    # do dowolnego miejsca (docs.md D2) — tylko przyciski "do przodu / do tylu".
    step: bool = False
    position: bool = False
    ended_events: bool = False


# Wlasny skrypt mpv do przewijania czolowki/zakonczenia (czasy z AniSkip).
SKIP_SCRIPT = Path(__file__).resolve().parent / "assets" / "aniweb-skip.lua"


def skip_script_text():
    return SKIP_SCRIPT.read_text(encoding="utf-8")


class PlayerError(RuntimeError):
    """Blad odtwarzania. Komunikat idzie do UI."""


class PlayerBackend:
    name = "abstract"
    capabilities = Capabilities()

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
        raise NotImplementedError

    def pause(self):
        raise PlayerError(f"{self.name}: pauza nieobslugiwana")

    def seek(self, delta):
        raise PlayerError(f"{self.name}: przewijanie nieobslugiwane")

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

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
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

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
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

    Nagloweki (Referer, UA) idza przez --http-header-fields-append, wiec na
    Linuksie proxy z docs.md §7 nie jest do niczego potrzebne.
    """

    name = "mpv-ipc"
    capabilities = Capabilities(
        pause=True, stop=True, seek=True, step=True, position=True, ended_events=True
    )

    # Ile czekamy, az mpv wystawi socket IPC po starcie procesu.
    SOCKET_WAIT_SECONDS = 5.0
    IPC_TIMEOUT_SECONDS = 2.0

    def __init__(self, mpv_command="mpv", ipc_socket=""):
        self.mpv_command = mpv_command
        self.ipc_socket = ipc_socket or os.path.join(
            tempfile.gettempdir(), f"ani-web-mpv-{os.getpid()}.sock"
        )
        self.process = None

    @property
    def is_running(self):
        return self.process is not None and self.process.poll() is None

    # --- IPC --------------------------------------------------------------

    def _request(self, *command):
        """Jedno zapytanie JSON IPC. Nowe polaczenie na komende — mpv na to pozwala.

        mpv wtraca w ten sam strumien asynchroniczne zdarzenia, wiec czytamy
        linie tak dlugo, az trafimy na odpowiedz (ta z kluczem "error").
        """
        payload = (json.dumps({"command": list(command)}) + "\n").encode("utf-8")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.IPC_TIMEOUT_SECONDS)
            sock.connect(self.ipc_socket)
            sock.sendall(payload)

            buffer = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    raise PlayerError("mpv zamknal socket IPC")
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in message:
                        return message

    def _command(self, *command):
        try:
            message = self._request(*command)
        except (OSError, socket.timeout) as exc:
            raise PlayerError(f"mpv nie odpowiada po IPC: {exc}") from exc
        if message.get("error") != "success":
            raise PlayerError(f"mpv odrzucil komende {command[0]}: {message.get('error')}")
        return message.get("data")

    def _get(self, prop):
        """Odczyt wlasciwosci best-effort — status nie moze wywalac zapytania."""
        try:
            return self._command("get_property", prop)
        except PlayerError:
            return None

    # --- cykl zycia -------------------------------------------------------

    def _wait_for_socket(self):
        deadline = time.monotonic() + self.SOCKET_WAIT_SECONDS
        while time.monotonic() < deadline:
            if not self.is_running:
                code = self.process.returncode if self.process else "?"
                raise PlayerError(f"mpv zakonczyl sie od razu (kod {code})")
            if os.path.exists(self.ipc_socket):
                return
            time.sleep(0.05)
        raise PlayerError(f"mpv nie wystawil socketu IPC w {self.SOCKET_WAIT_SECONDS:.0f}s")

    def _build_args(self, stream_url, headers, position, title, subtitles="", audio="", skip=None):
        args = [
            self.mpv_command,
            f"--input-ipc-server={self.ipc_socket}",
            "--force-window=yes",
            "--keep-open=no",
        ]
        if position:
            args.append(f"--start={int(position)}")
        if title:
            args.append(f"--force-media-title={title}")
        for key, value in (headers or {}).items():
            # -append, a nie lista po przecinkach: wartosci naglowkow moga
            # zawierac przecinki i rozjechalyby parsowanie.
            args.append(f"--http-header-fields-append={key}: {value}")
        if subtitles:
            args.append(f"--sub-file={subtitles}")
        if audio:
            args.append(f"--audio-file={audio}")
        opts = script_opts(skip or {})
        if opts:
            args.append(f"--script={SKIP_SCRIPT}")
            args.append(f"--script-opts={opts}")
        args.append(stream_url)
        return args

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
        # Restart zamiast loadfile: deterministyczny, a pozycja startowa
        # przechodzi wtedy przez --start, bez wyscigu z ladowaniem pliku.
        if self.is_running:
            self.stop()

        if os.path.exists(self.ipc_socket):
            os.unlink(self.ipc_socket)

        try:
            self.process = subprocess.Popen(
                self._build_args(stream_url, headers, position, title, subtitles, audio, skip),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise PlayerError(f"Nie znaleziono {self.mpv_command} — zainstaluj mpv") from exc

        self._wait_for_socket()
        return self.status()

    def pause(self):
        if not self.is_running:
            raise PlayerError("mpv nie jest uruchomiony")
        self._command("cycle", "pause")
        return self.status()

    def seek(self, delta):
        if not self.is_running:
            raise PlayerError("mpv nie jest uruchomiony")
        self._command("seek", float(delta), "relative")
        return self.status()

    def stop(self):
        if self.is_running:
            try:
                self._command("quit")
            except PlayerError:
                # Socket moze byc juz martwy — wtedy zostaje sygnal.
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)

        self.process = None
        if os.path.exists(self.ipc_socket):
            try:
                os.unlink(self.ipc_socket)
            except OSError:
                pass
        return {"state": "stopped"}

    def status(self):
        if self.process is None:
            return {"state": "idle"}
        if not self.is_running:
            return {"state": "ended", "exit_code": self.process.returncode}

        payload = {"state": "paused" if self._get("pause") else "playing"}
        position = self._get("time-pos")
        if position is not None:
            payload["position"] = round(position)
        duration = self._get("duration")
        if duration:
            payload["duration"] = round(duration)
        return payload


class AndroidIntentPlayer(PlayerBackend):
    """Docelowy backend produkcyjny: `am start` do mpv-android / VLC. Stage 2.

    Model DWUWARSTWOWY (docs.md §2):
      1. start   — `am start` z Termuksa. Fire-and-forget: intent NIE tworzy
                   procesu potomnego, wiec nie ma czego wait()-owac (D3).
      2. kontrola — `adb shell` po self-adb (wireless debugging na 127.0.0.1);
                   stan czytany z `dumpsys media_session`.

    Kanal adb jest BEST-EFFORT. Po restarcie urzadzenia trzeba sparowac od nowa,
    a port bywa losowy — dlatego brak adb degraduje capabilities, a nie wywala
    odtwarzania. `am start` dziala bez adb.

    Wymagania na urzadzeniu (docs.md §10) — bez nich to nie ruszy:
      - "Draw over other apps" dla Termuksa (inaczej `am start` z tla nie przejdzie),
      - wylaczony phantom process killer,
      - termux-wake-lock.

    Zweryfikowane na Magcubic HY300 Pro+ z mpv-android (docs.md D10): start,
    pauza, krok +-10/+-60 s, stan i pozycja z `dumpsys`, wykrycie konca.
    """

    name = "android-intent"
    # `position` jest zgrubna: `dumpsys` odswieza ja co kilka sekund (D2). Seek do
    # dowolnego miejsca nie istnieje; jest tylko krok +-10 s / +-60 s przez klawisze.
    capabilities = Capabilities(
        pause=True, stop=True, seek=False, step=True, position=True, ended_events=True
    )

    COMMAND_TIMEOUT = 10

    # Po `am start` odtwarzacz potrzebuje chwili, zeby zarejestrowac sesje
    # medialna. Bez tej karencji brak sesji tuz po starcie bylby odczytany jako
    # "odtwarzacz zamkniety" i dozorca oglaszalby koniec odcinka w chwili startu.
    SESSION_GRACE_SECONDS = 8.0

    # Klasy Activity odtwarzaczy. mpv-android jest domyslny (D1): hwdec + extras
    # z pozycja. VLC bywa zepsuty na Androidzie 13+ (VLC #2776).
    ACTIVITIES = {
        "is.xyz.mpv": "is.xyz.mpv/.MPVActivity",
        "org.videolan.vlc": "org.videolan.vlc/.gui.video.VideoPlayerActivity",
    }

    KEYCODE_PLAY_PAUSE = 85
    KEYCODE_STOP = 86
    # Zmierzone w mpv-android na HY300: strzalki przewijaja o ~10 s (do klatki
    # kluczowej), klawisze mediow FF/REW o ~60 s.
    SEEK_KEYCODES = {10: 22, -10: 21, 60: 90, -60: 89}

    # mpv-android nie przyjmuje Referera ani napisow z intentu (#427), wiec tak jak
    # ani-cli (android_mpv) piszemy je do pliku, ktory uzytkownik raz dolacza w
    # mpv.conf (`include=/storage/emulated/0/mpv/mpv.config.mp4`) — docs.md §7.
    # Plik piszemy ZAWSZE (takze pusty), zeby referer z poprzedniego odcinka nie
    # zostal na nastepny.
    CONFIG_DIR = "/sdcard/mpv"
    CONFIG_FILE = "/sdcard/mpv/mpv.config.mp4"
    # Ta sama sciezka widziana z procesu aplikacji (Termux), bez adb. Dziala, gdy
    # Termux ma uprawnienie do pamieci wspoldzielonej (termux-setup-storage albo
    # `pm grant com.termux android.permission.WRITE_EXTERNAL_STORAGE`).
    LOCAL_CONFIG_FILE = "/storage/emulated/0/mpv/mpv.config.mp4"

    def __init__(self, package="is.xyz.mpv", adb_command="adb", runner=None, local_config_file=None):
        self.package = package
        self.local_config_file = local_config_file or self.LOCAL_CONFIG_FILE
        # Ostrzezenie widoczne w UI, gdy konfiguracji nie udalo sie zapisac.
        self.warning = None
        self.adb_command = adb_command
        # Wstrzykiwalne, zeby dalo sie testowac bez Androida i bez adb.
        self._run = runner if runner is not None else _run_command
        self.started = False
        self.config_written = False
        self.config_method = None
        self._started_at = 0.0
        self._last_state = "idle"

    # --- komendy ----------------------------------------------------------

    def _am_start(self, args):
        """`am start` uruchamiany lokalnie w Termuksie, bez adb."""
        return self._run(["am", "start", *args], timeout=self.COMMAND_TIMEOUT)

    def _adb_shell(self, args):
        return self._run(
            [self.adb_command, "shell", *args], timeout=self.COMMAND_TIMEOUT
        )

    @property
    def adb_available(self):
        try:
            completed = self._run([self.adb_command, "get-state"], timeout=self.COMMAND_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0 and "device" in (completed.stdout or "")

    def describe(self):
        """Capabilities zaleza od tego, czy self-adb faktycznie stoi.

        Bez adb zostaje samo uruchamianie — UI ma wtedy schowac pauze, zamiast
        pokazywac przycisk, ktory nic nie robi.
        """
        payload = super().describe()
        if not self.adb_available:
            payload["capabilities"] = asdict(
                Capabilities(play=True, pause=False, stop=False, position=False)
            )
            payload["adb"] = False
        else:
            payload["adb"] = True
        payload["warning"] = self.warning
        return payload

    # --- odtwarzanie ------------------------------------------------------

    def _intent_args(self, stream_url, position, title):
        activity = self.ACTIVITIES.get(self.package)
        args = ["-a", "android.intent.action.VIEW", "-d", stream_url, "-t", "video/*"]
        if activity:
            args += ["-n", activity]
        else:
            args += ["-p", self.package]

        if position:
            # mpv-android czyta pozycje startowa z extras (w milisekundach).
            # VLC uzywa tej samej konwencji od 1.4.1 (D5).
            args += ["--ei", "position", str(int(position) * 1000)]
        if title:
            args += ["--es", "title", title]
        return args

    @staticmethod
    def build_config(headers=None, subtitles="", audio="", skip=None, script_path=""):
        """Tresc mpv.config.mp4: referrer, napisy, audio, przewijanie (po jednym w linii)."""
        lines = []
        referer = (headers or {}).get("Referer")
        if referer:
            lines.append(f"referrer={referer}")
        if subtitles:
            lines.append(f"sub-file={subtitles}")
            # Napisy maja byc widoczne od razu, bez grzebania w menu.
            lines.append("sub-visibility=yes")
        if audio:
            lines.append(f"audio-file={audio}")
        opts = script_opts(skip or {})
        if opts and script_path:
            lines.append(f"script={script_path}")
            lines.append(f"script-opts={opts}")
        return "".join(line + "\n" for line in lines)

    def _write_config(self, content):
        """Zapisuje plik konfiguracji; zwraca "direct", "adb" albo None.

        Najpierw bezposrednio (nie zalezy od Wireless debugging, ktory potrafi
        zgasnac sam), potem przez adb. Gdy zawiedzie jedno i drugie, stary plik
        zostaje na dysku — a z nim Referer i napisy POPRZEDNIEGO odcinka — dlatego
        wywolujacy musi to pokazac uzytkownikowi (`self.warning`).
        """
        try:
            os.makedirs(os.path.dirname(self.local_config_file), exist_ok=True)
            with open(self.local_config_file, "w", encoding="utf-8") as handle:
                handle.write(content)
            return "direct"
        except OSError:
            pass

        if not self.adb_available:
            return None
        script = f"mkdir -p {self.CONFIG_DIR} && printf %s {shlex.quote(content)} > {self.CONFIG_FILE}"
        try:
            completed = self._adb_shell([script])
        except (OSError, subprocess.SubprocessError):
            return None
        return "adb" if completed.returncode == 0 else None

    def _ensure_skip_script(self):
        """Kladzie skrypt Lua obok pliku konfiguracji; zwraca sciezke albo "".

        Sciezka jest taka, jaka widzi mpv-android (ta sama co przy zapisie
        bezposrednim). Najpierw zapis bezposredni, potem adb.
        """
        local_path = os.path.join(os.path.dirname(self.local_config_file), SKIP_SCRIPT.name)
        try:
            text = skip_script_text()
        except OSError:
            return ""
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            return local_path
        except OSError:
            pass
        if not self.adb_available:
            return ""
        script = (
            f"mkdir -p {self.CONFIG_DIR} && "
            f"printf %s {shlex.quote(text)} > {self.CONFIG_DIR}/{SKIP_SCRIPT.name}"
        )
        try:
            completed = self._adb_shell([script])
        except (OSError, subprocess.SubprocessError):
            return ""
        return local_path if completed.returncode == 0 else ""

    def play(self, stream_url, headers=None, position=0, title="", subtitles="", audio="", skip=None):
        # Intent nie niesie Referera ani napisow — ida plikiem (CONFIG_FILE).
        script_path = self._ensure_skip_script() if script_opts(skip or {}) else ""
        method = self._write_config(
            self.build_config(headers, subtitles, audio, skip, script_path)
        )
        self.config_method = method
        self.config_written = method is not None
        self.warning = None if method else (
            "Nie udało się zapisać konfiguracji mpv (brak dostępu do pamięci i brak "
            "adb) — napisy i Referer mogą być z poprzedniego odcinka albo strumień da 403."
        )
        try:
            completed = self._am_start(self._intent_args(stream_url, position, title))
        except FileNotFoundError as exc:
            raise PlayerError(
                "Brak polecenia `am` — ten backend dziala tylko w Termuksie na Androidzie"
            ) from exc
        except subprocess.SubprocessError as exc:
            raise PlayerError(f"`am start` nie odpowiedzialo: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            hint = detail[-1] if detail else f"kod {completed.returncode}"
            raise PlayerError(
                f"`am start` odrzucil intent: {hint}. "
                "Sprawdz uprawnienie „Draw over other apps” dla Termuksa (docs.md §10)."
            )

        self.started = True
        self._started_at = time.monotonic()
        self._last_state = "playing"
        return self.status()

    def pause(self):
        if not self.started:
            raise PlayerError("Nic nie jest uruchomione")
        if not self.adb_available:
            raise PlayerError(
                "Pauza wymaga self-adb (wireless debugging na 127.0.0.1) — docs.md §2"
            )
        self._adb_shell(["input", "keyevent", str(self.KEYCODE_PLAY_PAUSE)])
        # Stan i tak weryfikujemy przez dumpsys — keyevent nie potwierdza niczego.
        return self.status()

    def seek(self, delta):
        if not self.started:
            raise PlayerError("Nic nie jest uruchomione")
        keycode = self.SEEK_KEYCODES.get(int(delta))
        if keycode is None:
            raise PlayerError(
                f"Obslugiwany krok to {sorted(self.SEEK_KEYCODES)} sekund, nie {delta}"
            )
        if not self.adb_available:
            raise PlayerError("Przewijanie wymaga self-adb (wireless debugging)")
        self._adb_shell(["input", "keyevent", str(keycode)])
        return self.status()

    def stop(self):
        if self.adb_available and self.started:
            self._adb_shell(["input", "keyevent", str(self.KEYCODE_STOP)])
        # force-stop dziala lokalnie w Termuksie i jest pewniejszy niz keyevent.
        try:
            self._run(["am", "force-stop", self.package], timeout=self.COMMAND_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass

        self.started = False
        self._last_state = "stopped"
        return {"state": "stopped"}

    def status(self):
        if not self.started:
            return {"state": self._last_state if self._last_state == "stopped" else "idle"}
        if not self.adb_available:
            # Bez adb nie wiemy nic ponad "kazalismy uruchomic". Mowimy to wprost,
            # zamiast udawac, ze leci — dozorca nie zglosi wtedy falszywego konca.
            return {"state": "unknown"}

        try:
            completed = self._adb_shell(["dumpsys", "media_session"])
        except (OSError, subprocess.SubprocessError) as exc:
            return {"state": "unknown", "error": str(exc)}

        parsed = parse_media_session(completed.stdout or "", self.package)
        if parsed is None:
            if time.monotonic() - self._started_at < self.SESSION_GRACE_SECONDS:
                # Jeszcze sie podnosi — nie wolno tego ogłosić jako koniec.
                return {"state": "starting"}
            # Sesja zniknela po karencji — odtwarzacz zostal zamkniety.
            self.started = False
            self._last_state = "ended"
            return {"state": "ended"}

        self._last_state = parsed["state"]
        return parsed


# Kody stanu PlaybackState z Androida (android.media.session.PlaybackState).
_PLAYBACK_STATES = {
    0: "unknown",   # STATE_NONE
    1: "stopped",
    2: "paused",
    3: "playing",
    6: "playing",   # BUFFERING — dla uzytkownika to nadal "leci"
    7: "unknown",   # ERROR
}

_STATE_RE = re.compile(r"state=PlaybackState\s*\{(?P<body>[^}]*)\}")
# Lookbehind odcina wylacznie "buffered position=0", ktore inaczej nadpisywaloby
# prawdziwe position=124500 zerem — w bloku PlaybackState wystepuja OBA.
# Android 14 zapisuje stan jako `state=PLAYING(3)`, starsze wersje jako `state=3`
# (sprawdzone na HY300: dumpsys media_session). Bierzemy liczbe w obu przypadkach.
_FIELD_RE = re.compile(
    r"(?<!buffered )(?P<key>state|position|duration)=(?:[A-Z_]+\()?(?P<value>-?\d+)\)?"
)


def parse_media_session(output, package):
    """Wyciaga stan sesji medialnej danej paczki z `dumpsys media_session`.

    Zwraca None, gdy paczka nie ma aktywnej sesji — dla nas to znaczy, ze
    odtwarzacz zostal zamkniety.

    Format `dumpsys` nie jest publicznym API i rozni sie miedzy wersjami
    Androida, dlatego parsowanie jest defensywne: szukamy bloku paczki, a w nim
    pierwszego PlaybackState. Wzorowane na bibliotece `androidtv` (docs.md §13).
    """
    # Kotwica "package=<nazwa>" (linia sesji), nie samo wystapienie nazwy: dumpsys
    # wspomina paczke tez w "Audio playback ... packages=<nazwa>", a to NIE jest
    # aktywna sesja. "packages=" nie pasuje do "package=".
    anchor = re.search(r"package=" + re.escape(package) + r"(?![\w.])", output)
    if anchor is None:
        return None

    # Blok paczki konczy sie tam, gdzie zaczyna sie nastepna sesja.
    start = anchor.start()
    remainder = output[start:]
    next_session = remainder.find("\n  package=", 1)
    block = remainder if next_session == -1 else remainder[:next_session]

    match = _STATE_RE.search(block)
    if match is None:
        return {"state": "unknown"}

    fields = {}
    for found in _FIELD_RE.finditer(match.group("body")):
        # Pierwsze wystapienie wygrywa — dumpsys potrafi powtorzyc klucz.
        fields.setdefault(found.group("key"), int(found.group("value")))

    payload = {"state": _PLAYBACK_STATES.get(fields.get("state", 0), "unknown")}
    position = fields.get("position")
    if position is not None and position >= 0:
        # dumpsys podaje milisekundy; pozycja aktualizuje sie TYLKO przy zmianach
        # stanu, wiec jest zgrubna (docs.md D2).
        payload["position"] = position // 1000
    duration = fields.get("duration")
    if duration is not None and duration > 0:
        payload["duration"] = duration // 1000
    return payload


def _run_command(command, timeout=10):
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )


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
