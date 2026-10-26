"""Warstwa zrodel — adapter ani-cli.

To jest seam z docs.md §9. Reszta aplikacji zna WYLACZNIE kontrakt:

    search(query)                          -> [SearchResult]
    resolve(provider_id, episode, quality) -> ResolvedStream

Nic poza tym modulem nie ma prawa znac formatu wyjscia ani-cli. ani-cli psuje
sie regularnie (domeny allanime, Cloudflare, od v4.15 zaleznosc od Botana),
wiec podmiana na anipy-cli albo inne zrodlo ma kosztowac jeden adapter.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass, field

# Referery wymagane przez CDN-y allanime. Odwzorowane ze zrodla ani-cli;
# ANI_CLI_PLAYER=debug zwraca sam URL i NIE podaje referera, wiec musimy
# odtworzyc te logike u siebie (docs.md §7).
#
# UWAGA: ta wartosc MUSI zgadzac sie z `allanime_refr` w zainstalowanym ani-cli.
# Sprawdzone 2026-07-30 przeciwko 4.15.0 (== master): "https://mkissa.to".
# Poprzednio bylo tu "https://youtu-chan.com" — nieaktualne, allanime zmienilo
# domeny. Przy kazdej aktualizacji ani-cli warto porownac:
#     grep -E '^allanime_refr=' "$(command -v ani-cli)"
# Nadpisywalne konfiguracja (`allanime_referer`), zeby zmiana domeny nie
# wymagala zmiany kodu.
DEFAULT_REFERER = "https://mkissa.to"
MP4UPLOAD_REFERER = "https://www.mp4upload.com"

# UA uzywany przez ani-cli. Do zweryfikowania przeciwko zainstalowanej wersji —
# flagi i staleset ani-cli sa wprost oznaczone jako "subject to change".
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) "
    "Gecko/20100101 Firefox/150.0"
)

# Adresy sa tokenizowane i krotko zyjace. Faktycznego TTL nie znamy, wiec
# trzymamy zachowawczo nisko — rozwiazujemy just-in-time, nie z wyprzedzeniem.
STREAM_TTL_SECONDS = 300

# Sonda udajaca mpv — patrz backend/mpv-probe.
PROBE_SCRIPT = Path(__file__).resolve().parent / "mpv-probe"
PROBE_MARKER = "ANIWEB-ARG\t"
# ani-cli moze poprzedzic linie sekwencjami sterujacymi (postep), wiec szukamy
# znacznika gdziekolwiek w linii, nie od jej poczatku.
_PROBE_LINE_RE = re.compile(re.escape(PROBE_MARKER) + r"(.*)")

DEBUG_ALL_LINKS_MARKER = "All links:"
DEBUG_SELECTED_MARKER = "Selected link:"


class SourceError(RuntimeError):
    """Zrodlo niedostepne. Komunikat idzie prosto do UI."""


@dataclass
class SearchResult:
    provider_id: str
    title: str
    episodes: int = 0


@dataclass
class ResolvedStream:
    stream_url: str
    referer: str = ""
    headers: dict = field(default_factory=dict)
    ttl: int = STREAM_TTL_SECONDS
    available_qualities: dict = field(default_factory=dict)
    # Napisy i osobna sciezka audio z ani-cli (puste, gdy zrodlo ich nie ma).
    subtitles: str = ""
    audio: str = ""


def referer_for(url, default_referer=DEFAULT_REFERER):
    """Referer wymagany przez dany CDN (albo pusty, gdy niepotrzebny)."""
    lowered = url.lower()
    if "mp4upload" in lowered:
        return MP4UPLOAD_REFERER
    if "sharepoint" in lowered:
        return ""
    return default_referer


def headers_for(url, default_referer=DEFAULT_REFERER):
    headers = {"User-Agent": USER_AGENT}
    referer = referer_for(url, default_referer)
    if referer:
        headers["Referer"] = referer
    return headers


def build_resolve_command(
    query, episode, quality="1080", select=1, ani_cli_command="ani-cli", no_detach=False
):
    """Wywolanie ani-cli w trybie nieinteraktywnym.

    -S wybiera n-ty wynik wyszukiwania, -e numer odcinka. Razem omijaja OBA
    prompty fzf. ANI_CLI_NON_INTERACTIVE nie istnieje w aktualnym masterze —
    nie polegac na nim.
    """
    command = [
        ani_cli_command,
        "-q", str(quality),
        "-S", str(select),
        "-e", str(episode),
    ]
    if no_detach:
        # Potrzebne sondzie: bez tego ani-cli odpala "odtwarzacz" w tle (nohup)
        # i wyrzuca jego stdout.
        command.append("--no-detach")
        # Bez tego po "odtworzeniu" ani-cli wchodzi w menu (nastepny/powtorz) i
        # czeka na terminal az do timeoutu.
        command.append("--exit-after-play")
    command.append(query)
    return command


def resolve_env(base_env=None, player="debug"):
    """Srodowisko dla podprocesu ani-cli.

    Dziedziczymy os.environ, gdy nie podano nic jawnie. Zbudowanie env od zera
    wyglada czysciej, ale jest bledne: ani-cli to skrypt POSIX-owy wolajacy
    curl/sed/grep i czytajacy HOME, a bez PATH nawet samo ani-cli nie zostanie
    znalezione (Python schodzi wtedy do os.defpath = "/bin:/usr/bin"). Na
    Termuksie, gdzie cale userland siedzi w /data/data/com.termux/files/usr/bin,
    oznacza to brak absolutnie wszystkiego.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["ANI_CLI_PLAYER"] = str(player)
    return env


