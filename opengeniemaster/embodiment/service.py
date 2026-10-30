"""Robot-side EEP execution service (reference implementation).

A real robot exposes its execution services (policy inference, robot
control, perception/detectors) through a small HTTP/JSON surface that
:class:`~opengeniemaster.embodiment.adapters.HttpAdapter` calls::

    POST /eep/v0/execute  {"request": ExecutionRequest, "mode": "language"} -> {"feedback": Feedback}
    POST /eep/v0/observe  {"camera": "head", "location": null}            -> {"observation": Observation}
    POST /eep/v0/check    {"criterion": "on_slot", "args": {...}, "close_range": false} -> {"result": CriterionResult}
    GET  /eep/v0/state                                                   -> {"state": {...}}
    GET  /eep/v0/health                                                  -> {"ok": true, "protocol": "open-eep/0.1"}
    POST /eep/v0/stop | /eep/v0/resume                                   -> {"ok": true}

Integrators implement :class:`RobotBackend` (wrapping their VLA server, WBC
controller and detectors) and run :func:`serve`. Request handling lives in
:meth:`RobotService.handle`, independent of the socket server.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .._serde import SerdeError
from ..eep.types import (
    PROTOCOL_VERSION,
    CriterionResult,
    ExecutionMode,
    ExecutionRequest,
    Feedback,
    Observation,
)

log = logging.getLogger(__name__)


class RobotBackend:
    """What a robot must provide. Override every method."""

    robot_id: str = "robot"

    def execute(self, req: ExecutionRequest, mode: ExecutionMode) -> Feedback:  # pragma: no cover - interface
        raise NotImplementedError

    def observe(self, camera: str = "head", location: str | None = None) -> Observation:  # pragma: no cover
        raise NotImplementedError

    def check(self, criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:  # pragma: no cover
        raise NotImplementedError

    def state(self) -> dict[str, Any]:
        return {}

    def stop(self) -> None:
        pass

    def resume(self) -> None:
        pass


class SimBackend(RobotBackend):
    """Exposes one robot of a :class:`~opengeniemaster.sim.world.SimWorld` as a service."""

    def __init__(self, world, robot_id: str) -> None:
        self.world = world
        self.robot_id = robot_id
        self._lock = threading.Lock()

    def execute(self, req: ExecutionRequest, mode: ExecutionMode) -> Feedback:
        with self._lock:
            return self.world.execute(self.robot_id, req, mode)

    def observe(self, camera: str = "head", location: str | None = None) -> Observation:
        with self._lock:
            return self.world.observe(self.robot_id, camera=camera, location=location)

    def check(self, criterion: str, args: dict[str, Any], close_range: bool = False) -> CriterionResult:
        with self._lock:
            result, _ = self.world.check(self.robot_id, criterion, args, close_range=close_range)
            return result

    def state(self) -> dict[str, Any]:
        r = self.world.robot(self.robot_id)
        return {"location": r.location, "holding": r.holding is not None, "clock_s": round(r.clock, 2), "stopped": r.stopped}

    def stop(self) -> None:
        self.world.robot(self.robot_id).stopped = True

    def resume(self) -> None:
        self.world.robot(self.robot_id).stopped = False


class RobotService:
    def __init__(self, backend: RobotBackend, *, token: str | None = None) -> None:
        self.backend = backend
        self.token = token

    def handle(self, method: str, path: str, body: Any, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        if self.token:
            lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
            if lowered.get("authorization", "") != f"Bearer {self.token}":
                return 401, {"error": {"code": "unauthorized", "message": "missing or invalid bearer token"}}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return 400, {"error": {"code": "invalid_request", "message": "request body must be a JSON object"}}
        try:
            if method == "GET" and path == "/eep/v0/health":
                return 200, {"ok": True, "protocol": PROTOCOL_VERSION, "robot_id": self.backend.robot_id}
            if method == "GET" and path == "/eep/v0/state":
                return 200, {"state": self.backend.state()}
            if method == "POST" and path == "/eep/v0/execute":
                req = ExecutionRequest.from_dict(body["request"])
                if req.robot_id != self.backend.robot_id:
                    return 400, {"error": {"code": "wrong_robot", "message": f"this service controls {self.backend.robot_id}, not {req.robot_id}"}}
                mode = ExecutionMode(body.get("mode") or (req.mode.value if req.mode else "language"))
                return 200, {"feedback": self.backend.execute(req, mode).to_dict()}
            if method == "POST" and path == "/eep/v0/observe":
                obs = self.backend.observe(camera=body.get("camera") or "head", location=body.get("location"))
                return 200, {"observation": obs.to_dict()}
            if method == "POST" and path == "/eep/v0/check":
                res = self.backend.check(body["criterion"], dict(body.get("args") or {}), bool(body.get("close_range")))
                return 200, {"result": res.to_dict()}
            if method == "POST" and path == "/eep/v0/stop":
                self.backend.stop()
                return 200, {"ok": True}
            if method == "POST" and path == "/eep/v0/resume":
                self.backend.resume()
                return 200, {"ok": True}
        except (KeyError, ValueError, SerdeError) as exc:
            return 400, {"error": {"code": "invalid_request", "message": str(exc)}}
        except Exception as exc:  # backend failure: report, keep serving
            log.exception("backend error")
            return 500, {"error": {"code": "backend_error", "message": str(exc)}}
        return 404, {"error": {"code": "not_found", "message": f"{method} {path}"}}

    def as_transport(self):
        """In-process transport for :class:`HttpAdapter` (no sockets needed)."""

        def transport(method: str, url: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
            from urllib.parse import urlparse

            from .adapters import AdapterError

            status, payload = self.handle(method, urlparse(url).path, body, {"Authorization": f"Bearer {self.token}"} if self.token else None)
            if status >= 400:
                raise AdapterError(f"HTTP {status}: {payload}", status=status)
            return payload

        return transport


def make_server(service: RobotService, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = None
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    self._reply(400, {"error": {"code": "invalid_json", "message": "body is not JSON"}})
                    return
            status, payload = service.handle(method, self.path.split("?", 1)[0], body, dict(self.headers.items()))
            self._reply(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

    return ThreadingHTTPServer((host, port), Handler)


def serve(service: RobotService, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(service, host, port)
    log.info("EEP robot service for %s on http://%s:%d", service.backend.robot_id, host, server.server_address[1])
    try:
        server.serve_forever()
    finally:
        server.server_close()
