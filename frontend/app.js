"use strict";

/*
 * Frontend jest celowo glupi: nie zna ani-cli, nie zna Androida, nie zaklada
 * co odtwarzacz potrafi. Wszystko czyta z `capabilities` w /api/status i chowa
 * kontrolki, ktorych aktywny backend nie obsluguje (docs.md §2.1).
 *
 * Suwaka postepu tu nie ma i nie ma byc — docs.md D2.
 *
 * Katalog (AniList) i zrodlo (allanime) sa rozdzielone: wyszukiwanie i opisy
 * dzialaja nawet gdy zrodlo padnie, a blad pojawia sie dopiero przy Odtworz
 * (docs.md D7). Dlatego bledy `kind: "catalogue"` i `kind: "source"` sa
 * pokazywane, ale zaden z nich nie blokuje drugiej czesci UI.
 */

const el = (id) => document.getElementById(id);

const ui = {
  error: el("error"),
  navBack: el("nav-back"),
  navSettings: el("nav-settings"),

  screens: {
    home: el("screen-home"),
    detail: el("screen-detail"),
    settings: el("screen-settings"),
  },

  searchForm: el("search-form"),
  search: el("search"),
  continueSection: el("continue-section"),
  continueList: el("continue-list"),
  resultsSection: el("results-section"),
  resultsList: el("results-list"),
  homeHint: el("home-hint"),

  detailCover: el("detail-cover"),
  detailTitle: el("detail-title"),
  detailFacts: el("detail-facts"),
  detailDescription: el("detail-description"),
  detailEpisode: el("detail-episode"),
  detailEpisodeNumber: el("detail-episode-number"),
  detailQuality: el("detail-quality"),
  detailResume: el("detail-resume"),
  detailPlay: el("detail-play"),
  detailForget: el("detail-forget"),

  setQuality: el("set-quality"),
  settingsSave: el("settings-save"),
  settingsHint: el("settings-hint"),
  directQuery: el("direct-query"),
  directEpisode: el("direct-episode"),
  directPlay: el("direct-play"),
  capabilities: el("capabilities"),

  endedPrompt: el("ended-prompt"),
  endedTitle: el("ended-title"),
  endedSub: el("ended-sub"),
  endedNext: el("ended-next"),
  endedReplay: el("ended-replay"),
  endedClose: el("ended-close"),
  endedPrev: el("ended-prev"),
  endedEpisode: el("ended-episode"),
  endedEpisodeNumber: el("ended-episode-number"),
  endedQuality: el("ended-quality"),
  seekRow: el("seek-row"),

  setAutoplay: el("set-autoplay"),
  setSkip: el("set-skip"),

  nowbar: el("nowbar"),
  conn: el("conn"),
  adbHint: el("adb-hint"),
  stateDot: el("state-dot"),
  nowTitle: el("now-title"),
  nowMeta: el("now-meta"),
  pause: el("pause"),
  stop: el("stop"),
};

const STATE_LABELS = {
  idle: "bezczynny",
  playing: "odtwarzanie",
  paused: "pauza",
  stopped: "zatrzymany",
  ended: "koniec",
  unknown: "nieznany",
  offline: "brak połączenia",
  starting: "uruchamianie…",
};

const CAPABILITY_LABELS = {
  play: "odtwarzanie",
  pause: "pauza",
  stop: "zatrzymanie",
  seek: "przeskakiwanie",
  position: "odczyt pozycji",
  step: "przewijanie o krok",
  ended_events: "wykrycie końca",
};

const FORMAT_LABELS = {
  TV: "serial",
  TV_SHORT: "serial krótki",
  MOVIE: "film",
  SPECIAL: "special",
  OVA: "OVA",
  ONA: "ONA",
  MUSIC: "teledysk",
};

let capabilities = {};
let currentDetail = null;
let defaultQuality = "1080";
let autoplayNext = false;
let skipIntro = false;
// Ostatnio odtwarzane — pytanie po odcinku musi wiedziec, co wlaczyc dalej,
// a `current` jest wtedy juz wyczyszczone przez backend.
let lastPlayed = null;
let lastState = null;
// Zatrzymanie przez uzytkownika NIE moze wywolac pytania "nastepny odcinek?".
let userStopped = false;