def parse_debug_output(output):
    """Rozbija wyjscie ANI_CLI_PLAYER=debug na jakosci i wybrany adres.

    Format wyjscia:
        All links:
        1080 >https://...
        720 >https://...
        Selected link:
        https://...
    """
    qualities = {}
    selected = ""
    section = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == DEBUG_ALL_LINKS_MARKER:
            section = "links"
            continue
        if line == DEBUG_SELECTED_MARKER:
            section = "selected"
            continue

        if section == "links" and " >" in line:
            quality, _, url = line.partition(" >")
            if url:
                qualities[quality.strip()] = url.strip()
        elif section == "selected" and not selected:
            selected = line

    if not selected and qualities:
        # Fallback: brak markera "Selected link:", ale mamy linki.
        selected = next(iter(qualities.values()))

    return selected, qualities


def parse_probe_output(output):
    """Wyciaga adres, Referer i napisy z argumentow, ktore ani-cli podal sondzie.

    Zwraca slownik (puste pola, gdy brak) albo None, gdy w wyjsciu nie ma
    zadnych linii sondy — to znaczy, ze ani-cli nie dotarlo do odtwarzacza.
    """
    args = [m.group(1).rstrip("\r") for m in _PROBE_LINE_RE.finditer(output)]
    if not args:
        return None

    result = {"url": "", "referer": "", "subtitles": "", "audio": ""}
    options = {
        "--referrer=": "referer",
        "--http-referrer=": "referer",
        "--sub-file=": "subtitles",
        "--audio-file=": "audio",
    }
    for arg in args:
        for prefix, key in options.items():
            if arg.startswith(prefix):
                result[key] = arg[len(prefix):]
                break
        else:
            if not arg.startswith("--"):
                # Adres jest ostatnim argumentem nie bedacym opcja.
                result["url"] = arg
    return result


def _no_stream_message(completed):
    """Komunikat, gdy ani-cli nie oddalo adresu.

    Nie zgadujemy przyczyny. Wczesniej bylo tu twarde "zaktualizuj ani-cli", co
    myli, gdy ani-cli jest aktualne, a padlo cos po stronie allanime (typowo
    zagadka Cloudflare — wtedy ani-cli mowi "Episode is released, but no valid
    sources!"). Pokazujemy wiec to, co powiedzialo samo ani-cli.
    """
    detail = (completed.stderr or completed.stdout or "").strip()
    if detail:
        last_line = detail.splitlines()[-1].strip()
        return f"Zrodlo niedostepne — ani-cli: {last_line}"
    return (
        "Zrodlo niedostepne — ani-cli nie zwrocilo adresu i nic nie wypisalo. "
        "Sprawdz aktualnosc ani-cli oraz dostepnosc allanime."
    )


