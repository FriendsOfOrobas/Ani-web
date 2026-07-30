# duff.md — podsumowanie zmian do synchronizacji

Stan na: 2026-07-30. Branch: `claude/ani-remote-project-pkyuwl`.
Baza: `0e4dd11 skeleton start` (czyli `main`).

Ten plik istnieje po to, zeby przeniesc kontekst tej sesji do czatu w VS Code.

---

## 0. WAZNE — czego tu NIE MA

Praca zostala **przerwana w polowie**. Zrobione jest:

- [x] Opcja 4 — przepisanie `docs.md` pod research
- [x] Opcja 3, czesc backendowa — moduly `config`/`players`/`ani_runner`/`state`
- [ ] **Opcja 3, `backend/server.py` jako aplikacja Flask + endpoint SSE** — NIE zrobione
- [ ] **Opcja 3, rozbicie frontendu** na `index.html` + `styles.css` + `app.js` — NIE zrobione
- [ ] **Testy nowych modulow** — NIE zrobione

Konsekwencja, o ktorej trzeba wiedziec: **`backend/server.py` jest nadal STARY**
(stdlib `http.server`) i zawiera **wlasna, przestarzala klase `AppState`**, ktora
duplikuje i kloci sie z nowa z `backend/state.py`. Stary `server.py` nie importuje
jeszcze nowych modulow. To stan przejsciowy — do posprzatania w nastepnym kroku.

Testy (`tests/test_server.py`) nadal testuja STARA klase `AppState` ze
`server.py` i przechodza (3/3). Po przepisaniu `server.py` bedzie je trzeba
przepisac razem z nim.

## 1. Co bylo powodem zmian

Research (`research-android-control.md`, wrzucony do repo) obalil kilka zalozen
z pierwotnego `docs.md`. Trzy najwazniejsze:

| Bylo w `docs.md` | Jak jest naprawde |
|---|---|
| VLC domyslny, bo „prostszy i stabilniejszy niz ani-cli" | Odwrotnie. VLC-Android **nie ma** interfejsu HTTP/RC (to desktop-only). Referer i tak musi zbudowac ani-cli albo nasze proxy. |
| „backend monitoruje stan procesu" | Na Androidzie `am start` **nie tworzy procesu potomnego** — nie ma czego `wait()`-owac. Stan tylko przez polling `dumpsys media_session`. |
| „przekazywanie prostych komend sterujacych do VLC" | play/pause ✅, stop ✅, **seek do znacznika ❌**, live position ❌. |

Do tego blad w kodzie: `AppState.start_playback()` w galezi „vlc znaleziony"
**nic nie uruchamiala** — ustawiala tylko `status="started"`. Realnie proces
startowal wylacznie przez ani-cli.

## 2. Fundamentalne ograniczenie projektu (do zapamietania)

Na Android TV, z Termuksa, bez roota **nie da sie miec jednoczesnie** obrazu na
HDMI **i** pelnej kontroli (pauza + seek + odczyt pozycji):

- mpv w Termuksie + IPC → pelna kontrola, ale **brak obrazu na HDMI**
  (tylko `null`/`image`, wariant x11 bez akceleracji sprzetowej)
- mpv-android/VLC przez `am start` → **obraz na HDMI**, ale kontrola zdegradowana

Wybrano obraz na HDMI. Dlatego model jest **dwuwarstwowy**: start przez intent,
sterowanie przez self-adb (wireless debugging na 127.0.0.1).

**Kluczowy wniosek dla developmentu:** na Linuksie to ograniczenie nie obowiazuje,
wiec rozwijamy przeciwko wspolnemu interfejsowi `PlayerBackend` z roznymi
`Capabilities`, a frontend chowa kontrolki, ktorych backend nie wspiera.

## 3. Nowe pliki i po co sa