const ACTIVE_STATES = new Set(["playing", "paused", "starting"]);
const FINISHED_STATES = new Set(["ended", "idle", "stopped"]);

// --- pomocnicze -----------------------------------------------------------

function showError(message) {
  ui.error.textContent = message || "";
  ui.error.hidden = !message;
}

function formatTime(seconds) {
  if (typeof seconds !== "number" || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// Zwykle wywolania maja krotki limit; start odtwarzania moze trwac dlugo
// (ani-cli + sonda + ewentualny powrot do trybu debug na slabym urzadzeniu).
const API_TIMEOUT_MS = 10000;
const PLAY_TIMEOUT_MS = 120000;

// --- lista odcinkow ------------------------------------------------------
//
// Lista rozwijana zamiast klikania numeru. Element listy to {number, ...}; tytul
// (gdy kiedys bedzie) pokazujemy obok numeru, reszte pol ignorujemy — patrz
// backend/episodes.py. Bez listy (nieznana liczba odcinkow) zostaje pole liczbowe.

function episodeLabel(item) {
  return item.title ? `${item.number}. ${item.title}` : `Odcinek ${item.number}`;
}

function fillEpisodes(select, numberInput, list, selected) {
  const hasList = Array.isArray(list) && list.length > 0;
  select.hidden = !hasList;
  numberInput.hidden = hasList;
  const label = select.closest(".field")?.querySelector("label");
  if (label) label.htmlFor = hasList ? select.id : numberInput.id;

  select.replaceChildren();
  if (!hasList) {
    numberInput.value = selected;
    return;
  }
  const options = document.createDocumentFragment();
  for (const item of list) {
    const option = document.createElement("option");
    option.value = String(item.number);
    option.textContent = episodeLabel(item);
    options.append(option);
  }
  select.append(options);
  selectEpisode(select, numberInput, selected);
}

function selectEpisode(select, numberInput, number) {
  if (select.hidden) {
    numberInput.value = number;
    return;
  }
  const exists = Array.from(select.options).some((o) => o.value === String(number));
  select.value = exists ? String(number) : select.options[select.options.length - 1]?.value;
}

function readEpisode(select, numberInput) {
  return Number(select.hidden ? numberInput.value : select.value) || 1;
}

async function api(path, options, timeoutMs = API_TIMEOUT_MS) {
  let response;
  try {
    response = await fetch(path, { ...options, signal: AbortSignal.timeout(timeoutMs) });
  } catch (err) {
    // Surowe "Failed to fetch" nic nie mowi uzytkownikowi z telefonu.
    if (err.name === "AbortError" || err.name === "TimeoutError") {
      throw new Error("Serwer nie odpowiedział w czasie — spróbuj ponownie.");
    }
    throw new Error("Brak połączenia z serwerem.");
  }
  let data = {};
  try {
    data = await response.json();
  } catch {
    // Puste albo nie-JSON — zostaje sam kod statusu.
  }
  if (!response.ok) {
    const error = new Error(data.error || `Błąd ${response.status}`);
    error.kind = data.kind;
    throw error;
  }
  return data;
}

const post = (path, body, timeoutMs) =>
  api(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    },
    timeoutMs
  );

// Przyciski ze zmienna etykieta (np. Pauza/Wznow) trzymaja ja w data-label,
// zeby withBusy nie przywrocil starej po zmianie stanu.
async function withBusy(button, action) {
  const label = button.dataset.label ?? button.textContent;
  button.disabled = true;
  showError("");
  try {
    return await action();
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = button.dataset.label ?? label;
  }
}

// Jedno odtwarzanie naraz: Odtworz, Nastepny, Znowu, odtwarzanie po tytule i
// autoplay moga sie zbiec (np. dwie ramki "ended") i dwa razy uderzyc w /api/play.
let playInFlight = false;

async function guardedPlay(action) {
  if (playInFlight) throw new Error("Odtwarzanie już się uruchamia…");
  playInFlight = true;
  userStopped = false;
  try {
    return await action();
  } finally {
    playInFlight = false;
  }
}

// --- nawigacja ------------------------------------------------------------

let screenStack = ["home"];

function show(name) {
  for (const [key, node] of Object.entries(ui.screens)) {
    node.hidden = key !== name;
  }
  ui.navBack.hidden = name === "home";
  ui.navSettings.hidden = name !== "home";
  window.scrollTo(0, 0);
}

function goTo(name) {
  if (screenStack[screenStack.length - 1] !== name) screenStack.push(name);
  show(name);
}

// Ostatnio kliknieta karta: po "Wroc" fokus (klawiatura, pilot, czytnik) ma
// wrocic tam, skad uzytkownik przyszedl, a nie na poczatek strony.
let lastCardButton = null;

ui.navBack.addEventListener("click", () => {
  screenStack.pop();
  const target = screenStack[screenStack.length - 1] || "home";
  show(target);
  if (target === "home" && lastCardButton && lastCardButton.isConnected) {
    lastCardButton.focus();
  }
});

ui.navSettings.addEventListener("click", () => goTo("settings"));

// --- karty anime ----------------------------------------------------------

function animeCard(item, extra) {
  const li = document.createElement("li");
  li.className = "card-item";

  const button = document.createElement("button");
  button.className = "card-button";
  button.type = "button";
  button.addEventListener("click", () => {
    lastCardButton = button;
    openDetail(item.anilist_id);
  });

  if (item.cover) {
    const img = document.createElement("img");
    img.src = item.cover;
    img.alt = "";
    img.loading = "lazy";
    img.decoding = "async";
    button.append(img);
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = "cover-placeholder";
    placeholder.textContent = item.title;
    button.append(placeholder);
  }

  const caption = document.createElement("span");
  caption.className = "card-caption";

  const title = document.createElement("strong");
  title.textContent = item.title;
  caption.append(title);

  const sub = document.createElement("small");
  sub.textContent = extra || describeAnime(item);
  caption.append(sub);

  button.append(caption);
  li.append(button);
  return li;
}

function describeAnime(item) {
  const bits = [];
  if (item.year) bits.push(item.year);
  if (item.format) bits.push(FORMAT_LABELS[item.format] || item.format.toLowerCase());
  if (item.episodes) bits.push(`${item.episodes} odc.`);
  return bits.join(" · ");
}

// --- szukanie -------------------------------------------------------------

// Numer zapytania: wolna odpowiedz na "nar" nie moze nadpisac wyniku "naruto".
let searchSeq = 0;

ui.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = ui.search.value.trim();
  if (!query) return;

  return withBusy(ui.searchForm.querySelector("button"), async () => {
    const seq = ++searchSeq;
    ui.homeHint.textContent = "Szukam…";
    const data = await api(`/api/anime?q=${encodeURIComponent(query)}`);
    if (seq !== searchSeq) return;
    ui.resultsList.replaceChildren(...data.results.map((item) => animeCard(item)));
    ui.resultsSection.hidden = data.results.length === 0;
    ui.homeHint.textContent = data.results.length
      ? ""
      : `Brak wyników dla „${query}”.`;
  });
});

