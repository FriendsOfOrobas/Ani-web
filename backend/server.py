"""Aplikacja Flask — serwuje frontend i wystawia API z docs.md §6.

Ten modul jest CIENKI. Cala logika siedzi w state/players/ani_runner; tutaj
zostaje wylacznie HTTP: routing, kodowanie odpowiedzi i strumien SSE.

Stara klasa AppState oparta na http.server zostala usunieta — jedyny stan
aplikacji mieszka teraz w state.AppState.
"""

import argparse
import json
import queue
import uuid

from flask import Flask, Response, jsonify, request, send_from_directory

from ani_runner import AniCliSource, SourceError
from config import ROOT_DIR, data_path, load_config
from players import BACKENDS, PlayerError, select_backend
from state import AppState, SettingsStore

FRONTEND_DIR = ROOT_DIR / "frontend"

UID_COOKIE = "aniweb_uid"
UID_MAX_AGE = 60 * 60 * 24 * 365

# Bez tego posrednicy i przegladarki potrafia uznac cicha sesje SSE za zerwana.
SSE_HEARTBEAT_SECONDS = 15


# --- pomocnicze -----------------------------------------------------------


def _sse_frame(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_stream(state):
    q = state.subscribe()
    try:
        # Pierwsza klatka od razu, zeby UI nie czekal na pierwsza zmiane stanu.
        yield _sse_frame(state.as_dict())
        while True:
            try:
                yield _sse_frame(q.get(timeout=SSE_HEARTBEAT_SECONDS))
            except queue.Empty:
                yield ": keepalive\n\n"
    finally:
        # Klient rozlaczony — inaczej publish() karmilby martwa kolejke.
        state.unsubscribe(q)


def _current_uid():
    return request.cookies.get(UID_COOKIE) or uuid.uuid4().hex


def _json(payload, status=200, uid=None):
    response = jsonify(payload)
    response.status_code = status
    if uid is not None and request.cookies.get(UID_COOKIE) != uid:
        response.set_cookie(UID_COOKIE, uid, max_age=UID_MAX_AGE, samesite="Lax")
    return response


def _request_json():
    return request.get_json(silent=True) or {}


# --- fabryka --------------------------------------------------------------


def create_app(config=None, player=None, source=None, settings=None):
    """Fabryka aplikacji. Wszystkie zaleznosci wstrzykiwalne — pod testy."""
    config = config if config is not None else load_config()

    if player is None:
        player = select_backend(config)
    if source is None:
        source = AniCliSource(
            ani_cli_command=config["ani_cli_command"],
            quality=config["quality"],
            default_referer=config["allanime_referer"],
        )
    if settings is None:
        settings = SettingsStore(data_path(config, "settings", "settings.json"))

    state = AppState(player, source)

    app = Flask(__name__, static_folder=None)
    app.config["ANI_WEB"] = {
        "config": config,
        "state": state,
        "settings": settings,
        "source": source,
    }

    # --- frontend ---------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.get("/<path:filename>")
    def frontend_asset(filename):
        # send_from_directory pilnuje wyjscia poza katalog.
        return send_from_directory(FRONTEND_DIR, filename)

    @app.route(
        "/api/<path:endpoint>",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def api_not_found(endpoint):
        # Bez tego nieznane /api/* wpada w catch-all frontendu i wraca jako 405
        # (bo tamta trasa jest GET-only) albo jako HTML. API ma odpowiadac JSON-em.
        return _json({"error": f"Nieznany endpoint /api/{endpoint}", "kind": "request"}, 404)

    # --- API --------------------------------------------------------------

    @app.get("/api/status")
    def api_status():
        payload = state.as_dict()
        payload["source"] = {"name": source.name, "missing": source.check()}
        return _json(payload)

    @app.get("/api/events")
    def api_events():
        response = Response(_event_stream(state), mimetype="text/event-stream")
        response.headers["Cache-Control"] = "no-cache"
        # Wylacza buforowanie po stronie ewentualnego reverse-proxy.
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.post("/api/play")
    def api_play():
        data = _request_json()
        query = (data.get("query") or "").strip()
        if not query:
            return _json({"error": "Brak pola 'query'", "kind": "request"}, 400)

        return _json(
            state.play(
                query,
                episode=int(data.get("episode", 1)),
                quality=data.get("quality") or config["quality"],
                position=int(data.get("position", 0)),
            )
        )

    @app.post("/api/pause")
    def api_pause():
        return _json(state.pause())

    @app.post("/api/stop")
    def api_stop():
        return _json(state.stop())

    @app.get("/api/settings")
    def api_settings_get():
        uid = _current_uid()
        return _json(settings.get(uid), uid=uid)

    @app.post("/api/settings")
    def api_settings_post():
        data = _request_json()
        if not isinstance(data, dict):
            return _json({"error": "Oczekiwano obiektu JSON", "kind": "request"}, 400)
        uid = _current_uid()
        return _json(settings.update(uid, data), uid=uid)

    @app.get("/api/anime")
    def api_anime():
        return _json(
            {"error": "Katalog nie jest jeszcze zaimplementowany (Stage 1)", "kind": "todo"},
            501,
        )

    # --- bledy ------------------------------------------------------------

    # Warstwy nizsze rzucaja typami domenowymi; mapowanie na HTTP siedzi tutaj,
    # zeby nie rozsmarowywac kodow statusu po calej aplikacji.

    @app.errorhandler(SourceError)
    def handle_source_error(exc):
        return _json({"error": str(exc), "kind": "source"}, 502)

    @app.errorhandler(PlayerError)
    def handle_player_error(exc):
        return _json({"error": str(exc), "kind": "player"}, 409)

    @app.errorhandler(NotImplementedError)
    def handle_not_implemented(exc):
        return _json({"error": str(exc) or "Nie zaimplementowano", "kind": "todo"}, 501)

    @app.errorhandler(ValueError)
    def handle_value_error(exc):
        return _json({"error": f"Nieprawidlowe dane: {exc}", "kind": "request"}, 400)

    return app


# --- uruchamianie ---------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="ani-web",
        description="Lokalny serwer Ani-Web (backend w Termuksie albo na Linuksie).",
    )
    parser.add_argument("--host", help="Adres nasluchiwania (domyslnie z config)")
    parser.add_argument("--port", type=int, help="Port (domyslnie z config)")
    parser.add_argument(
        "--player",
        choices=["auto", *sorted(BACKENDS)],
        help=(
            "Backend odtwarzania. Na Linuksie do testow: mpv-ipc (pelna kontrola) "
            "albo simulated (nic nie uruchamia)."
        ),
    )
    parser.add_argument("--debug", action="store_true", help="Tryb debug Flaska")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = load_config()

    # CLI ma pierwszenstwo nad config.json/config.local.json/ANI_WEB_*.
    for key in ("host", "port", "player"):
        value = getattr(args, key)
        if value is not None:
            config[key] = value

    app = create_app(config)
    bundle = app.config["ANI_WEB"]

    missing = bundle["source"].check()
    if missing:
        print("Uwaga — brakujace zaleznosci zrodla: " + ", ".join(missing))
        print("Rozwiazywanie adresow nie zadziala, reszta API tak.")

    print(f"Odtwarzacz: {bundle['state'].player.name}")
    print(f"Serwuje na http://{config['host']}:{config['port']}")

    # threaded=True jest wymagane: kazde otwarte SSE trzyma watek na stale.
    app.run(host=config["host"], port=config["port"], threaded=True, debug=args.debug)


if __name__ == "__main__":
    main()
