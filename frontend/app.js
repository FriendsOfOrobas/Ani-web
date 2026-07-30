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

  setAutoplay: el("set-autoplay"),

  nowbar: el("nowbar"),
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

async function api(path, options) {
  const response = await fetch(path, options);
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

const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

async function withBusy(button, action) {
  const label = button.textContent;
  button.disabled = true;
  showError("");
  try {
    return await action();
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
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

ui.navBack.addEventListener("click", () => {
  screenStack.pop();
  show(screenStack[screenStack.length - 1] || "home");
});

ui.navSettings.addEventListener("click", () => goTo("settings"));

// --- karty anime ----------------------------------------------------------

function animeCard(item, extra) {
  const li = document.createElement("li");
  li.className = "card-item";

  const button = document.createElement("button");
  button.className = "card-button";
  button.type = "button";
  button.addEventListener("click", () => openDetail(item.anilist_id));

  if (item.cover) {
    const img = document.createElement("img");
    img.src = item.cover;
    img.alt = "";
    img.loading = "lazy";
    button.append(img);
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = "cover-placeholder";
    placeholder.textContent = "?";
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

ui.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = ui.search.value.trim();
  if (!query) return;

  return withBusy(ui.searchForm.querySelector("button"), async () => {
    ui.homeHint.textContent = "Szukam…";
    const data = await api(`/api/anime?q=${encodeURIComponent(query)}`);
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
  ui.detailEpisode.value = resume.episode;

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

async function openDetail(anilistId) {
  goTo("detail");
  currentDetail = null;
  ui.detailTitle.textContent = "Ładowanie…";
  ui.detailFacts.textContent = "";
  ui.detailDescription.textContent = "";
  ui.detailCover.hidden = true;
  ui.detailForget.hidden = true;
  ui.detailResume.textContent = "";

  try {
    const meta = await api(`/api/anime/${anilistId}`);
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
    ui.detailEpisode.max = meta.episodes || "";

    const history = await api(`/api/history/${anilistId}`);
    ui.detailForget.hidden = !history.entry;
    renderResume(meta.resume, history.entry);
  } catch (err) {
    showError(err.message);
    ui.detailTitle.textContent = "Nie udało się wczytać";
  }
}

async function playEpisode({ anilistId, episode, quality, position = 0, title, totalEpisodes }) {
  const snapshot = await post("/api/play", {
    anilist_id: anilistId,
    episode,
    quality: quality || defaultQuality,
    position,
  });
  lastPlayed = {
    anilistId,
    episode,
    quality: quality || defaultQuality,
    title: title || snapshot.current?.title || "",
    totalEpisodes: totalEpisodes || snapshot.current?.total_episodes || 0,
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
      episode: Number(ui.detailEpisode.value) || 1,
      quality: ui.detailQuality.value,
      position: Number(currentDetail.resume?.position || 0),
      title: currentDetail.title,
      totalEpisodes: currentDetail.episodes,
    });
  })
);

ui.detailForget.addEventListener("click", () =>
  withBusy(ui.detailForget, async () => {
    await api(`/api/history/${currentDetail.anilist_id}`, { method: "DELETE" });
    ui.detailForget.hidden = true;
    ui.detailResume.textContent = "";
    ui.detailEpisode.value = 1;
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
    render(
      await post("/api/play", {
        query,
        episode: Number(ui.directEpisode.value) || 1,
        quality: defaultQuality,
      })
    );
  })
);

// --- pytanie po odcinku (odpowiednik menu ani-cli) ------------------------

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
  ui.endedPrompt.hidden = false;

  // Autoplay jest opcja, nie domyslna: przy zamknieciu odtwarzacza z pilota
  // automatyczne wlaczanie kolejnego odcinka byloby wbrew intencji.
  if (autoplayNext && finished && !last) {
    playNext().catch((err) => showError(err.message));
  }
}

function playNext() {
  return playEpisode({
    anilistId: lastPlayed.anilistId,
    episode: lastPlayed.episode + 1,
    quality: lastPlayed.quality,
    title: lastPlayed.title,
    totalEpisodes: lastPlayed.totalEpisodes,
  });
}

ui.endedNext.addEventListener("click", () => withBusy(ui.endedNext, playNext));

ui.endedReplay.addEventListener("click", () =>
  withBusy(ui.endedReplay, () =>
    playEpisode({
      anilistId: lastPlayed.anilistId,
      episode: lastPlayed.episode,
      quality: lastPlayed.quality,
      title: lastPlayed.title,
      totalEpisodes: lastPlayed.totalEpisodes,
    })
  )
);

ui.endedClose.addEventListener("click", hideEndedPrompt);

// --- pasek odtwarzania ----------------------------------------------------

function applyCapabilities(caps) {
  capabilities = caps || {};
  ui.pause.hidden = !capabilities.pause;
  ui.stop.hidden = !capabilities.stop;

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

function render(data) {
  if (!data) return;
  if (data.capabilities) applyCapabilities(data.capabilities);

  const state = data.state || "unknown";
  const current = data.current;

  // Przejscie z grania w koniec = odcinek sie skonczyl albo odtwarzacz zamknieto.
  // Backend zglasza to sam, z watku dozorujacego — nie trzeba nic klikac.
  const previousState = lastState;
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

  if (data.last_error) showError(data.last_error);
}

ui.pause.addEventListener("click", () =>
  withBusy(ui.pause, async () => render(await post("/api/pause")))
);

ui.stop.addEventListener("click", () =>
  withBusy(ui.stop, async () => {
    userStopped = true;
    hideEndedPrompt();
    render(await post("/api/stop"));
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
    };
    localStorage.setItem(LOCAL_KEY, JSON.stringify(settings));
    applySettings(settings);
    ui.settingsHint.textContent = "Zapisano lokalnie…";
    await post("/api/settings", settings);
    ui.settingsHint.textContent = "Zapisano.";
  })
);

// --- strumien zdarzen -----------------------------------------------------

function connectEvents() {
  const events = new EventSource("/api/events");

  events.addEventListener("message", (event) => {
    try {
      render(JSON.parse(event.data));
    } catch {
      // Uszkodzona klatka nie moze zabic strumienia.
    }
  });

  events.addEventListener("error", () => {
    // EventSource sam wznawia; pokazujemy tylko, ze stan jest nieaktualny.
    ui.stateDot.dataset.state = "offline";
  });
}

// --- start ----------------------------------------------------------------

(async function start() {
  show("home");
  try {
    render(await api("/api/status"));
  } catch (err) {
    showError(`Backend nie odpowiada: ${err.message}`);
  }
  await loadSettings();
  await loadContinue();
  connectEvents();
})();
