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

### Później
- Stage 1: wyszukiwanie i katalog (AniList + cache SQLite),
- Stage 2: kanał kontrolny (self-adb, play/pause/stop),
- Stage 3: historia i wznawianie odcinka.

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
    config.py        # wczytywanie konfiguracji
    config.json      # domyślna konfiguracja
  data/
    cache/           # cache metadanych (gitignored)
    settings/        # ustawienia i historia (gitignored)
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
- mapowanie tytuł AniList → tytuł allanime przez `rapidfuzz`, z ręcznym
  rozstrzyganiem w UI przy niskim score; potwierdzone mapowanie trafia do cache.

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
- `GET  /api/anime` — lista / szczegóły (Stage 1)

**Świadomie nie ma `/api/seek`** — patrz §2.

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

### D6 — Kwestie otwarte
- Który konkretnie box i wersja Androida? Bez tego nie da się ocenić, czy
  self-adb będzie stabilny — a to decyduje, czy Stage 2 wchodzi w ogóle.
- Czy urządzenie jest zrootowane? Jeśli tak, **cała §2 przestaje obowiązywać**:
  mpv w Termuksie + IPC staje się najlepszym stackiem i dostajemy pełną kontrolę.
- Czy box uruchomi Kodi? Wtedy alternatywą jest JSON-RPC Kodi (prawdziwy seek,
  pozycja, eventy po HTTP, bez adb) — kosztem tego, że nie jest ani-cli-native.

## 12. Plan kolejnych kroków

- **Stage 0** — pipeline end-to-end: ani-cli → URL → odtwarzacz, status po SSE.
  Kryterium: odcinek leci pełnoekranowo na TV, włączony z telefonu.
- **Stage 1** — wyszukiwanie i katalog: AniList, cache SQLite, `rapidfuzz`.
- **Stage 2** — kanał kontrolny: self-adb, play/pause/stop, polling `dumpsys`.
- **Stage 3** — historia i wznawianie.

## 13. Materiały źródłowe

- `research-android-control.md` — pełny research, na którym oparty jest ten dokument.
- Biblioteka `androidtv` z Home Assistant — referencyjna implementacja sterowania
  Android TV po adb (dokładne komendy `dumpsys`, kody keyeventów, parsowanie stanu).
  Najbardziej wartościowa lektura pod Stage 2.
- `ani-tui` — najbliższy wzorzec dla katalogu, historii i wznawiania.
- `anipy-cli` — alternatywa dla ani-cli, w Pythonie, importowalna bezpośrednio.
