"""Warstwa zrodel — adapter ani-cli.

To jest seam z docs.md §9. Reszta aplikacji zna WYLACZNIE kontrakt:

    search(query)                          -> [SearchResult]
    resolve(provider_id, episode, quality) -> ResolvedStream

Nic poza tym modulem nie ma prawa znac formatu wyjscia ani-cli. ani-cli psuje
sie regularnie (domeny allanime, Cloudflare, od v4.15 zaleznosc od Botana),
wiec podmiana na anipy-cli albo inne zrodlo ma kosztowac jeden adapter.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field

# Referery wymagane przez CDN-y allanime. Odwzorowane ze zrodla ani-cli;
# ANI_CLI_PLAYER=debug zwraca sam URL i NIE podaje referera, wiec musimy
# odtworzyc te logike u siebie (docs.md §7).
DEFAULT_REFERER = "https://youtu-chan.com"
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


def referer_for(url):
    """Referer wymagany przez dany CDN (albo pusty, gdy niepotrzebny)."""
    lowered = url.lower()
    if "mp4upload" in lowered:
        return MP4UPLOAD_REFERER
    if "sharepoint" in lowered:
        return ""
    return DEFAULT_REFERER


def headers_for(url):
    headers = {"User-Agent": USER_AGENT}
    referer = referer_for(url)
    if referer:
        headers["Referer"] = referer
    return headers


def build_resolve_command(query, episode, quality="1080", select=1, ani_cli_command="ani-cli"):
    """Wywolanie ani-cli w trybie nieinteraktywnym.

    -S wybiera n-ty wynik wyszukiwania, -e numer odcinka. Razem omijaja OBA
    prompty fzf. ANI_CLI_NON_INTERACTIVE nie istnieje w aktualnym masterze —
    nie polegac na nim.
    """
    return [
        ani_cli_command,
        "-q", str(quality),
        "-S", str(select),
        "-e", str(episode),
        query,
    ]


def resolve_env(base_env=None):
    """Srodowisko dla podprocesu ani-cli.

    Dziedziczymy os.environ, gdy nie podano nic jawnie. Zbudowanie env od zera
    wyglada czysciej, ale jest bledne: ani-cli to skrypt POSIX-owy wolajacy
    curl/sed/grep i czytajacy HOME, a bez PATH nawet samo ani-cli nie zostanie
    znalezione (Python schodzi wtedy do os.defpath = "/bin:/usr/bin"). Na
    Termuksie, gdzie cale userland siedzi w /data/data/com.termux/files/usr/bin,
    oznacza to brak absolutnie wszystkiego.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["ANI_CLI_PLAYER"] = "debug"
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

    def __init__(self, ani_cli_command="ani-cli", quality="1080", timeout=60, env=None):
        self.ani_cli_command = ani_cli_command
        self.quality = quality
        self.timeout = timeout
        self.env = env

    def check(self):
        """Weryfikacja zaleznosci przy starcie — czytelny blad zamiast kryptycznego.

        Botan jest twarda zaleznoscia od ani-cli v4.15 (deszyfrowanie zrodel),
        wiec jego brak objawia sie pozniej jako niedzialajace rozwiazywanie URL.
        """
        missing = []
        if not shutil.which(self.ani_cli_command):
            missing.append(self.ani_cli_command)
        if not shutil.which("botan"):
            missing.append("botan (wymagane przez ani-cli >= 4.15)")
        return missing

    def search(self, query):
        # Stage 1. ani-cli nie ma trybu "wypisz wyniki wyszukiwania" bez fzf,
        # wiec to bedzie albo zapytanie do API allanime, albo anipy-cli.
        raise NotImplementedError("search: Stage 1 (docs.md §12)")

    def resolve(self, provider_id, episode, quality=None):
        command = build_resolve_command(
            query=provider_id,
            episode=episode,
            quality=quality or self.quality,
            ani_cli_command=self.ani_cli_command,
        )
        try:
            completed = subprocess.run(
                command,
                env=resolve_env(self.env),
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

        selected, qualities = parse_debug_output(completed.stdout)
        if not selected:
            raise SourceError(
                "Zrodlo niedostepne — zaktualizuj ani-cli (brak adresu w wyjsciu)"
            )

        return ResolvedStream(
            stream_url=selected,
            referer=referer_for(selected),
            headers=headers_for(selected),
            available_qualities=qualities,
        )
