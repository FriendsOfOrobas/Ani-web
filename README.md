# Ani-Web

Webowe GUI do ani-cli. Telefon jest pilotem, obraz leci na HDMI urządzenia
z Android TV, backend chodzi w Termuksie na tym urządzeniu. Rozwijane i
testowane na Linuksie.

Stack jest celowo chudy: czysty HTML + CSS + JS bez frameworka, Python z jedną
zależnością (Flask), SQLite na cache i historię.

- **[docs.md](docs.md)** — architektura, logika działania, **rejestr decyzji**
  (D1–D8) i plan etapów. Zacznij tam, jeśli chcesz zrozumieć *dlaczego*.
- **[deployment.md](deployment.md)** — instalacja pod Termuksa i Android TV.
- **[research-android-control.md](research-android-control.md)** — research
  techniczny, na którym oparte są decyzje.
- **[duff.md](duff.md)** — zapis przenoszenia kontekstu między sesjami.

## Co działa dzisiaj

| Etap | Zakres | Stan |
|---|---|---|
| Stage 0 | ani-cli → URL → odtwarzacz, status po SSE | **działa** |
| Stage 1 | katalog: szukanie, opisy, okładki, cache | **działa** |
| Stage 3 | historia i wznawianie odcinka | **działa** |
| Stage 2 | kanał kontrolny przez self-adb (pauza/stan na Androidzie) | **zablokowany** — patrz docs.md D6 |

Odtwarzacze (`PlayerBackend`) — każdy deklaruje swoje `capabilities`, a UI chowa
kontrolki, których dany backend nie obsługuje:

| Backend | Uruchamia | pauza | pozycja | Stan |
|---|---|---|---|---|
| `simulated` | nic | ✅ | ❌ | działa — do klikania UI i do testów |
| `ani-cli` | ani-cli sam odpala odtwarzacz | ❌ | ❌ | działa |
| `mpv-ipc` | mpv + socket JSON IPC | ✅ | ✅ | **niesprawdzone z prawdziwym mpv** |
| `android-intent` | `am start` do mpv-android/VLC | ✅ | ❌ | szkielet, Stage 2 |

## Uruchomienie na Linuksie

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python backend/server.py --player simulated
```

Potem <http://127.0.0.1:8000>. `--player simulated` niczego nie odtwarza, ale
katalog, historia i całe UI działają — to najszybszy sposób, żeby cokolwiek
zobaczyć.

Z telefonu w tej samej sieci: dodaj `--host 0.0.0.0` i wejdź na IP maszyny.

Testy:

```sh
.venv/bin/python -m unittest discover -s tests
```

Nie ruszają sieci ani ani-cli — wszystko na atrapach, włącznie z atrapą socketu
IPC mpv.

### Prawdziwe odtwarzanie

Potrzebne **osobno**: źródło (ani-cli) i odtwarzacz (mpv). Samo mpv nie
wystarczy — `/api/play` najpierw rozwiązuje adres przez ani-cli.

```sh
# ani-cli nie jest w repozytoriach Fedory
git clone https://github.com/pystardust/ani-cli && sudo cp ani-cli/ani-cli /usr/local/bin/
sudo dnf install botan2 mpv        # botan jest twardą zależnością ani-cli >= 4.15