// --- szczegoly ------------------------------------------------------------

function renderResume(resume, entry) {
  if (!resume) {
    ui.detailResume.textContent = "";
    return;
  }
  selectEpisode(ui.detailEpisode, ui.detailEpisodeNumber, resume.episode);

  if (resume.reason === "wznowienie") {
    ui.detailResume.textContent = `Wznowisz odcinek ${resume.episode} od ${formatTime(resume.position)}.`;
  } else if (resume.reason === "nastepny odcinek") {
    ui.detailResume.textContent = `Poprzedni odcinek dokończony — proponuję odcinek ${resume.episode}.`;
  } else if (resume.reason === "koniec serii") {
    ui.detailResume.textContent = "Obejrzane do końca.";
  } else if (entry) {
    ui.detailResume.textContent = `Ostatnio odcinek ${resume.episode}.`;
  } else {
    ui.detailResume.textContent = "";
  }
}

// Numer otwarcia: dwa szybkie klikniecia kart nie moga zostawic widoku jednego
// anime z przyciskiem Odtworz wskazujacym na drugie.
let detailSeq = 0;

async function openDetail(anilistId) {
  const seq = ++detailSeq;
  goTo("detail");
  ui.detailTitle.focus();
  currentDetail = null;
  ui.detailTitle.textContent = "Ładowanie…";
  ui.detailFacts.textContent = "";
  ui.detailDescription.textContent = "";
  ui.detailCover.hidden = true;
  ui.detailForget.hidden = true;
  ui.detailResume.textContent = "";

  try {
    const meta = await api(`/api/anime/${anilistId}`);
    if (seq !== detailSeq) return;
    currentDetail = meta;

    ui.detailTitle.textContent = meta.title;
    ui.detailFacts.textContent = [describeAnime(meta), meta.score ? `${meta.score}/100` : ""]
      .filter(Boolean)
      .join(" · ");
    ui.detailDescription.textContent = meta.description || "Brak opisu.";
    if (meta.cover) {
      ui.detailCover.src = meta.cover;
      ui.detailCover.hidden = false;
    }
    ui.detailQuality.value = defaultQuality;
    ui.detailEpisodeNumber.max = meta.aired || meta.episodes || "";
    fillEpisodes(ui.detailEpisode, ui.detailEpisodeNumber, meta.episode_list, 1);

    const history = await api(`/api/history/${anilistId}`);
    if (seq !== detailSeq) return;
    ui.detailForget.hidden = !history.entry;
    renderResume(meta.resume, history.entry);
  } catch (err) {
    if (seq !== detailSeq) return;
    showError(err.message);
    ui.detailTitle.textContent = "Nie udało się wczytać";
  }
}

