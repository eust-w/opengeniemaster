"""Real-robot path hardening: retries, unconfirmed stops, service limits, pre-flight doctor."""

import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import create_scenario_task, sim
from opengeniemaster.cli import main
from opengeniemaster.doctor import FAIL, OK, run
from opengeniemaster.eep.types import ExecutionMode, ExecutionRequest, TaskStatus
from opengeniemaster.embodiment.adapters import AdapterError, HttpAdapter, urllib_transport
from opengeniemaster.embodiment.robots import make_robot
from opengeniemaster.embodiment.service import RobotService, SimBackend, make_server
from opengeniemaster.harness.events import EventLog, rotated_paths
from opengeniemaster.sim.scenes import build


class FlakyTransport:
    """Fails the first ``failures`` calls of every path, then delegates to the in-process service."""

    def __init__(self, inner, failures=1, error=None):
        self.inner = inner
        self.failures = failures
        self.error = error or AdapterError("connection reset")
        self.calls = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url))
        if sum(1 for c in self.calls if c == (method, url)) <= self.failures:
            raise self.error
        return self.inner(method, url, body, timeout)


def http_robot(transport, **kwargs):
    adapter = HttpAdapter("http://robot.local", transport=transport, backoff_s=0, **kwargs)
    return adapter, make_robot("wh-01", "wheeled_humanoid", adapter)


class HttpAdapterRetryTest(unittest.TestCase):
    def setUp(self):
        self.sc = build("item_retrieval", 0)
        self.service = RobotService(SimBackend(self.sc.world, "wh-01"))

    def test_idempotent_calls_retry_transient_failures(self):
        flaky = FlakyTransport(self.service.as_transport(), failures=2)
        adapter, robot = http_robot(flaky, retries=2)
        self.assertNotIn("service_unavailable", adapter.observe(robot).flags)
        self.assertTrue(adapter.health(robot)["ok"])
        self.assertTrue(adapter.stop(robot))
        self.assertEqual(sum(1 for m, u in flaky.calls if u.endswith("/observe")), 3)

    def test_execute_is_never_retried(self):
        flaky = FlakyTransport(self.service.as_transport(), failures=1)
        adapter, robot = http_robot(flaky, retries=5)
        fb = adapter.execute(robot, ExecutionRequest(robot_id="wh-01", skill="navigate_to", args={"location": "shelf_A"}),
                             ExecutionMode.PRIMITIVE)
        self.assertEqual(fb.failure_reason.code, "service_unavailable")
        self.assertEqual(sum(1 for m, u in flaky.calls if u.endswith("/execute")), 1)

    def test_client_errors_are_not_retried(self):
        flaky = FlakyTransport(self.service.as_transport(), failures=10, error=AdapterError("HTTP 401: nope", status=401))
        adapter, robot = http_robot(flaky, retries=3)
        health = adapter.health(robot)
        self.assertFalse(health["ok"])
        self.assertEqual(health["http_status"], 401)
        self.assertEqual(len(flaky.calls), 1)

    def test_state_is_cached_briefly(self):
        calls = []
        inner = self.service.as_transport()

        def counting(method, url, body, timeout):
            calls.append(url)
            return inner(method, url, body, timeout)

        clock = [0.0]
        adapter, robot = http_robot(counting, state_ttl_s=1.0, clock=lambda: clock[0])
        for _ in range(5):
            adapter.now(robot)
        self.assertEqual(len(calls), 1)
        clock[0] = 2.0
        adapter.now(robot)
        self.assertEqual(len(calls), 2)
        adapter.execute(robot, ExecutionRequest(robot_id="wh-01", skill="navigate_to", args={"location": "shelf_A"}),
                        ExecutionMode.PRIMITIVE)
        self.assertEqual(adapter.state(robot)["location"], "shelf_A")  # an action invalidates the cache

    def test_malformed_json_is_an_adapter_error(self):
        server = _raw_server(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nContent-Type: application/json\r\n\r\nnot json!")
        if server is None:
            self.skipTest("cannot bind a local port")
        port, thread = server
        with self.assertRaises(AdapterError):
            urllib_transport("GET", f"http://127.0.0.1:{port}/eep/v0/state", None, 5)
        thread.join(5)


class UnconfirmedStopTest(unittest.TestCase):
    def test_stop_that_the_robot_does_not_confirm_is_reported_and_still_blocks(self):
        h, sc = sim()
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).steps[0].assigned_robot
        robot = h.fleet.get(rid)
        robot.adapter.stop = lambda r: False  # the execution service is unreachable
        out = h.stop_robot(rid, "intrusion")
        self.assertFalse(out["confirmed"])
        self.assertIn("physical emergency stop", out["warning"])
        self.assertEqual(h.get_task(task.task_id).status, TaskStatus.PAUSED)
        fb = h.execute(ExecutionRequest(robot_id=rid, skill="navigate_to", args={"location": "storage"}))
        self.assertEqual(fb.failure_reason.code, "admission_rejected")
        stop_events = [e for e in h.events.rows if e["type"] == "stop"]
        self.assertFalse(stop_events[-1]["data"]["confirmed"])

    def test_unconfirmed_resume_keeps_the_robot_stopped(self):
        h, _ = sim()
        rid = next(iter(h.fleet)).robot_id
        h.stop_robot(rid)
        h.fleet.get(rid).adapter.resume = lambda r: False
        out = h.resume_robot(rid)
        self.assertFalse(out["confirmed"])
        self.assertEqual(h.fleet.get(rid).status, "stopped")