.venv/bin/python backend/server.py --player mpv-ipc
```

Backend sprawdza zależności przy starcie i wypisuje, czego brakuje.

Bez `--player` wchodzi `auto`: wybiera `ani-cli` jeśli jest w `PATH`, inaczej
`simulated`. **`auto` nigdy nie wybierze `mpv-ipc`** — backend produkcyjny
wskazuje się jawnie, w `--player` albo w `backend/config.local.json`.

### Uwaga: VS Code jako Flatpak

Jeśli odpalasz z terminala VS Code zainstalowanego jako Flatpak, backend **nie
zobaczy** ani-cli, botana ani mpv z hosta — Flatpak ma własne `/usr/bin`.
Objaw: „Nie znaleziono ani-cli" przy poprawnie zainstalowanym ani-cli.
Rozwiązanie: uruchamiaj z **normalnego terminala hosta**. Dopisanie
`/run/host/usr/bin` do `PATH` nie pomoże — binarki hosta nie mają w sandboxie
swoich bibliotek.

Venv utworzony w Flatpaku też nie zadziała na hoście (linkuje do wersji Pythona
z runtime'u Flatpaka) — trzeba go odtworzyć.

## Konfiguracja

Kolejność nadpisywania, od najsłabszej: `DEFAULTS` → `backend/config.json` →
`backend/config.local.json` → zmienne `ANI_WEB_*` → argumenty CLI.

`config.local.json` jest w `.gitignore` — tam wpisuj ustawienia konkretnego
urządzenia.

```jsonc
{
  "player": "mpv-ipc",              // wymuszenie backendu
  "quality": "1080",
  "allanime_referer": "https://mkissa.to",  // musi zgadzać się z ani-cli!
  "data_dir": "data"
}
```

`allanime_referer` **musi** odpowiadać `allanime_refr` w zainstalowanym ani-cli.
allanime zmienia domeny, więc przy aktualizacji ani-cli warto sprawdzić:

```sh
grep -E '^allanime_refr=' "$(command -v ani-cli)"
```

Zły Referer objawia się tym, że adres się rozwiązuje, ale CDN odrzuca odtwarzanie.

## API

```
GET    /api/status              stan + capabilities aktywnego backendu
GET    /api/events              SSE, push stanu odtwarzania
POST   /api/play                { query | anilist_id, episode, quality, position }
POST   /api/pause               tylko gdy capabilities.pause
POST   /api/stop
GET    /api/anime?q=…           szukanie w katalogu (AniList)
GET    /api/anime/<anilist_id>  opis, okładka, podpowiedź wznowienia
GET    /api/history             ostatnio oglądane (uid z ciasteczka)
GET    /api/history/<id>        od czego kontynuować
DELETE /api/history/<id>        usunięcie z historii
GET    /api/settings            odczyt ustawień
POST   /api/settings            zapis ustawień
```

Kody błędów mówią, **co** padło: `502 kind=source` (źródło strumieni),
`502 kind=catalogue` (AniList), `409 kind=player` (backend nie potrafi),
`501 kind=todo` (jeszcze nie zrobione).

**Nie ma `/api/seek` i nie będzie** — patrz docs.md D2. Na Androidzie nie da się
przeskoczyć do znacznika ani odczytać płynnej pozycji, więc UI nie ma suwaka
postępu.

## Znane ograniczenia

- **Katalog i źródło są niezależne.** Metadane idą z AniList, strumienie
  z allanime. Gdy allanime padnie (Cloudflare bywa włączany), szukanie i opisy
  działają dalej, a błąd pojawia się dopiero przy „Odtwórz" — to celowe (D7).
- **ani-cli psuje się regularnie.** Dlatego siedzi za `SourceProvider` (§9
  docs.md) — podmiana na `anipy-cli` to koszt jednego adaptera.
- **Na Android TV nie ma pełnej kontroli bez roota.** Obraz na HDMI wyklucza
  seek i płynną pozycję — docs.md §2. Jeśli urządzenie **jest** zrootowane,
  całe to ograniczenie znika (D6).
- Projekt do użytku prywatnego; allanime jest źródłem nielicencjonowanym.
  Nie jest to rzecz do publicznego hostingu — docs.md §8.

---

**Pytania i decyzje „co do większego obrazu" dopisuj tutaj albo do rejestru
decyzji w docs.md §11.** Otwarte na teraz (D6, blokują Stage 2):

1. Który konkretnie box i wersja Androida?
2. Czy urządzenie jest zrootowane? Jeśli tak, docs.md §2 przestaje obowiązywać.
3. Czy box uruchomi Kodi? Wtedy alternatywą jest JSON-RPC Kodi — prawdziwy seek
   i pozycja po HTTP, bez adb.