function playEpisode({
  anilistId, episode, quality, position = 0, title, totalEpisodes, episodeList,
}) {
  return guardedPlay(() =>
    startEpisode({ anilistId, episode, quality, position, title, totalEpisodes, episodeList })
  );
}

async function startEpisode({
  anilistId, episode, quality, position, title, totalEpisodes, episodeList,
}) {
  const snapshot = await post(
    "/api/play",
    {
      anilist_id: anilistId,
      episode,
      quality: quality || defaultQuality,
      position,
      skip_intro: skipIntro,
    },
    PLAY_TIMEOUT_MS
  );
  lastPlayed = {
    anilistId,
    episode,
    quality: quality || defaultQuality,
    title: title || snapshot.current?.title || "",
    totalEpisodes: totalEpisodes || snapshot.current?.total_episodes || 0,
    episodeList: episodeList || [],
  };
  hideEndedPrompt();
  render(snapshot);
  loadContinue();
  return snapshot;
}

ui.detailPlay.addEventListener("click", () =>
  withBusy(ui.detailPlay, async () => {
    if (!currentDetail) return;
    ui.detailPlay.textContent = "Szukam źródła…";
    await playEpisode({
      anilistId: currentDetail.anilist_id,
      episode: readEpisode(ui.detailEpisode, ui.detailEpisodeNumber),
      quality: ui.detailQuality.value,
      position: Number(currentDetail.resume?.position || 0),
      title: currentDetail.title,
      totalEpisodes: currentDetail.aired || currentDetail.episodes,
      episodeList: currentDetail.episode_list,
    });
  })
);

ui.detailForget.addEventListener("click", () =>
  withBusy(ui.detailForget, async () => {
    // Przycisk leży tuz pod "Odtworz", pod kciukiem — bez pytania latwo o pomylke.
    if (!window.confirm("Usunąć to anime z historii oglądania?")) return;
    await api(`/api/history/${currentDetail.anilist_id}`, { method: "DELETE" });
    ui.detailForget.hidden = true;
    ui.detailResume.textContent = "";
    selectEpisode(ui.detailEpisode, ui.detailEpisodeNumber, 1);
    loadContinue();
  })
);

// --- kontynuuj ------------------------------------------------------------

async function loadContinue() {
  try {
    const data = await api("/api/history");
    const entries = data.entries || [];
    ui.continueList.replaceChildren(
      ...entries.map((entry) =>
        animeCard(
          {
            anilist_id: entry.anilist_id,
            title: entry.title || `#${entry.anilist_id}`,
            cover: entry.cover,
          },
          entry.position
            ? `odc. ${entry.episode} · ${formatTime(entry.position)}`
            : `odc. ${entry.episode}`
        )
      )
    );
    ui.continueSection.hidden = entries.length === 0;
  } catch {
    // Historia to dodatek — jej brak nie moze psuc ekranu glownego.
    ui.continueSection.hidden = true;
  }
}