```
research-android-control.md   research zrodlowy, wrzucony do repo dla trwalosci
requirements.txt              tylko Flask (patrz D4 nizej)
duff.md                       ten plik

backend/config.py             DEFAULTS -> config.json -> config.local.json -> ANI_WEB_*
backend/config.json           domyslna konfiguracja
backend/players.py            PlayerBackend + Capabilities + 4 implementacje
backend/ani_runner.py         SourceProvider — seam izolujacy ani-cli
backend/state.py              AppState (nowy) + SettingsStore + pub/sub pod SSE
```

### `players.py` — 4 backendy, rozne mozliwosci

| Backend | pause | seek | position | Status |
|---|---|---|---|---|
| `SimulatedPlayer` | ✅ | ❌ | ❌ | **dziala** (CI/testy, nic nie uruchamia) |
| `AniCliPlayer` | ❌ | ❌ | ❌ | **dziala** (zachowane stare zachowanie subprocess) |
| `MpvIpcPlayer` | ✅ | ✅ | ✅ | szkielet, `NotImplementedError` — Stage 0 |
| `AndroidIntentPlayer` | ✅ | ❌ | ❌ | szkielet, `NotImplementedError` — Stage 0/2 |

`select_backend(config)`: `"auto"` → `AniCliPlayer` jesli ani-cli jest w PATH,
inaczej `SimulatedPlayer`. Backend produkcyjny wskazuje sie jawnie w
`config.local.json` (gitignored).

### `ani_runner.py` — testowalne kawalki

Czyste funkcje, sprawdzalne bez ani-cli na maszynie:

- `build_resolve_command()` → `['ani-cli', '-q', '1080', '-S', '1', '-e', '5', 'query']`
  (`-S` + `-e` omija **oba** prompty fzf)
- `parse_debug_output()` → parsuje format `<quality> ><url>` z `ANI_CLI_PLAYER=debug`
- `referer_for()` → mp4upload → `https://www.mp4upload.com`,
  sharepoint → brak, domyslnie → `https://youtu-chan.com`
- `AniCliSource.check()` → weryfikuje ani-cli **i botan** (twarda zaleznosc od v4.15)