class RobotServiceHardeningTest(unittest.TestCase):
    def setUp(self):
        sc = build("item_retrieval", 0)
        self.service = RobotService(SimBackend(sc.world, "wh-01"), token="s3cret")
        try:
            self.server = make_server(self.service, "127.0.0.1", 0, max_body=1024)
        except OSError as exc:
            self.skipTest(f"cannot bind a local port: {exc}")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _raw(self, request: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(request)
            chunks = []
            while True:
                data = s.recv(65536)
                if not data:
                    break
                chunks.append(data)
        return b"".join(chunks)

    def test_oversized_body_is_refused(self):
        resp = self._raw(b"POST /eep/v0/observe HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer s3cret\r\n"
                         b"Content-Length: 999999\r\nConnection: close\r\n\r\n{}")
        self.assertIn(b" 413 ", resp.split(b"\r\n", 1)[0])

    def test_bad_content_length_is_a_client_error(self):
        resp = self._raw(b"POST /eep/v0/observe HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer s3cret\r\n"
                         b"Content-Length: abc\r\nConnection: close\r\n\r\n")
        self.assertIn(b" 400 ", resp.split(b"\r\n", 1)[0])

    def test_unauthenticated_bodies_are_not_parsed(self):
        resp = self._raw(b"POST /eep/v0/observe HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nConnection: close\r\n\r\n{bad!")
        self.assertIn(b" 401 ", resp.split(b"\r\n", 1)[0])

    def test_token_check(self):
        self.assertTrue(self.service.authorized({"authorization": "Bearer s3cret"}))
        self.assertFalse(self.service.authorized({"Authorization": "Bearer s3cre"}))
        self.assertFalse(self.service.authorized({}))


class DoctorTest(unittest.TestCase):
    def test_environment_only(self):
        with tempfile.TemporaryDirectory() as d:
            report = run(state_dir=Path(d) / "state")
            self.assertTrue(report.ok)
            self.assertIn("state dir", [c.name for c in report.checks])

    def test_fleet_with_live_and_dead_robots(self):
        sc = build("store_deployment", 0)
        service = RobotService(SimBackend(sc.world, "wh-01"))
        try:
            server = make_server(service, "127.0.0.1", 0)
        except OSError as exc:
            self.skipTest(f"cannot bind a local port: {exc}")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()  # nothing listens here any more
        try:
            with tempfile.TemporaryDirectory() as d:
                fleet = Path(d) / "fleet.json"
                fleet.write_text(json.dumps({"domain": "store", "robots": [
                    {"robot_id": "wh-01", "model": "wheeled_humanoid", "endpoint": f"http://127.0.0.1:{server.server_address[1]}"},
                    {"robot_id": "wh-02", "model": "wheeled_humanoid", "endpoint": f"http://127.0.0.1:{dead_port}"},
                    {"robot_id": "imposter", "model": "wheeled_humanoid", "endpoint": f"http://127.0.0.1:{server.server_address[1]}"},
                ]}), encoding="utf-8")
                report = run(fleet=fleet, timeout=2)
                by_name = {c.name: c for c in report.checks}
                self.assertEqual(by_name["robot wh-01"].status, OK)
                self.assertEqual(by_name["robot wh-02"].status, FAIL)
                self.assertEqual(by_name["robot imposter"].status, FAIL)
                self.assertIn("controls 'wh-01'", by_name["robot imposter"].detail)
                self.assertFalse(report.ok)
                self.assertFalse(any(Path(d).glob("tasks*")))  # the doctor creates no state

                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(["doctor", "--fleet", str(fleet), "--timeout", "2", "--json"])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(out.getvalue())["ok"])
        finally:
            server.shutdown()
            server.server_close()

    def test_unreadable_fleet_file(self):
        report = run(fleet="/nonexistent/fleet.json")
        self.assertFalse(report.ok)


class EventLogRotationTest(unittest.TestCase):
    def test_rotates_keeps_backups_and_continues_sequence(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            log = EventLog(path, max_bytes=400, backups=2)
            for i in range(60):
                log.emit("tick", float(i), {"i": i, "pad": "x" * 20})
            self.assertEqual([p.name for p in rotated_paths(path)], ["events.jsonl.2", "events.jsonl.1"])
            rows = EventLog.load(path, include_rotated=True)
            seqs = [r["seq"] for r in rows]
            self.assertEqual(seqs, sorted(seqs))
            self.assertEqual(seqs[-1], 60)
            self.assertLess(len(rows), 60)  # the oldest file was dropped
            self.assertLess(len(EventLog.load(path)), len(rows))
            # a new process continues the sequence even right after a rotation
            log.max_bytes = 1
            log._rotate_if_needed()
            self.assertFalse(path.exists())
            self.assertEqual(EventLog(path, max_bytes=400)._seq, 60)

    def test_in_memory_rows_are_bounded(self):
        log = EventLog(max_rows=100)
        for i in range(1000):
            log.emit("tick", float(i), {})
        self.assertLessEqual(len(log.rows), 100)
        self.assertEqual(log.rows[-1]["seq"], 1000)
        self.assertEqual(log.dropped_rows + len(log.rows), 1000)

    def test_cli_replay_reads_rotated_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            log = EventLog(path, max_bytes=200, backups=3)
            for i in range(10):
                log.emit("tick", float(i), {"i": i})
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                main(["dataset", str(path), "--out", str(Path(d) / "ds.jsonl"), "--include-rotated"])
            self.assertTrue(rotated_paths(path))


def _raw_server(response: bytes):
    """A one-shot TCP server that answers any request with ``response``."""
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
    except OSError:
        return None

    def answer():
        conn, _ = sock.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(response)
        sock.close()

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return sock.getsockname()[1], thread


if __name__ == "__main__":
    unittest.main()