// --- odtwarzanie po tytule (obejscie katalogu) ----------------------------

ui.directPlay.addEventListener("click", () =>
  withBusy(ui.directPlay, async () => {
    const query = ui.directQuery.value.trim();
    if (!query) {
      showError("Podaj tytuł.");
      return;
    }
    await guardedPlay(async () =>
      render(
        await post(
          "/api/play",
          {
            query,
            episode: Number(ui.directEpisode.value) || 1,
            quality: defaultQuality,
          },
          PLAY_TIMEOUT_MS
        )
      )
    );
  })
);

// --- menu po odcinku (to samo, co w ani-cli) ------------------------------
//
// ani-cli po odcinku pokazuje: next, replay, previous, select, change_quality,
// quit. Tu: Nastepny, Znowu, Poprzedni, lista odcinkow, lista jakosci, zamkniecie.

function hideEndedPrompt() {
  ui.endedPrompt.hidden = true;
}

function showEndedPrompt(finished) {
  if (!lastPlayed) return;

  const last = lastPlayed.totalEpisodes && lastPlayed.episode >= lastPlayed.totalEpisodes;
  ui.endedTitle.textContent = finished
    ? `Odcinek ${lastPlayed.episode} skończony`
    : "Odtwarzacz zamknięty";
  ui.endedSub.textContent = last
    ? `${lastPlayed.title} — to był ostatni odcinek.`
    : `${lastPlayed.title} — następny: ${lastPlayed.episode + 1}.`;
  ui.endedNext.hidden = Boolean(last);
  ui.endedPrev.hidden = lastPlayed.episode <= 1;
  fillEpisodes(ui.endedEpisode, ui.endedEpisodeNumber, lastPlayed.episodeList, lastPlayed.episode);
  ui.endedQuality.value = lastPlayed.quality;
  ui.endedPrompt.hidden = false;
  // Fokus na akcji wiodacej, zeby pilot/klawiatura nie musialy tabowac przez
  // cala strone do pytania, ktore wlasnie sie pojawilo.
  (last ? ui.endedReplay : ui.endedNext).focus({ preventScroll: true });

  // Autoplay jest opcja, nie domyslna: przy zamknieciu odtwarzacza z pilota
  // automatyczne wlaczanie kolejnego odcinka byloby wbrew intencji.
  if (autoplayNext && finished && !last) {
    playNext().catch((err) => showError(err.message));
  }
}

// Odcinek `episode` tego samego anime, opcjonalnie w innej jakosci. Reszta
// (tytul, liczba odcinkow, lista) zostaje z poprzedniego odtwarzania.
function playOther(episode, quality) {
  return playEpisode({
    anilistId: lastPlayed.anilistId,
    episode,
    quality: quality || lastPlayed.quality,
    title: lastPlayed.title,
    totalEpisodes: lastPlayed.totalEpisodes,
    episodeList: lastPlayed.episodeList,
  });
}

function playNext() {
  return playOther(lastPlayed.episode + 1);
}

// Zmiana wyboru z listy startuje odtwarzanie od razu (jak "select" w ani-cli),
// a blad pokazuje sie jak przy kazdej innej akcji.
async function playFromMenu(action) {
  showError("");
  try {
    await action();
  } catch (err) {
    showError(err.message);
  }
}

ui.endedNext.addEventListener("click", () => withBusy(ui.endedNext, playNext));
ui.endedReplay.addEventListener("click", () =>
  withBusy(ui.endedReplay, () => playOther(lastPlayed.episode))
);
ui.endedPrev.addEventListener("click", () =>
  withBusy(ui.endedPrev, () => playOther(Math.max(1, lastPlayed.episode - 1)))
);
ui.endedEpisode.addEventListener("change", () =>
  playFromMenu(() => playOther(Number(ui.endedEpisode.value) || 1))
);
ui.endedEpisodeNumber.addEventListener("change", () =>
  playFromMenu(() => playOther(Number(ui.endedEpisodeNumber.value) || 1))
);
// "change_quality" w ani-cli odtwarza biezacy odcinek w nowej jakosci.
ui.endedQuality.addEventListener("change", () =>
  playFromMenu(() => playOther(lastPlayed.episode, ui.endedQuality.value))
);
ui.endedClose.addEventListener("click", hideEndedPrompt);

