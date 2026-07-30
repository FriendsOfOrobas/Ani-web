"use strict";

/*
 * Frontend jest celowo glupi: nie zna ani-cli, nie zna Androida, nie zaklada
 * co odtwarzacz potrafi. Wszystko czyta z `capabilities` w /api/status i chowa
 * kontrolki, ktorych aktywny backend nie obsluguje (docs.md §2.1).
 *
 * Suwaka postepu tu nie ma i nie ma go byc — docs.md D2.
 */

const el = (id) => document.getElementById(id);

const ui = {
  error: el("error"),
  navToggle: el("nav-toggle"),
  screenPlay: el("screen-play"),
  screenSettings: el("screen-settings"),

  query: el("query"),
  episode: el("episode"),
  quality: el("quality"),
  play: el("play"),
  pause: el("pause"),
  stop: el("stop"),

  stateDot: el("state-dot"),
  state: el("state"),
  player: el("player"),
  nowPlayingRow: el("now-playing-row"),
  nowPlaying: el("now-playing"),
  positionRow: el("position-row"),
  position: el("position"),
  sourceRow: el("source-row"),
  source: el("source"),

  setQuality: el("set-quality"),
  setEpisodeStep: el("set-episode-step"),
  settingsSave: el("settings-save"),
  settingsHint: el("settings-hint"),
  capabilities: el("capabilities"),
};

const STATE_LABELS = {
  idle: "bezczynny",
  playing: "odtwarzanie",
  paused: "pauza",
  stopped: "zatrzymany",
  ended: "koniec",
  unknown: "nieznany",
  offline: "brak połączenia z backendem",
};

const CAPABILITY_LABELS = {
  play: "odtwarzanie",
  pause: "pauza",
  stop: "zatrzymanie",
  seek: "przeskakiwanie",
  position: "odczyt pozycji",
  ended_events: "wykrycie końca",
};

let capabilities = {};

// --- pomocnicze -----------------------------------------------------------

function showError(message) {
  ui.error.textContent = message;
  ui.error.hidden = !message;
}

function clearError() {
  showError("");
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

function setRow(row, valueEl, value) {
  const has = value !== null && value !== undefined && value !== "";
  row.hidden = !has;
  if (has) valueEl.textContent = value;
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
    throw new Error(data.error || `Błąd ${response.status}`);
  }
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

// --- renderowanie ---------------------------------------------------------

function applyCapabilities(caps) {
  capabilities = caps || {};

  // Jedyna reguła: czego backend nie potrafi, tego nie pokazujemy.
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
  if (data.capabilities) applyCapabilities(data.capabilities);

  const state = data.state || "unknown";
  ui.state.textContent = STATE_LABELS[state] || state;
  ui.stateDot.dataset.state = state;
  ui.player.textContent = data.player || "—";

  const current = data.current;
  setRow(
    ui.nowPlayingRow,
    ui.nowPlaying,
    current ? `${current.query} — odc. ${current.episode}` : null
  );

  // Pozycja tylko gdy backend ja faktycznie raportuje (docs.md D2).
  if (capabilities.position && typeof data.position === "number") {
    const suffix = data.duration ? ` / ${formatTime(data.duration)}` : "";
    setRow(ui.positionRow, ui.position, formatTime(data.position) + suffix);
  } else {
    ui.positionRow.hidden = true;
  }

  if (data.source) {
    const missing = data.source.missing || [];
    setRow(
      ui.sourceRow,
      ui.source,
      missing.length ? `${data.source.name} — brakuje: ${missing.join(", ")}` : data.source.name
    );
  }

  if (data.last_error) {
    showError(data.last_error);
  }
}

// --- akcje ----------------------------------------------------------------

async function withBusy(button, action) {
  button.disabled = true;
  clearError();
  try {
    render(await action());
  } catch (err) {
    showError(err.message);
  } finally {
    button.disabled = false;
  }
}

ui.play.addEventListener("click", () => {
  const query = ui.query.value.trim();
  if (!query) {
    showError("Podaj tytuł anime.");
    ui.query.focus();
    return;
  }
  return withBusy(ui.play, () =>
    post("/api/play", {
      query,
      episode: Number(ui.episode.value) || 1,
      quality: ui.quality.value,
    })
  );
});

ui.pause.addEventListener("click", () => withBusy(ui.pause, () => post("/api/pause")));
ui.stop.addEventListener("click", () => withBusy(ui.stop, () => post("/api/stop")));

// --- ekrany ---------------------------------------------------------------

ui.navToggle.addEventListener("click", () => {
  const showSettings = ui.screenSettings.hidden;
  ui.screenSettings.hidden = !showSettings;
  ui.screenPlay.hidden = showSettings;
  ui.navToggle.textContent = showSettings ? "Wróć" : "Ustawienia";
});

// --- ustawienia -----------------------------------------------------------

const LOCAL_KEY = "ani-web-settings";

function readLocal() {
  try {
    return JSON.parse(localStorage.getItem(LOCAL_KEY)) || {};
  } catch {
    return {};
  }
}

function applySettings(settings) {
  if (settings.quality) {
    ui.quality.value = settings.quality;
    ui.setQuality.value = settings.quality;
  }
  if (settings.episode) {
    ui.episode.value = settings.episode;
    ui.setEpisodeStep.value = settings.episode;
  }
}

async function loadSettings() {
  // localStorage od razu, backend nadpisuje gdy odpowie — UI nie czeka na siec.
  applySettings(readLocal());
  try {
    applySettings(await api("/api/settings"));
  } catch {
    // Ustawienia to nie funkcja krytyczna; brak backendu nie blokuje UI.
  }
}

ui.settingsSave.addEventListener("click", async () => {
  const settings = {
    quality: ui.setQuality.value,
    episode: Number(ui.setEpisodeStep.value) || 1,
  };
  localStorage.setItem(LOCAL_KEY, JSON.stringify(settings));
  applySettings(settings);
  ui.settingsHint.textContent = "Zapisano lokalnie…";
  try {
    await post("/api/settings", settings);
    ui.settingsHint.textContent = "Zapisano.";
  } catch (err) {
    ui.settingsHint.textContent = `Zapisano lokalnie, backend odmówił: ${err.message}`;
  }
});

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
    ui.state.textContent = STATE_LABELS.offline;
  });
}

// --- start ----------------------------------------------------------------

(async function start() {
  try {
    render(await api("/api/status"));
  } catch (err) {
    showError(`Backend nie odpowiada: ${err.message}`);
  }
  await loadSettings();
  connectEvents();
})();
