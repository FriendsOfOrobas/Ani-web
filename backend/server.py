import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


class AppState:
    def __init__(self, ani_cli_command="ani-cli"):
        self.status = "idle"
        self.player = "vlc"
        self.current_source = None
        self.ani_cli_command = ani_cli_command
        self.process = None
        self.pid = None

    def as_dict(self):
        return {
            "status": self.status,
            "player": self.player,
            "current_source": self.current_source,
            "pid": self.pid,
        }

    def start_playback(self, source):
        self.current_source = source

        if self.process is not None and self.process.poll() is None:
            return self.as_dict()

        if shutil_which("vlc"):
            self.player = "vlc"
            self.status = "started"
            self.pid = None
            return self.as_dict()

        if shutil_which(self.ani_cli_command) or os.path.exists(self.ani_cli_command):
            self.player = "ani-cli"
            self.process = subprocess.Popen(
                [self.ani_cli_command, source],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.pid = self.process.pid
            self.status = "playing"
            return self.as_dict()

        self.player = "vlc"
        self.status = "simulated"
        self.pid = None
        return self.as_dict()

    def stop_playback(self):
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=2)

        self.process = None
        self.pid = None
        self.status = "stopped"
        self.current_source = None
        return self.as_dict()


def shutil_which(name):
    return any(
        os.access(os.path.join(path, name), os.X_OK)
        for path in os.environ.get("PATH", "").split(":")
        if path
    )


class AniHandler(BaseHTTPRequestHandler):
    state = AppState()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._send_json(self.state.as_dict())
            return
        if parsed.path == "/":
            with open("frontend/index.html", "r", encoding="utf-8") as fh:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(fh.read().encode("utf-8"))
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/play":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body or "{}")
            result = self.state.start_playback(data.get("source", "demo.mp4"))
            self._send_json(result)
            return
        if parsed.path == "/api/stop":
            self._send_json(self.state.stop_playback())
            return
        self._send_json({"error": "not found"}, 404)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host="127.0.0.1", port=8000):
    server = HTTPServer((host, port), AniHandler)
    print(f"Serving on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
