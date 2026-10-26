# Ani-Web — dokumentacja architektury i logiki działania

> Ten dokument został przepisany po analizie researchu technicznego
> (`research-android-control.md`). Kilka wcześniejszych założeń okazało się
> nietrafione — patrz sekcja [11. Rejestr decyzji](#11-rejestr-decyzji).

## 1. Cel projektu

Ani-Web to lokalny interfejs webowy, który zamienia telefon w pilota do
odtwarzania anime na urządzeniu z Android TV. Backend działa w Termuksie na
samym urządzeniu TV, frontend otwiera się w przeglądarce telefonu.

Główne założenia:
- UI działa lokalnie, w przeglądarce telefonu (mobile-first, kioskowy),
- backend w Termuksie na urządzeniu TV uruchamia i obsługuje odtwarzanie,
- odtwarzanie leci na fizyczny wyjściowy obraz HDMI urządzenia TV,
- system zachowuje ustawienia i historię oglądania,
- aplikacja ma działać w środowisku ograniczonym zasobowo,
- brak wymogu roota.

## 2. Fundamentalne ograniczenie (przeczytaj przed projektowaniem UI)

Na Android TV, z Termuksa, bez roota **nie da się uzyskać jednocześnie**:
odtwarzania na HDMI **i** pełnej kontroli (pauza + seek do znacznika + odczyt
bieżącej pozycji). Trzeba wybrać jedno albo drugie:

| Stack | Obraz na HDMI | Pauza | Seek do znacznika | Odczyt pozycji |
|---|---|---|---|---|
| mpv w Termuksie + socket IPC | ❌ (tylko `null`/`image`, brak hwdec) | ✅ | ✅ | ✅ |
| mpv-android / VLC przez `am start` | ✅ (hwdec) | ✅ przez media session | ❌ | ⚠️ zgrubnie, `dumpsys` |

Wybieramy **obraz na HDMI** — bez tego projekt nie ma sensu. Konsekwencja:
**model kontroli jest dwuwarstwowy**:

1. **Start odtwarzania** — `am start` z intentem do mpv-android/VLC.
   Fire-and-forget, z pozycją startową w extras.
2. **Sterowanie w trakcie** — keyevents / `media dispatch` przez **self-adb**
   (wireless debugging na `127.0.0.1`), stan czytany pollingiem
   `dumpsys media_session`.

Z tego wynika kilka twardych reguł projektowych:

- **UI nie może mieć suwaka postępu ani seeka do znacznika.** Nie mamy takich
  danych ani takiej komendy. Pole `position` z `dumpsys media_session`
  aktualizuje się **tylko przy zmianach stanu** (play/pause/seek), nie płynie.
- **Backend nie monitoruje procesu potomnego.** Intent nie tworzy procesu
  dziecka Termuksa — nie ma czego `wait()`-ować. Stan wykrywamy pollingiem.
- **Self-adb jest zawodny** (re-pairing po restarcie, losowy port). Musi być
  traktowany jako kanał best-effort, z jawnym fallbackiem do
  fire-and-forget i ukrywaniem kontrolek, których nie da się obsłużyć.

### 2.1 Środowisko developerskie ratuje testowalność

Na Linuksie ograniczenie z §2 **nie obowiązuje** — tam mpv + `--input-ipc-server`
daje pełną kontrolę i normalny obraz. Dlatego rozwijamy przeciwko
**wspólnemu interfejsowi `PlayerBackend`** z dwiema implementacjami:

- `MpvIpcPlayer` — dev na Linuksie, pełne możliwości, w pełni testowalne,
- `AndroidIntentPlayer` — prod na TV, możliwości zdegradowane,
- `SimulatedPlayer` — CI/testy, nic nie uruchamia.

Każdy backend deklaruje swoje **capabilities**, a frontend chowa kontrolki,
których dana implementacja nie wspiera. To jedyny sposób, żeby nie zbudować UI
pod nieistniejące możliwości.

## 3. Zakres funkcjonalności

### MVP (Stage 0)
- prosty interfejs HTML + CSS + JavaScript, mobile-first,
- backend Flask serwujący UI i API,
- rozwiązanie adresu strumienia przez ani-cli,
- uruchomienie odtwarzacza z pozycją startową,
- status odtwarzania pushowany do telefonu przez SSE,
- zapis ustawień (przeglądarka + backend).

### Później (kolejność zmieniona — patrz D8)
- Stage 1: wyszukiwanie i katalog (AniList + cache SQLite),
- Stage 3: historia i wznawianie odcinka,
- Stage 2: kanał kontrolny (self-adb, play/pause/stop) — ostatni, bo D6.

## 4. Architektura

### 4.1 Warstwy systemu

1. **Interfejs użytkownika** — statyczne HTML/CSS/JS w przeglądarce telefonu;
   ekran główny, szczegóły anime, ustawienia; `EventSource` na status.
2. **Backend lokalny (Flask, w Termuksie)** — serwuje frontend, wystawia API,
   trzyma stan, pushuje zdarzenia przez SSE.
3. **Warstwa źródeł (`SourceProvider`)** — izoluje ani-cli za wąskim
   kontraktem (patrz §9).
4. **Warstwa odtwarzania (`PlayerBackend`)** — izoluje sposób uruchamiania i
   sterowania odtwarzaczem (patrz §2.1).
5. **Warstwa danych** — ustawienia, cache metadanych, historia; SQLite +
   pliki JSON w `data/`.

### 4.2 Organizacja plików

```text
Ani-Web/
  README.md
  docs.md
  deployment.md
  research-android-control.md   # research źródłowy, ustalenia techniczne
  requirements.txt
  frontend/
    index.html
    styles.css
    app.js
  backend/
    server.py        # aplikacja Flask, routing, SSE
    state.py         # AppState — stan aplikacji
    players.py       # PlayerBackend + implementacje
    ani_runner.py    # SourceProvider — adapter ani-cli
    metadata.py      # katalog: AniList + Jikan + cache (Stage 1)
    mapping.py       # leniwe mapowanie tytuł -> źródło (D7)
    history.py       # historia i wznawianie (Stage 3)
    watcher.py       # dozorca stanu odtwarzania (D9)
    db.py            # schemat i połączenia SQLite
    config.py        # wczytywanie konfiguracji
    config.json      # domyślna konfiguracja
  data/              # cały katalog gitignored: baza, cache, ustawienia
  tests/
```

## 5. Logika działania

### 5.1 Start aplikacji
1. Użytkownik uruchamia serwer w Termuksie na urządzeniu TV.
2. Backend wczytuje konfigurację i wybiera `PlayerBackend`.
3. Telefon otwiera lokalny adres HTTP urządzenia TV.
4. Frontend pobiera `/api/status`, czyta capabilities i buduje UI.
5. Frontend podłącza się do `/api/events` (SSE).

### 5.2 Uruchomienie odtwarzania
1. Użytkownik wybiera anime i odcinek na telefonie.
2. Frontend woła `POST /api/play`.
3. Backend woła `SourceProvider.resolve()` — **just-in-time**, bo adresy są
   tokenizowane i krótko żyjące.
4. Backend woła `PlayerBackend.play(url, referer, position)`.
5. Backend aktualizuje stan i pushuje go przez SSE.

### 5.3 Tryb kiosk
- UI maksymalnie proste, jeden cel: włączyć odcinek,
- duże przyciski dotykowe,
- ustawienia na osobnym ekranie,
- strona dodawalna do ekranu głównego telefonu (PWA-style).

### 5.4 Cache i metadane
- AniList GraphQL jako główne źródło (bez klucza, 90 req/min, pole `synonyms`),
- Jikan v4 jako fallback (bez klucza, 60 req/min),
- agresywny cache w SQLite — metadane są niemal statyczne,
- mapowanie tytuł AniList → tytuł allanime **leniwie, dopiero przy `play`**;
  potwierdzone mapowanie trafia do cache. `rapidfuzz` okazał się niepotrzebny —
  patrz D7.

### 5.5 Ustawienia i stan użytkownika
- preferencje w `localStorage` + kopia po stronie backendu,
- historia oglądania w SQLite na urządzeniu,
- identyfikator użytkownika z ciasteczka.

## 6. Komunikacja frontend ↔ backend

- `GET  /api/status` — stan + **capabilities** aktywnego backendu
- `GET  /api/events` — SSE, push stanu odtwarzania
- `POST /api/play` — start odtwarzania
- `POST /api/pause` — pauza (tylko gdy `capabilities.pause`)
- `POST /api/stop` — zatrzymanie
- `GET  /api/settings` — odczyt ustawień
- `POST /api/settings` — zapis ustawień
- `GET  /api/anime?q=…` — wyszukiwanie w katalogu (AniList), Stage 1
- `GET  /api/anime/<anilist_id>` — szczegóły: opis, okładka, liczba odcinków
- `GET  /api/history` — ostatnio oglądane dla uid z ciasteczka, Stage 3
- `GET  /api/history/<anilist_id>` — od czego kontynuować, Stage 3
- `DELETE /api/history/<anilist_id>` — usunięcie pozycji z historii

**Świadomie nie ma `/api/seek`** — patrz §2.

`POST /api/play` przyjmuje **albo** `query` (tytuł wprost, jak dotąd), **albo**
`anilist_id` — wtedy backend sam mapuje tytuł na źródło (leniwie, patrz D7).

## 7. Problem Referera (nie do pominięcia)

Adresy strumieni z allanime **wymagają nagłówka Referer**, a `ANI_CLI_PLAYER=debug`
zwraca sam URL, bez Referera. Do tego **mpv-android nie przyjmuje Referera przez
intent** (mpv-android issue #427).

Wybrane rozwiązanie: **lokalne reverse-proxy w naszym backendzie**, które
dokleja Referer i User-Agent do żądań w stronę CDN-u. Odtwarzacz dostaje wtedy
adres wskazujący na nasz serwer i żaden Referer w intencie nie jest potrzebny.
Zaleta: działa dla dowolnego odtwarzacza, niezależnie od jego wsparcia dla
nagłówków. Alternatywy (replikacja logiki Referera w backendzie, albo oddanie
budowania intentu ścieżce `android_vlc` w ani-cli) zostają jako plan B.

## 8. Bezpieczeństwo i ograniczenia

- aplikacja działa wyłącznie w sieci lokalnej, nie jest do publicznego hostingu,
- warstwa bezpieczeństwa minimalna, bez złożonej autoryzacji,
- lokalne reverse-proxy z §7 **nie może** być otwartym proxy — tylko adresy
  rozwiązane w tej sesji, z krótkim TTL,
- allanime to źródło nielicencjonowane; projekt do użytku prywatnego,
  nie do redystrybucji jako usługa.

## 9. Izolacja zależności od ani-cli

ani-cli psuje się regularnie (zmiany domen allanime, blokady Cloudflare,
znikające źródła, a od v4.15 nowa twarda zależność od biblioteki **Botan**).
Dlatego całość ukrywamy za wąskim kontraktem:

```python
search(query) -> [ {provider_id, title, episodes} ]
resolve(provider_id, episode, quality) -> {stream_url, referer, headers, ttl}
```

Reguły:
- frontend, katalog i historia zależą **tylko** od tego interfejsu, nigdy od
  formatu wyjścia ani-cli — podmiana na `anipy-cli` to koszt jednego adaptera,
- weryfikacja zależności (ani-cli, Botan) przy starcie, z czytelnym błędem,
- wersję ani-cli trzymać aktualizowalną, ale znany-dobry stan przypięty,
- w UI komunikat „źródło niedostępne — zaktualizuj ani-cli", nie kryptyczny błąd.

Wywołanie nieinteraktywne (potwierdzone w źródle ani-cli):

```sh
ANI_CLI_PLAYER=debug ani-cli -q 1080 -S 1 -e 5 "search term"
```

`-S` (n-ty wynik) razem z `-e` (odcinek) omija oba prompty fzf.
`ANI_CLI_NON_INTERACTIVE` **nie istnieje** w aktualnym masterze — nie polegać.

## 10. Wymagania środowiska (Android TV + Termux)

Bez tych trzech rzeczy backend nie będzie działał stabilnie:

1. **„Draw over other apps" dla Termuksa** — bez tego proces w tle
   **nie może wywołać `am start`** (ograniczenie Androida 10+). Twardy wymóg.
2. **Wyłączony phantom process killer** (Android 12+) — inaczej Android zabija
   procesy potomne Termuksa (`signal 9`). Limit 32 procesów jest
   **systemowy, nie per-app**:
   `adb shell settings put global settings_enable_monitor_phantom_procs false`
3. **`termux-wake-lock`** + wyłączona optymalizacja baterii dla Termuksa.

Termux musi pochodzić z **F-Droid lub APK z GitHuba** — build z Play Store jest
niekompatybilny. Szczegóły instalacji: `deployment.md`.

## 11. Rejestr decyzji

Sekcja na świadome decyzje i na to, co research zweryfikował negatywnie.

### D1 — VLC nie jest „prostszą alternatywą dla ani-cli" (odwrócone)
Pierwotnie: „VLC ma być domyślnym odtwarzaczem, a jego obsługa ma być prostsza
i bardziej stabilna niż pełna integracja z ani-cli".
**Nieprawda.** VLC-Android nie wystawia interfejsu HTTP/RC — to funkcja
wyłącznie desktopowa, a wszystkie „VLC remote" w sklepie to piloty do VLC na
desktopie. Do tego Referer i tak musi zbudować ani-cli albo nasze proxy.
Pominięcie ani-cli nie upraszcza niczego.
**Decyzja:** ani-cli zostaje warstwą rozwiązywania źródeł, schowaną za
`SourceProvider` (§9). Domyślny odtwarzacz na TV to **mpv-android** (hwdec +
extras `position`), z VLC jako alternatywą; na Androidzie 13+ intenty VLC są
u części użytkowników zepsute (VLC issue #2776).

### D2 — Brak seeka i brak suwaka postępu (ograniczenie zaakceptowane)
Seek do dowolnego znacznika nie istnieje dla odtwarzaczy uruchamianych
intentem, a `position` z `dumpsys media_session` nie płynie w czasie.
**Decyzja:** nie wystawiamy `/api/seek` i nie rysujemy suwaka. Ewentualny
zgrubny skok ±10 s przez powtarzane media-keys — dopiero po weryfikacji na
sprzęcie, jako funkcja opcjonalna.

**Aktualizacja D2 (2026-10-26):** nadal NIE ma seeka do dowolnej pozycji ani suwaka.
Doszlo przewijanie o staly krok (capability `step`, `POST /api/seek {"delta": N}`,
N ∈ {-60, -10, 10, 60}). Na Androidzie to klawisze wysylane przez adb —
zmierzone w mpv-android na HY300: strzalki ≈ ±10 s (do klatki kluczowej), klawisze
mediow FF/REW ≈ ±60 s; `mpv-ipc` robi to komenda `seek` wzgledem pozycji.

### D11 — Lista odcinkow i miejsce na opisy (rozbudowa, nie funkcja)
UI wybiera odcinek z listy rozwijanej (szczegoly i menu po odcinku), nie
klikaniem numeru. Lista pochodzi z `backend/episodes.py`: `episode_list(meta)`
zwraca `[{"number": n}, ...]` dla wydanych odcinkow (`AnimeMeta.aired` =
`nextAiringEpisode - 1` dla serii w emisji, inaczej `episodes`). Bez liczby
odcinkow front pokazuje pole liczbowe. Element listy moze miec opcjonalne
`title`, `description`, `thumbnail`, `air_date` — front pokazuje `title` obok
numeru, reszte ignoruje. Zrodlo (ani-cli) zna tylko numery, a AniList nie daje
stabilnie tytulow odcinkow, wiec dzis NIC ich nie pobiera; dostawce opisow
dopinamy przez `extra` w `episode_list` bez zmian w reszcie kodu.

### Menu po odcinku = menu ani-cli
Odpowiada pozycjom ani-cli (`next, replay, previous, select, change_quality, quit`):
Nastepny, Znowu, Poprzedni, lista odcinkow (start od razu po wyborze), lista
jakosci (odtwarza biezacy odcinek w nowej jakosci, jak `change_quality`), zamkniecie.

### D3 — Model intentu zamiast modelu procesu (poprawka)
Pierwotnie: „backend monitoruje stan procesu i reaguje na błędy".
Na Androidzie `am start` nie tworzy procesu potomnego Termuksa.
**Decyzja:** stan wykrywamy pollingiem `dumpsys media_session` przez self-adb.
Zakończenie odtwarzania to przejście stanu, nie kod wyjścia procesu.

### D4 — Flask jako serwer HTTP
`http.server` ze stdlib by wystarczył, ale Flask instaluje się w Termuksie
z czystych wheeli (bez kompilacji), a SSE i routing są znacznie mniej boilerplate'owe.
**Świadomie odrzucone:** `uvicorn[standard]` — `uvloop`/`httptools` wymagają
kompilacji i **nie budują się w Termuksie** (uvicorn #1249, uvloop #613).
Jeśli kiedyś FastAPI, to wyłącznie `uvicorn` bez extras `[standard]`.

### D5 — Wznawianie odcinka z pozycji wyjścia
Zarówno VLC-Android (od v1.4.1) jak i mpv-android zwracają intent wynikowy
z końcową pozycją. Shellowy `am start` nie jest Activity, więc nie odbierze go
czysto.
**Decyzja na teraz:** zapisywać ostatnią znaną pozycję z `dumpsys` przy
pauzie/stopie (przybliżone). **Opcja o najwyższej dźwigni:** ~50-linijkowy APK
z Activity robiącą `startActivityForResult` i raportującą pozycję po HTTP do
backendu — podnosi wznawianie z „przybliżonego" do „dokładnego". Poza
założeniem „czysty HTML/JS + Python", więc świadomie odłożone.

### D7 — Mapowanie tytułu na źródło jest LENIWE (dopiero przy `play`)
Katalog opiera się na AniList, a odtwarzanie na allanime. To dwa niezależne
światy i trzeba je zmostkować mapowaniem tytułu.

**Decyzja:** mapowanie robimy **leniwie, dopiero przy `POST /api/play`**, nie
z góry przy wyszukiwaniu. Powód jest praktyczny: allanime potrafi zniknąć na
godziny (zaobserwowane 2026-07-30 — zagadka Cloudflare na `api.mkissa.net`,
HTTP 403 dla każdego klienta bez JS). Przy mapowaniu z góry padałoby całe
wyszukiwanie; przy leniwym **katalog, opisy i okładki działają normalnie**,
a błąd pojawia się tylko przy próbie odtworzenia konkretnego odcinka.

Mechanika: kandydatów na zapytanie do allanime bierzemy z AniList w kolejności
`romaji` → `english` → `synonyms`, i próbujemy po kolei. Pierwszy, który zwróci
adres, trafia do tabeli `mapping` w cache i od tej pory jest używany od razu.
Tym samym `rapidfuzz` z §5.4 **nie jest na tym etapie potrzebny** — ani-cli nie
udostępnia listy wyników wyszukiwania, więc nie ma czego dopasowywać rozmyto;
zostaje próbowanie kandydatów. Dodanie `rapidfuzz` ma sens dopiero, gdy
zejdziemy do API allanime bezpośrednio albo na `anipy-cli`.

### D8 — Stage 3 przed Stage 2
Historia i wznawianie nie zależą od sprzętu ani od self-adb, a Stage 2 jest
zablokowany kwestiami otwartymi z D6. **Decyzja:** kolejność to Stage 1 →
Stage 3 → Stage 2 (patrz §12).

Konsekwencja dla wznawiania: dopóki nie ma Stage 2, pozycję znamy tylko z
backendów, które ją raportują (`capabilities.position` — czyli `mpv-ipc` na
Linuksie). Dla `AniCliPlayer` i `AndroidIntentPlayer` historia zapisuje sam
**numer odcinka**, bez pozycji w czasie. To wystarcza do „kontynuuj od odcinka
N" i jest uczciwe wobec D2.

### D9 — Dozorca stanu zamiast czekania na kliknięcie
`publish()` było wołane wyłącznie z `play`/`pause`/`stop`, czyli tylko w reakcji
na akcję użytkownika. Kiedy odtwarzacz kończył odcinek albo był zamykany
z pilota, **nikt się o tym nie dowiadywał** — UI wisiał na „odtwarzanie".

**Decyzja:** osobny wątek (`watcher.py`) odpytuje odtwarzacz co
`watch_interval` sekund i pushuje zmiany przez SSE. Polling, nie zdarzenia, bo
na Androidzie nie ma czego nasłuchiwać — intent nie tworzy procesu potomnego (D3).

Daje trzy rzeczy: wykrycie zamknięcia odtwarzacza, płynny podgląd pozycji tam
gdzie backend ją raportuje, oraz sygnał końca odcinka, na którym UI buduje
pytanie „następny odcinek?" (odpowiednik menu ani-cli po odcinku).

Koniec odcinka zapisuje w historii flagę `completed`, osobną od `position` —
dzięki temu wznawianie przechodzi na następny odcinek także dla backendów,
które pozycji nie znają (`ani-cli`, `android-intent`).

Autoplay następnego odcinka jest **opcją, domyślnie wyłączoną**: zamknięcie
odtwarzacza z pilota nie może samo włączać kolejnego odcinka.

### D10 — Stage 2 zaimplementowany „na słowo honoru"
`AndroidIntentPlayer` jest napisany w całości: `am start` z extras pozycji,
pauza przez `input keyevent`, stan z `dumpsys media_session`. **Nie był
uruchomiony na żadnym urządzeniu** — nie ma jeszcze odpowiedzi na D6.

Ryzyka, które trzeba sprawdzić na sprzęcie:
- format `dumpsys media_session` nie jest publicznym API i różni się między
  wersjami Androida; parser jest defensywny, ale zgadywać nie sposób,
- czy `am start` z tła faktycznie przejdzie (uprawnienie „Draw over other apps"),
- czy self-adb na `127.0.0.1` da się utrzymać po restarcie.

**Świadoma degradacja:** bez działającego adb backend zgłasza mniejsze
`capabilities` (bez pauzy) i stan `unknown` zamiast udawać, że wie co leci.
Uruchamianie działa wtedy dalej — `am start` nie potrzebuje adb.

**Weryfikacja na sprzęcie (2026-10-26, Magcubic HY300 Pro+, Android 14, bez roota, ARMv7):**
mpv-android 2026-09-17 (`is.xyz.mpv`, build armeabi-v7a) przez `AndroidIntentPlayer` + adb:
play z pozycją startową (extras `position`, ms), pauza/wznowienie przez
`input keyevent 85`, stan i pozycja z `dumpsys media_session`, wykrycie końca
(`ended`) i `stop` — **działa end-to-end**. Pozycja z mpv płynie podczas grania
(co kilka sekund), nie tylko przy zmianie stanu — D2 jest dla mpv zbyt pesymistyczne.

Znalezione i naprawione przez test na sprzęcie (240 testów zielonych):
- Android 14 zapisuje `state=PLAYING(3)`, nie `state=3` — parser zwracał zawsze `unknown`.
- Nazwa paczki występuje też w sekcji „Audio playback … packages=…"; to nie
  jest sesja — parser musi kotwiczyć na `package=<nazwa>`.

Pułapki testowe (nie bugi): URL-e z blender.org dają 404; webm z Wikimedii nie
wspiera seeka po HTTP, więc z `position` kończy się `reason 4` — pliki do testów
muszą być seekowalnymi mp4. VLC nie zweryfikowany (pierwotny wniosek o jego
zepsuciu wynikał z 404 w moim URL-u, nie z VLC; D1 nadal zaleca mpv-android).

**Test pełnego przebiegu w Termuksie na HY300 (2026-10-26):**
- Środowisko: Python 3.14, Flask 3.1.3, ani-cli 5.1.6 (git), android-tools (adb)
  — wszystko instaluje się z `pkg`; 241 testów przechodzi na urządzeniu (~16 s).
  `pkg install android-tools` wymagało też `apt install --only-upgrade abseil-cpp libc++`
  (niezgodność wersji w repo Termuksa).
- Self-adb: `adb pair` kodem + `adb connect 127.0.0.1:5555`, potem
  `ANDROID_SERIAL=127.0.0.1:5555` (ten sam telefon widnieje też jako
  `emulator-5554`, bez zmiennej adb zgłasza „more than one device").
- Wymagane: `appops set com.termux SYSTEM_ALERT_WINDOW allow`. `am start` działa
  tylko z prawdziwej sesji Termuksa (nie z `adb shell run-as`, domena `runas_app`).
- ani-cli 5.x nie używa Botana (czek `botan` w `AniCliSource.check()` poprawiony:
  wymagany tylko, gdy skrypt ani-cli go wymienia) i ma nowe źródło (hianime/zoko).
- **Referer:** `ANI_CLI_PLAYER=debug` nie zwraca referera, a CDN daje 403 bez
  niego (mpv-android: `HTTP error 403`, stream kończy się od razu). ani-cli 5.x
  sam rozwiązuje to dla mpv-android przez plik `/storage/emulated/0/mpv/mpv.config.mp4`
  z `referrer=…` + `include=` w `mpv.conf` aplikacji. To prostsza droga niż proxy
  z §7, ale wymaga znajomości `refr` (host embedu) i dostępu do /sdcard.
  **Zweryfikowane na HY300:** `referrer=https://zokoanime.video/` w
  `/sdcard/mpv/mpv.config.mp4` + `include=` w `mpv.conf` → mpv-android gra HLS
  (PLAYING, pozycja płynie), bez tego 403. `refr` da się wyciągnąć z ani-cli,
  podstawiając jako `ANI_CLI_PLAYER` skrypt o nazwie zawierającej „mpv" i
  uruchamiając z `--no-detach` — dostaje `--referrer=…`, `--sub-file=…` i URL.
  **Wdrożone (2026-10-26):** `backend/mpv-probe` + `AniCliSource.resolve` (z zapasem na tryb
  `debug`) wyciągają Referer, napisy i audio; `AndroidIntentPlayer` zapisuje
  `/sdcard/mpv/mpv.config.mp4` (referrer, sub-file, sub-visibility=yes) przez adb
  PRZED `am start` — zawsze, nawet pusty, żeby Referer poprzedniego odcinka nie
  został. Zweryfikowane na HY300: odcinek Naruto gra z napisami (przebieg
  /api/play → ani-cli → plik → mpv). **Jednorazowo** użytkownik wpisuje w mpv-android
  (Settings → Advanced → Edit mpv.conf): `include=/storage/emulated/0/mpv/mpv.config.mp4`.
- **Konfiguracja mpv bez adb (2026-10-26):** `AndroidIntentPlayer` zapisuje plik
  najpierw bezpośrednio (Termux ma `WRITE_EXTERNAL_STORAGE`), dopiero potem przez adb;
  gdy oba zawiodą, snapshot niesie `warning`, a UI go pokazuje. Zweryfikowane
  przy rozłączonym adb. Frontend: jedno połączenie SSE (zamykane przy ukryciu karty,
  reconnect z backoffem), seed pierwszej ramki bez fałszywego „następny odcinek?",
  timeouty (10 s; 120 s dla /api/play), jedna blokada startu odtwarzania, uid w
  ciasteczku ustalany na /api/status.
- **Obciążenie przy graniu HLS (HY300, 1 GB):** mpv RES ~213 MB, ~31% jednego rdzenia
  (z 400%); serwer Ani-Web RES ~6–27 MB, ~0,2% CPU; MemAvailable 271 MB, swap wolny 434 MB.
- Zasoby (1 GB RAM, `low_ram=true`): MemAvailable 320 MB → 305 MB po starcie
  serwera (python RSS ~27 MB, ~0% CPU w spoczynku). Swap już w ~50%.

### D6 — Kwestie otwarte
- (odpowiedź 2026-10-26: Magcubic HY300 Pro+, Android 14, bez roota, Kodi nie sprawdzano)
- Który konkretnie box i wersja Androida? Bez tego nie da się ocenić, czy
  self-adb będzie stabilny — a to decyduje, czy Stage 2 wchodzi w ogóle.
- Czy urządzenie jest zrootowane? Jeśli tak, **cała §2 przestaje obowiązywać**:
  mpv w Termuksie + IPC staje się najlepszym stackiem i dostajemy pełną kontrolę.
- Czy box uruchomi Kodi? Wtedy alternatywą jest JSON-RPC Kodi (prawdziwy seek,
  pozycja, eventy po HTTP, bez adb) — kosztem tego, że nie jest ani-cli-native.

## 12. Plan kolejnych kroków

Kolejność zmieniona względem pierwotnej — patrz D8. Stage 2 jest ostatni, bo
blokują go kwestie otwarte z D6 (jaki box, root, Kodi).

- **Stage 0** — pipeline end-to-end: ani-cli → URL → odtwarzacz, status po SSE.
  Kryterium: odcinek leci pełnoekranowo na TV, włączony z telefonu.
- **Stage 1** — wyszukiwanie i katalog: AniList + Jikan jako fallback, cache
  SQLite, leniwe mapowanie tytułu na źródło (D7). Bez `rapidfuzz` — patrz D7.
- **Stage 3** — historia i wznawianie (uid z ciasteczka, SQLite).
- **Stage 2** — kanał kontrolny: self-adb, play/pause/stop, polling `dumpsys`.
  **Zaimplementowany, ale NIEZWERYFIKOWANY NA SPRZĘCIE** — patrz D10.

## 13. Materiały źródłowe

- `research-android-control.md` — pełny research, na którym oparty jest ten dokument.
- Biblioteka `androidtv` z Home Assistant — referencyjna implementacja sterowania
  Android TV po adb (dokładne komendy `dumpsys`, kody keyeventów, parsowanie stanu).
  Najbardziej wartościowa lektura pod Stage 2.
- `ani-tui` — najbliższy wzorzec dla katalogu, historii i wznawiania.
- `anipy-cli` — alternatywa dla ani-cli, w Pythonie, importowalna bezpośrednio.