class SourceProvider:
    """Kontrakt warstwy zrodel. Implementacje: AniCliSource (i pozniej inne)."""

    name = "abstract"

    def check(self):
        raise NotImplementedError

    def search(self, query):
        raise NotImplementedError

    def resolve(self, provider_id, episode, quality=None):
        raise NotImplementedError


class AniCliSource(SourceProvider):
    """Adapter na ani-cli uruchamiane jako podproces."""

    name = "ani-cli"

    def __init__(
        self,
        ani_cli_command="ani-cli",
        quality="1080",
        timeout=60,
        env=None,
        default_referer=DEFAULT_REFERER,
    ):
        self.ani_cli_command = ani_cli_command
        self.quality = quality
        self.timeout = timeout
        self.env = env
        self.default_referer = default_referer

    def check(self):
        """Weryfikacja zaleznosci przy starcie — czytelny blad zamiast kryptycznego.

        Botan byl twarda zaleznoscia ani-cli ~4.15 (deszyfrowanie zrodel), a jego
        brak objawial sie pozniej jako niedzialajace rozwiazywanie URL. Nowsze
        ani-cli (5.1.6, sprawdzone na HY300) botana nie uzywaja, wiec wymagamy go
        tylko wtedy, gdy skrypt ani-cli sam go wymienia.
        """
        missing = []
        located = shutil.which(self.ani_cli_command)
        if not located:
            missing.append(self.ani_cli_command)
        elif _script_mentions(located, "botan") and not shutil.which("botan"):
            missing.append("botan (wymagane przez ten ani-cli)")
        return missing

    def search(self, query):
        # Stage 1. ani-cli nie ma trybu "wypisz wyniki wyszukiwania" bez fzf,
        # wiec to bedzie albo zapytanie do API allanime, albo anipy-cli.
        raise NotImplementedError("search: Stage 1 (docs.md §12)")

    def resolve(self, provider_id, episode, quality=None):
        quality = quality or self.quality
        probe = self._probe_command()

        if probe:
            completed = self._run(provider_id, episode, quality, player=probe, no_detach=True)
            parsed = parse_probe_output(completed.stdout)
            if parsed and parsed["url"]:
                url = parsed["url"]
                referer = parsed["referer"] or referer_for(url, self.default_referer)
                headers = {"User-Agent": USER_AGENT}
                if referer:
                    headers["Referer"] = referer
                return ResolvedStream(
                    stream_url=url,
                    referer=referer,
                    headers=headers,
                    subtitles=parsed["subtitles"],
                    audio=parsed["audio"],
                )
            # Sonda nic nie dala (stary ani-cli bez --no-detach / bez galezi mpv,
            # albo fixture w testach) — wracamy do trybu debug.

        completed = self._run(provider_id, episode, quality, player="debug")
        selected, qualities = parse_debug_output(completed.stdout)
        if not selected:
            raise SourceError(_no_stream_message(completed))

        return ResolvedStream(
            stream_url=selected,
            referer=referer_for(selected, self.default_referer),
            headers=headers_for(selected, self.default_referer),
            available_qualities=qualities,
        )

    def _probe_command(self):
        """Sciezka sondy, jesli da sie ja uruchomic; inaczej None."""
        if not PROBE_SCRIPT.is_file():
            return None
        if not os.access(PROBE_SCRIPT, os.X_OK):
            try:
                PROBE_SCRIPT.chmod(PROBE_SCRIPT.stat().st_mode | 0o111)
            except OSError:
                return None
        return str(PROBE_SCRIPT)

    def _run(self, provider_id, episode, quality, player, no_detach=False):
        command = build_resolve_command(
            query=provider_id,
            episode=episode,
            quality=quality,
            ani_cli_command=self.ani_cli_command,
            no_detach=no_detach,
        )
        try:
            return subprocess.run(
                command,
                env=resolve_env(self.env, player=player),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SourceError(
                f"Nie znaleziono {self.ani_cli_command} — zainstaluj ani-cli"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError("ani-cli nie odpowiedzialo w czasie") from exc


def _script_mentions(path, word):
    """Czy plik tekstowy (skrypt ani-cli) wspomina dane slowo. Blad czytania = nie."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return word in handle.read()
    except OSError:
        return False