// --- przewijanie o krok (+-10 s, +-60 s) -----------------------------------

ui.seekRow.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-delta]");
  if (!button) return;
  withBusy(button, async () => {
    render(await post("/api/seek", { delta: Number(button.dataset.delta) }));
  });
});

// --- pasek odtwarzania ----------------------------------------------------

function applyCapabilities(caps) {
  capabilities = caps || {};
  ui.pause.hidden = !capabilities.pause;
  ui.stop.hidden = !capabilities.stop;
  ui.seekRow.hidden = !capabilities.step;

  ui.capabilities.replaceChildren();
  for (const [key, label] of Object.entries(CAPABILITY_LABELS)) {
    const on = Boolean(capabilities[key]);
    const li = document.createElement("li");
    li.dataset.on = String(on);
    const name = document.createElement("span");
    name.textContent = label;
    const value = document.createElement("span");
    value.textContent = on ? "tak" : "nie";
    li.append(name, value);
    ui.capabilities.append(li);
  }
}

// `seed`: pierwsza ramka po (ponownym) polaczeniu. Ustawia punkt odniesienia, ale
// nie moze wywolac pytania "nastepny odcinek?" o odcinek, ktory skonczyl sie,
// gdy telefon spal.
let backendErrorShown = false;

function render(data, { seed = false } = {}) {
  if (!data) return;
  if (data.capabilities) applyCapabilities(data.capabilities);

  const state = data.state || "unknown";
  const current = data.current;

  // Przejscie z grania w koniec = odcinek sie skonczyl albo odtwarzacz zamknieto.
  // Backend zglasza to sam, z watku dozorujacego — nie trzeba nic klikac.
  const previousState = seed ? null : lastState;
  lastState = state;
  if (ACTIVE_STATES.has(previousState) && FINISHED_STATES.has(state)) {
    if (userStopped) {
      userStopped = false;
    } else {
      showEndedPrompt(state === "ended");
    }
  }

  ui.stateDot.dataset.state = state;
  ui.nowbar.hidden = !current && state !== "playing" && state !== "paused";

  if (current) {
    ui.nowTitle.textContent = current.title || current.query || "—";
    const bits = [`odc. ${current.episode}`, STATE_LABELS[state] || state];
    if (capabilities.position && typeof data.position === "number") {
      const suffix = data.duration ? ` / ${formatTime(data.duration)}` : "";
      bits.push(formatTime(data.position) + suffix);
    }
    ui.nowMeta.textContent = bits.join(" · ");
  } else {
    ui.nowTitle.textContent = STATE_LABELS[state] || state;
    ui.nowMeta.textContent = data.player ? `odtwarzacz: ${data.player}` : "";
  }

  // Etykieta zalezy od stanu: "Pauza" gdy gra, "Wznów" gdy wstrzymane.
  const pauseLabel = state === "paused" ? "Wznów" : "Pauza";
  ui.pause.dataset.label = pauseLabel;
  if (!ui.pause.disabled) ui.pause.textContent = pauseLabel;
  ui.pause.setAttribute("aria-pressed", String(state === "paused"));

  // Wireless debugging potrafi zgasnac sam; bez niego pauza i stan nie dzialaja.
  const noAdb = data.adb === false && data.player === "android-intent";
  ui.adbHint.textContent = data.warning
    ? data.warning
    : "Pauza i stan niedostępne — włącz Wireless debugging na urządzeniu.";
  ui.adbHint.hidden = !(noAdb || data.warning);

  // Blad z backendu pokazujemy i tez gasimy, gdy backend go zwolnil — inaczej
  // stary blad wisialby do przeladowania strony.
  if (data.last_error) {
    showError(data.last_error);
    backendErrorShown = true;
  } else if (backendErrorShown) {
    showError("");
    backendErrorShown = false;
  }
}

ui.pause.addEventListener("click", () =>
  withBusy(ui.pause, async () => render(await post("/api/pause")))
);

ui.stop.addEventListener("click", () =>
  withBusy(ui.stop, async () => {
    userStopped = true;
    hideEndedPrompt();
    try {
      render(await post("/api/stop"));
    } catch (err) {
      // Stop sie nie udal — flaga nie moze zjesc nastepnego prawdziwego konca.
      userStopped = false;
      throw err;
    }
    loadContinue();
  })
);