Uwaga: `ANI_CLI_NON_INTERACTIVE` **nie istnieje** w aktualnym masterze ani-cli —
nie polegac (jest tylko w starym mirrorze man-page'a).

## 4. Decyzje zapisane w `docs.md` §11

- **D1** — ani-cli zostaje warstwa rozwiazywania zrodel, schowana za
  `SourceProvider`. Domyslny odtwarzacz na TV: **mpv-android** (hwdec + extras
  `position`), VLC jako alternatywa. Na Androidzie 13+ intenty VLC sa u czesci
  uzytkownikow zepsute (VLC #2776).
- **D2** — **brak `/api/seek` i brak suwaka postepu.** Nie mamy takich danych.
  `position` z `dumpsys` aktualizuje sie tylko przy zmianach stanu, nie plynie.
- **D3** — model intentu, nie model procesu.
- **D4** — **Flask**. Swiadomie odrzucone: `uvicorn[standard]` — uvloop/httptools
  wymagaja kompilacji i **nie buduja sie w Termuksie** (uvicorn #1249, uvloop #613).
- **D5** — wznawianie z pozycji wyjscia; na teraz przybliżone z `dumpsys`.
  Opcja o najwyzszej dzwigni: ~50-linijkowy APK z `startActivityForResult`.
- **D6, kwestie otwarte** — trzy pytania blokujace Stage 2:
  1. Ktory box i jaka wersja Androida? (decyduje, czy self-adb bedzie stabilny)
  2. **Czy urzadzenie jest zrootowane?** Jesli tak, **cala §2 przestaje
     obowiazywac** — mpv w Termuksie + IPC staje sie najlepszym stackiem.
  3. Czy box uruchomi Kodi? Wtedy alternatywa jest JSON-RPC Kodi (prawdziwy
     seek, pozycja, eventy po HTTP, bez adb).

## 5. Problem Referera (docs.md §7)

`ANI_CLI_PLAYER=debug` zwraca URL **bez** Referera, a strumienie allanime go
wymagaja. Dodatkowo **mpv-android nie przyjmuje Referera przez intent**
(mpv-android #427).

Wybrane rozwiazanie: **lokalne reverse-proxy w naszym backendzie**, doklejajace
Referer i UA. Odtwarzacz dostaje adres wskazujacy na nasz serwer i zaden Referer
w intencie nie jest potrzebny — dziala dla dowolnego odtwarzacza.
Plan B: oddac budowanie intentu sciezce `android_vlc` w ani-cli.

## 6. Wymagania srodowiska (bez tego nie zadziala)

1. **„Draw over other apps" dla Termuksa** — bez tego proces w tle **nie moze
   wywolac `am start`** (Android 10+). Twardy wymog.
2. **Wylaczony phantom process killer** (Android 12+, limit 32 procesow jest
   **systemowy, nie per-app**):
   `adb shell settings put global settings_enable_monitor_phantom_procs false`
3. **`termux-wake-lock`** + wylaczona optymalizacja baterii.

Termux musi byc z **F-Droid albo APK z GitHuba** — build z Play Store jest niekompatybilny.

## 7. Sprzatanie repo

Dodany `.gitignore` (`__pycache__/`, `*.py[cod]`, `.venv/`, `data/cache/`,
`data/settings/`, `backend/config.local.json`) i usuniete z repo dwa
zacommitowane wczesniej pliki `.pyc`.

## 8. Stan gita — PUSH JEST ZABLOKOWANY

Zdalne repo **nie przyjmuje pushy** z tej sesji:

```
GET .../info/refs?service=git-upload-pack   -> 200 OK   (czytanie dziala)
GET .../info/refs?service=git-receive-pack  -> 403
Permission to FriendsOfOrobas/Ani-web.git denied to FriendsOfOrobas.
```

GitHub App tej sesji ma dostep **read-only**. Probowane 3x z backoffem plus
`add_repo` z `access: "push"` (zwrocil `already_present`, nie podniosl uprawnien).

Do tego: `git ls-remote` pokazuje na zdalnym **tylko `main`** na `0e4dd11`.
Branch `claude/ani-remote-project-pkyuwl` **nie istnieje na remote** — lokalny
ref `remotes/origin/...` byl tylko zaseedowany przy starcie sesji.
**Nic z tej sesji nie jest na GitHubie.** Dlatego dostajesz patcha.

## 9. Jak to nalozyc u siebie

```sh
git checkout -b claude/ani-remote-project-pkyuwl   # jesli nie masz
git am < ani-web-changes.patch                     # zachowuje commity i opisy
# albo, jesli git am marudzi:
git apply ani-web-changes.patch
```

Weryfikacja po nalozeniu:

```sh
python3 -m unittest discover -s tests      # ma byc 3/3 OK
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd backend && python3 -c "import config, players, ani_runner, state; print('ok')"
```

## 10. Nastepny krok

Dokonczyc opcje 3, w tej kolejnosci:

1. `backend/server.py` → aplikacja Flask (factory), routing z `docs.md` §6,
   endpoint `GET /api/events` (SSE) karmiony z `AppState.subscribe()`.
   **Usunac stara klase `AppState` ze `server.py`** — zostaje ta ze `state.py`.
2. Frontend → `index.html` + `styles.css` + `app.js`; `app.js` czyta
   `capabilities` z `/api/status` i chowa nieobslugiwane kontrolki;
   `EventSource` na `/api/events`.
3. Testy → `test_config.py`, `test_players.py`, `test_ani_runner.py`
   (budowanie komendy + parsowanie wyjscia debug + logika Referera),
   przepisac `test_server.py` pod nowa strukture.

Potem Stage 0 z `docs.md` §12: pipeline end-to-end.
