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
from db import init_schema
from history import HistoryStore
from mapping import LazyMapper
from metadata import MetadataError, MetadataService
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


def create_app(config=None, player=None, source=None, settings=None, metadata=None, history=None):
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

    if metadata is None or history is None:
        db_path = data_path(config, "aniweb.sqlite3")
        init_schema(db_path)
        metadata = metadata if metadata is not None else MetadataService(db_path)
        history = history if history is not None else HistoryStore(db_path)

    mapper = LazyMapper(source, metadata)
    state = AppState(player, source, mapper=mapper)

    app = Flask(__name__, static_folder=None)
    app.config["ANI_WEB"] = {
        "config": config,
        "state": state,
        "settings": settings,
        "source": source,
        "metadata": metadata,
        "history": history,
        "mapper": mapper,
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
        anilist_id = data.get("anilist_id")

        if not query and anilist_id is None:
            return _json({"error": "Podaj 'query' albo 'anilist_id'", "kind": "request"}, 400)

        uid = _current_uid()
        episode = int(data.get("episode", 1))
        snapshot = state.play(
            query=query or None,
            anilist_id=None if anilist_id is None else int(anilist_id),
            episode=episode,
            quality=data.get("quality") or config["quality"],
            position=int(data.get("position", 0)),
        )

        current = snapshot.get("current") or {}
        if current.get("anilist_id"):
            previous = history.entry(uid, current["anilist_id"])
            history.record(
                uid,
                current["anilist_id"],
                episode,
                title=current.get("title"),
                cover=current.get("cover"),
            )
            # Zmiana odcinka zeruje pozycje — inaczej wznawianie skakaloby w nowym
            # odcinku do czasu ze starego.
            if previous and previous["episode"] != episode:
                history.clear_position(uid, current["anilist_id"])

        return _json(snapshot, uid=uid)

    @app.post("/api/pause")
    def api_pause():
        return _json(state.pause())

    @app.post("/api/stop")
    def api_stop():
        uid = _current_uid()
        # Pozycje trzeba odczytac PRZED stop() — potem odtwarzacz jej nie zna.
        before = state.as_dict()
        current = before.get("current") or {}
        snapshot = state.stop()

        if current.get("anilist_id") and before.get("position") is not None:
            history.record(
                uid,
                current["anilist_id"],
                current.get("episode", 1),
                position=before.get("position"),
                duration=before.get("duration"),
                title=current.get("title"),
                cover=current.get("cover"),
            )

        return _json(snapshot, uid=uid)

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
    def api_anime_search():
        query = (request.args.get("q") or "").strip()
        if not query:
            return _json({"error": "Brak parametru 'q'", "kind": "request"}, 400)
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
        results = metadata.search(query, limit=limit)
        return _json({"query": query, "results": [m.as_dict() for m in results]})

    @app.get("/api/anime/<int:anilist_id>")
    def api_anime_details(anilist_id):
        uid = _current_uid()
        meta = metadata.details(anilist_id)
        payload = meta.as_dict()
        # Szczegoly od razu mowia, od czego kontynuowac — jedno zapytanie mniej z UI.
        payload["resume"] = history.resume(uid, anilist_id, total_episodes=meta.episodes)
        return _json(payload, uid=uid)

    @app.get("/api/history")
    def api_history():
        uid = _current_uid()
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
        return _json({"entries": history.recent(uid, limit=limit)}, uid=uid)

    @app.get("/api/history/<int:anilist_id>")
    def api_history_entry(anilist_id):
        uid = _current_uid()
        entry = history.entry(uid, anilist_id)
        return _json(
            {"entry": entry, "resume": history.resume(uid, anilist_id)},
            uid=uid,
        )

    @app.delete("/api/history/<int:anilist_id>")
    def api_history_delete(anilist_id):
        uid = _current_uid()
        removed = history.delete(uid, anilist_id)
        return _json({"deleted": removed}, 200 if removed else 404, uid=uid)

    # --- bledy ------------------------------------------------------------

    # Warstwy nizsze rzucaja typami domenowymi; mapowanie na HTTP siedzi tutaj,
    # zeby nie rozsmarowywac kodow statusu po calej aplikacji.

    @app.errorhandler(SourceError)
    def handle_source_error(exc):
        return _json({"error": str(exc), "kind": "source"}, 502)

    @app.errorhandler(MetadataError)
    def handle_metadata_error(exc):
        # Katalog padl, ale odtwarzanie po tytule wprost nadal dziala — dlatego
        # osobny "kind", zeby UI mogl to powiedziec zamiast udawac awarie calosci.
        return _json({"error": str(exc), "kind": "catalogue"}, 502)

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