// --- ustawienia -----------------------------------------------------------

const LOCAL_KEY = "ani-web-settings";

function applySettings(settings) {
  if (settings.quality) {
    defaultQuality = settings.quality;
    ui.setQuality.value = settings.quality;
    ui.detailQuality.value = settings.quality;
  }
  if (settings.autoplay_next !== undefined) {
    autoplayNext = Boolean(settings.autoplay_next);
    ui.setAutoplay.checked = autoplayNext;
  }
  if (settings.skip_intro !== undefined) {
    skipIntro = Boolean(settings.skip_intro);
    ui.setSkip.checked = skipIntro;
  }
}

async function loadSettings() {
  // localStorage od razu, backend nadpisuje gdy odpowie — UI nie czeka na siec.
  try {
    applySettings(JSON.parse(localStorage.getItem(LOCAL_KEY)) || {});
  } catch {
    /* uszkodzony wpis nie moze zablokowac startu */
  }
  try {
    applySettings(await api("/api/settings"));
  } catch {
    /* ustawienia to nie funkcja krytyczna */
  }
}

ui.settingsSave.addEventListener("click", () =>
  withBusy(ui.settingsSave, async () => {
    const settings = {
      quality: ui.setQuality.value,
      autoplay_next: ui.setAutoplay.checked,
      skip_intro: ui.setSkip.checked,
    };
    try {
      localStorage.setItem(LOCAL_KEY, JSON.stringify(settings));
    } catch {
      // Tryb prywatny / pelna kwota — zapis na serwerze i tak ma sie wykonac.
    }
    applySettings(settings);
    ui.settingsHint.textContent = "Zapisano lokalnie…";
    await post("/api/settings", settings);
    ui.settingsHint.textContent = "Zapisano.";
  })
);

// --- strumien zdarzen -----------------------------------------------------

// Dokladnie jedno polaczenie naraz. Serwer trzyma watek na kazdego klienta SSE,
// a urzadzenie ma 1 GB RAM — uspiony telefon albo ukryta karta nie moga
// zostawiac po sobie zywych polaczen (docs.md D4).
let events = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
const RECONNECT_MAX_MS = 30000;

function setConnection(ok) {
  ui.conn.hidden = ok;
  if (!ok) ui.stateDot.dataset.state = "offline";
}

function closeEvents() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (events) {
    events.close();
    events = null;
  }
}

function connectEvents() {
  closeEvents();
  // Pierwsza ramka na nowym polaczeniu tylko ustawia punkt odniesienia.
  let first = true;
  const source = new EventSource("/api/events");
  events = source;

  source.addEventListener("open", () => {
    reconnectDelay = 1000;
    setConnection(true);
  });

  source.addEventListener("message", (event) => {
    try {
      render(JSON.parse(event.data), { seed: first });
      first = false;
    } catch {
      // Uszkodzona klatka nie moze zabic strumienia.
    }
  });

  source.addEventListener("error", () => {
    setConnection(false);
    // Przy trwalym bledzie (CLOSED) przegladarka juz nie probuje — robimy to sami,
    // z rosnacym odstepem. Przy CONNECTING wznawia sama.
    if (source.readyState === EventSource.CLOSED && events === source) {
      source.close();
      events = null;
      reconnectTimer = setTimeout(connectEvents, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    closeEvents();
  } else {
    connectEvents();
  }
});
// Karta odlozona do pamieci podrecznej (bfcache) nie wywola visibilitychange.
window.addEventListener("pagehide", closeEvents);
window.addEventListener("pageshow", (event) => {
  if (event.persisted) connectEvents();
});

// --- start ----------------------------------------------------------------

(async function start() {
  show("home");
  // Status najpierw: ta odpowiedz ustala ciasteczko z uid, zanim ruszy reszta.
  // Dalej rownolegle — wolne ustawienia nie opozniaja historii ani strumienia.
  try {
    render(await api("/api/status"), { seed: true });
  } catch (err) {
    showError(`Backend nie odpowiada: ${err.message}`);
  }
  await Promise.allSettled([loadSettings(), loadContinue()]);
  connectEvents();
})();
