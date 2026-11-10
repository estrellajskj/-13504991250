"""Loopback-only UI/API for the PC controller. No hardware opens until Connect."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import argparse
import json
import mimetypes
import secrets
import socket
import sys
import webbrowser

from pc_control import ControlError, Controller

ROOT = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))
DATA_ROOT = Path(sys.executable).resolve().parent if FROZEN else ROOT
ASSETS = {"/pc-control.html", "/pc-control.js", "/pc-control.css"}


class ControllerServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, data, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("Browser disconnected while sending response")

    def _host_allowed(self):
        return self.headers.get("Host", "").lower() in (
            "127.0.0.1:" + str(self.server.server_port),
            "localhost:" + str(self.server.server_port),
        )

    def do_GET(self):
        if not self._host_allowed():
            self._send(403, {"error": "HOST"})
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send(302, b"", headers={"Location": "/pc-control.html"})
        elif path == "/api/info":
            self._send(200, dict(application="pump-pc", version=4,
                                 simulation=self.server.controller.simulation))
        elif path == "/api/state":
            try:
                since = max(0, int(parse_qs(parsed.query).get("since", ["0"])[0]))
            except ValueError:
                self._send(400, {"error": "SINCE"})
                return
            data = self.server.controller.snapshot(since)
            data["token"] = self.server.control_token
            self._send(200, data)
        elif path == "/api/ports":
            if self.server.controller.simulation:
                ports = [dict(device="SIM-PUMP", description="Simulated pump"),
                         dict(device="SIM-PUMP2", description="Simulated second pump"),
                         dict(device="SIM-BZY", description="Simulated BZY")]
            else:
                from serial.tools import list_ports
                ports = [dict(device=p.device, description=p.description) for p in list_ports.comports()]
            self._send(200, {"ports": ports})
        elif path == "/api/log.csv":
            recorder = self.server.controller.recorder
            if not recorder:
                self._send(404, {"error": "NO_RECORD"})
                return
            self._send(200, recorder.download(), "text/csv; charset=utf-8",
                       {"Content-Disposition": 'attachment; filename="' + recorder.path.name + '"'})
        elif path in ASSETS:
            try:
                data = (ROOT / path.lstrip("/")).read_bytes()
            except OSError:
                self._send(404, {"error": "MISSING_ASSET"})
                return
            self._send(200, data, (mimetypes.guess_type(path)[0] or "text/plain") + "; charset=utf-8")
        else:
            self._send(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        if (not self._host_allowed()
                or (origin is not None and origin != "http://" + self.headers.get("Host", ""))
                or not secrets.compare_digest(self.headers.get("X-Control-Token", ""), self.server.control_token)):
            self._send(403, {"error": "LOCAL_SESSION_REQUIRED"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 20000 or self.headers.get_content_type() != "application/json":
                raise ControlError("JSON_REQUIRED")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ControlError("JSON_OBJECT_REQUIRED")
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path == "/api/command":
                action = payload.get("action")
                if not isinstance(action, str):
                    raise ControlError("COMMAND_REQUIRED")
                result = controller.command(action, payload)
            elif path == "/api/sim" and controller.simulation:
                controller.plant.update(payload)
                result = "SIM_UPDATED"
            else:
                self._send(404, {"error": "NOT_FOUND"})
                return
            self._send(200, {"result": result})
        except ControlError as exc:
            self._send(409, {"error": str(exc)})
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            self._send(400, {"error": "INVALID_REQUEST", "detail": str(exc)[:200]})
        except Exception as exc:
            self.log_error("Request failed: %s", exc)
            self._send(500, {"error": "OPERATION_FAILED", "detail": str(exc)[:300]})

    def log_message(self, fmt, *args):
        # Polling should not fill the console or a redirected log.
        if args and isinstance(args[0], str) and args[0].startswith("GET /api/state"):
            return
        super().log_message(fmt, *args)


def create_server(port, folder=None, simulation=False):
    server = ControllerServer(("127.0.0.1", port), Handler)
    try:
        server.controller = Controller(folder or DATA_ROOT / "output" / ("pc-control-sim" if simulation else "pc-control"),
                                       simulation=simulation)
        server.control_token = secrets.token_hex(24)
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8778)
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--open", dest="open", action="store_true")
    browser.add_argument("--no-open", dest="open", action="store_false")
    parser.set_defaults(open=FROZEN)
    parser.add_argument("--simulate", action="store_true", help="Explicit simulation; never opens hardware")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65526:
        parser.error("--port must be between 1024 and 65526")
    try:
        import minimalmodbus  # noqa: F401
        import serial  # noqa: F401
    except ImportError:
        raise SystemExit("Install dependencies: python -m pip install -r requirements-pc.txt")
    server = None
    for port in range(args.port, args.port + (10 if args.open else 1)):
        url = f"http://127.0.0.1:{port}/pc-control.html"
        try:
            server = create_server(port, simulation=args.simulate)
            break
        except OSError as exc:
            if not args.open:
                raise SystemExit(f"Port {port} unavailable: {exc}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1) as response:
                    info = json.load(response)
                    if info.get("application") == "pump-pc" and info.get("version") == 4 and info.get("simulation") == args.simulate:
                        print(f"Controller already running: {url}", flush=True)
                        webbrowser.open(url)
                        return
            except (OSError, ValueError):
                continue
    if server is None:
        raise SystemExit("No free local port. Close conflicting services and retry.")
    print(f"PC pump controller: {url}", flush=True)
    print("SIMULATION ONLY - no physical hardware" if args.simulate else "REAL DEVICE MODE - connect pump and voltage acquisition independently", flush=True)
    print("Keep this service running. Closing the browser does not stop control.", flush=True)
    print("Stop charging/discharging, stop the pump, then close this service.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Service exiting. A physical pump may retain its last command.", file=sys.stderr)
    finally:
        server.server_close()
        server.controller.close()


if __name__ == "__main__":
    main()
