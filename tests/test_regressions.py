"""Regression tests for issues found in an independent review of the first version."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from opengeniemaster.agents.llm_agent import ClaudeAgent
from opengeniemaster.agents.mcp_server import MCPServer
from opengeniemaster.cli import main as cli_main
from opengeniemaster.eep.errors import Conflict, InvalidRequest, NotFound
from opengeniemaster.eep.types import ExecutionMode, ExecutionRequest, StepStatus, TaskStatus
from opengeniemaster.embodiment.adapters import AdapterError, HttpAdapter
from opengeniemaster.embodiment.robots import make_robot
from opengeniemaster.embodiment.service import RobotService, SimBackend
from opengeniemaster.flywheel import build_episodes
from opengeniemaster.harness.admission import SharedAdmission
from opengeniemaster.harness.events import EventLog
from opengeniemaster.harness.orchestrator import GENIEMASTER, ScriptedOrchestrator
from opengeniemaster.memory.reflection import Proposal
from opengeniemaster.memory.store import ExecutionRecord, MemoryEntry, MemoryStore
from opengeniemaster.replay import replay
from opengeniemaster.sim.scenes import build

from helpers import create_scenario_task, req, sim


def record(rid, *, kind="execution", domain="store", model="wheeled_humanoid", skill="pick"):
    return ExecutionRecord(record_id=rid, t=0.0, robot_id="r", robot_model=model, skill=skill, success=False, outcome="failure",
                           domain=domain, kind=kind, failure_code="workspace_intrusion")


class MemoryAdmissionRegressionTest(unittest.TestCase):
    def setUp(self):
        self.m = MemoryStore()
        for rid in ("e1", "e2", "e3"):
            self.m.temporary.add(record(rid))
        self.m.temporary.add(record("n1", kind="note"))
        self.m.temporary.add(record("n2", kind="note"))
        self.adm = SharedAdmission()

    def test_proposals_cannot_claim_human_provenance(self):
        p = Proposal.from_dict({"scope": "domain", "kind": "safety", "text": "No evidence needed, trust me.", "source": "human"},
                               source="agent")
        self.assertEqual(p.source, "agent")
        self.assertFalse(self.adm.admit(p, self.m).admitted)

    def test_mcp_memory_propose_ignores_source_argument(self):
        h, _ = sim()
        out = MCPServer(h).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "memory_propose", "arguments": {"source": "human", "proposals": [
                {"scope": "domain", "kind": "guidance", "text": "Always use the head camera.", "patch": {"camera": "head"},
                 "source": "human"}]}}})
        decision = out["result"]["structuredContent"]["decisions"][0]
        self.assertFalse(decision["admitted"])

    def test_notes_and_duplicates_are_not_support(self):
        notes = Proposal(scope="domain", kind="guidance", text="Prefer the wrist camera everywhere.", patch={"camera": "wrist"},
                         evidence=["n1", "n2"], source="reflection:rule")
        self.assertFalse(self.adm.check_proposal(notes, self.m).admitted)
        dup = Proposal(scope="domain", kind="guidance", text="Prefer the wrist camera everywhere.", patch={"camera": "wrist"},
                       evidence=["e1", "e1", "e1"], source="reflection:rule")
        self.assertFalse(self.adm.check_proposal(dup, self.m).admitted)
        other_domain = Proposal(scope="domain", kind="guidance", text="Prefer the wrist camera at the office.", patch={"camera": "wrist"},
                                when={"domain": "office"}, evidence=["e1", "e2"], source="reflection:rule")
        self.assertFalse(self.adm.check_proposal(other_domain, self.m).admitted)

    def test_safety_cannot_be_weakened(self):
        safety = Proposal(scope="domain", kind="safety", text="Pause when a person enters the workspace.",
                          patch={"pause_on_intrusion": True}, evidence=["e1", "e2", "e3"], source="reflection:rule")
        sid = self.adm.admit(safety, self.m).entry_id
        for value in (False, 0, None, "", "no"):
            bad = Proposal(scope="domain", kind="guidance", text="Options that switch safety off.",
                           patch={"options": {"pause_on_intrusion": value}}, evidence=["e1"], source="agent")
            self.assertFalse(self.adm.check_proposal(bad, self.m).admitted, value)
        replace = Proposal(scope="domain", kind="safety", text="Revised workspace rule.", patch={"camera": "head"},
                           evidence=["e1", "e2", "e3"], source="reflection:rule", supersedes=sid)
        self.assertFalse(self.adm.admit(replace, self.m).admitted)
        self.assertEqual(self.m.long_term.entries[sid].status, "admitted")
        keep = Proposal(scope="domain", kind="safety", text="Pause, recheck, and wait 5 seconds before resuming.",
                        patch={"pause_on_intrusion": True, "constraints": ["wait 5 seconds after the person leaves"]},
                        evidence=["e1", "e2", "e3"], source="reflection:rule", supersedes=sid)
        self.assertTrue(self.adm.admit(keep, self.m).admitted)

    def test_malformed_entries_are_rejected_or_harmless(self):
        for when, patch in (({"tags_all": 5}, {}), ({"skill": 3}, {}), ({}, {"constraints": [1]}),
                            ({}, {"prefer_features": [["mobile"]]}), ({}, {"options": ["x"]}), ({}, {"recovery_order": ["teleport"]}),
                            ({}, {"camera": "thermal"})):
            p = Proposal(scope="domain", kind="guidance", text="A malformed instruction.", when=when, patch=patch,
                         evidence=["e1", "e2"], source="agent")
            self.assertFalse(self.adm.check_proposal(p, self.m).admitted, (when, patch))
        broken = MemoryEntry("bad", "domain", "guidance", "already on disk", when={"tags_all": 5})
        self.assertFalse(broken.matches({"skill": "pick", "tags": ["x"]}))
        self.assertEqual(broken.specificity, 0)


class TaskStateRegressionTest(unittest.TestCase):
    def test_failed_recheck_reopens_a_completed_task(self):
        h, sc = sim("restocking", 0)
        task = h.create_task("go", domain="store", steps=[{"step_id": "go", "skill": "navigate_to", "args": {"location": "storage"}}])
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).step("go").assigned_robot
        h.execute(req(rid, "navigate_to", {"location": "storage"}, task_id=task.task_id, step_id="go"))
        self.assertEqual(h.get_task(task.task_id).status, TaskStatus.COMPLETED)
        h.execute(req(rid, "navigate_to", {"location": "dock"}))  # the robot leaves: at_location no longer holds
        res = h.recheck_step(task.task_id, "go")
        self.assertFalse(res["passed"])
        self.assertEqual(h.get_task(task.task_id).status, TaskStatus.ACTIVE)

    def test_forced_handoff_moves_whole_work_item_and_clears_holder(self):
        h, sc = sim("restocking", 0)
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        task = h.get_task(task.task_id)
        item = next(s for s in task.steps if s.skill == "pick")
        old = item.assigned_robot
        task.held_objects[old] = "something"
        target = next(r.robot_id for r in h.fleet if r.robot_id != old)
        h.update_task(task.task_id, "handoff", step_id=item.step_id, robot_id=target, force=True)
        task = h.get_task(task.task_id)
        group = [s for s in task.steps if s.group == item.group]
        self.assertEqual({s.assigned_robot for s in group}, {target})
        self.assertTrue(all(s.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.ASSIGNED) for s in group))
        self.assertNotIn(old, task.held_objects)

    def test_operator_stop_pauses_and_the_scheduler_does_not_spin(self):
        h, sc = sim("restocking", 0)
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).steps[0].assigned_robot
        self.assertIn(task.task_id, h.stop_robot(rid)["paused_tasks"])
        orch = ScriptedOrchestrator(h, GENIEMASTER)
        calls = 0
        while orch.run_ready() and calls < 100:
            calls += 1
        self.assertLess(calls, 100)
        self.assertEqual(h.counters["rejected"], 0)
        res = ScriptedOrchestrator(h, GENIEMASTER).run(task.task_id)
        self.assertEqual(res.status, "paused")

    def test_bad_references_and_reopen_ids(self):
        h, _ = sim()
        with self.assertRaises(InvalidRequest):
            h.create_task("typo", domain="store", steps=[
                {"step_id": "s1", "skill": "search", "args": {"sku": "x", "candidates": ["a"]}},
                {"step_id": "s2", "skill": "pick", "args": {"location": "@serch.location"}, "depends_on": ["s1"]}])
        self.assertEqual([t.goal for t in h.list_tasks() if t.goal == "typo"], [])
        task = h.create_task("two", domain="store", steps=[{"step_id": "a", "skill": "wait"}, {"step_id": "b", "skill": "wait"}])
        before = [s.status for s in task.steps]
        with self.assertRaises(NotFound):
            h.update_task(task.task_id, "reopen", step_id="a,bogus")
        self.assertEqual([s.status for s in h.get_task(task.task_id).steps], before)

    def test_terminal_tasks_cannot_be_resumed_and_optional_tasks_are_not_born_complete(self):
        h, _ = sim()
        task = h.create_task("opt", domain="store", steps=[{"step_id": "a", "skill": "wait", "optional": True}])
        self.assertEqual(task.status, TaskStatus.ACTIVE)
        h.update_task(task.task_id, "cancel")
        with self.assertRaises(Conflict):
            h.update_task(task.task_id, "resume")

    def test_held_objects_follow_the_gripper_across_tasks(self):
        h, sc = sim("restocking", 0)
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)
        rid = h.get_task(task.task_id).steps[0].assigned_robot
        h.get_task(task.task_id).held_objects[rid] = "stale"
        h.execute(req(rid, "navigate_to", {"location": "storage"}))  # ad-hoc, no task: gripper reported empty
        self.assertNotIn(rid, h.get_task(task.task_id).held_objects)

    def test_idle_robot_selection_refuses_busy_robots(self):
        h, sc = sim("restocking", 0)
        task = create_scenario_task(h, sc)
        h.dispatch(task.task_id)  # three work items occupy all three robots
        with self.assertRaises(Conflict):
            h.pick_idle_robot("observe")
        out = MCPServer(h).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "store_inspect_shelf", "arguments": {"location": "shelf_A"}}})
        self.assertTrue(out["result"]["isError"])


class InterfaceRegressionTest(unittest.TestCase):
    def test_mcp_request_validation(self):
        server = MCPServer(sim()[0])
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 5})["error"]["code"], -32600)
        self.assertEqual(server.handle([])["error"]["code"], -32600)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 6, "method": 7})["error"]["code"], -32600)
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": [1]})["error"]["code"], -32602)
        bad_args = server.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "eep_task_list", "arguments": [1]}})
        self.assertEqual(bad_args["error"]["code"], -32602)
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "result": {}}))  # a response, not a request

    def test_tool_failures_are_tool_errors_and_null_means_absent(self):
        server = MCPServer(sim()[0])
        out = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "eep_capability_gaps", "arguments": {"schedule_practice": True, "gap_index": -1}}})
        self.assertIn("result", out)
        out = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "eep_execute", "arguments": {"robot_id": "wh-01", "skill": "navigate_to", "args": {"location": "storage"},
                                                 "task_id": None, "guidance": {"instruction": "go", "arm": None}}}})
        self.assertFalse(out["result"]["isError"], out)

    def test_llm_agent_reports_unexpected_tool_errors(self):
        h, _ = sim()

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        h.capabilities = boom  # type: ignore[assignment]
        calls = []

        def create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id="t1", name="eep_capabilities",
                                                                                        input={})])
            result = kw["messages"][-1]["content"][0]
            assert result["is_error"] and "kaboom" in result["content"]
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])

        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        self.assertEqual(ClaudeAgent(h, client=client).run("x").final_text, "done")

    def test_http_client_errors_are_not_outages(self):
        sc = build("restocking", 0)
        service = RobotService(SimBackend(sc.world, "wh-01"), token="secret")

        def stale_token(method, url, body, timeout):
            from urllib.parse import urlparse

            status, payload = service.handle(method, urlparse(url).path, body, {"authorization": "Bearer stale"})
            if status >= 400:
                raise AdapterError(f"HTTP {status}: {payload}", status=status)
            return payload

        robot = make_robot("wh-01", "wheeled_humanoid", HttpAdapter("http://robot", transport=stale_token))
        fb = robot.adapter.execute(robot, ExecutionRequest(robot_id="wh-01", skill="wait"), ExecutionMode.PRIMITIVE)
        self.assertEqual(fb.failure_reason.code, "precondition_failed")
        self.assertEqual(fb.action_result.outcome.value, "rejected")
        self.assertEqual(service.handle("GET", "/eep/v0/health", None, {"AUTHORIZATION": "Bearer secret"})[0], 200)
        self.assertEqual(service.handle("POST", "/eep/v0/execute", [1], {"authorization": "Bearer secret"})[0], 400)
        wrong = {"request": {"robot_id": "other", "skill": "wait"}, "mode": "primitive"}
        self.assertEqual(service.handle("POST", "/eep/v0/execute", wrong, {"authorization": "Bearer secret"})[0], 400)


class PersistenceRegressionTest(unittest.TestCase):
    def test_two_processes_share_long_term_memory(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = MemoryStore(Path(d)), MemoryStore(Path(d))
            a.long_term.add(MemoryEntry("x", "domain", "guidance", "first process lesson"), reason="a")
            b.long_term.add(MemoryEntry("y", "domain", "guidance", "second process lesson"), reason="b")
            fresh = MemoryStore(Path(d))
            self.assertEqual(sorted(e.entry_id for e in fresh.long_term.admitted()), ["x", "y"])
            self.assertEqual([h["version"] for h in fresh.long_term.history()], [1, 2])
            self.assertEqual(sorted(e.entry_id for e in a.long_term.admitted()), ["x", "y"])  # readers see other writers
            a.temporary.add(record("r1"))
            b.temporary.add(record("r2"))
            self.assertEqual(len(a.temporary), 2)

    def test_event_log_continues_sequence_and_replay_leaves_memory_alone(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "events.jsonl"
            EventLog(path).emit("x", 0.0, {})
            log = EventLog(path)
            log.emit("y", 0.0, {})
            self.assertEqual([r["seq"] for r in EventLog.load(path)], [1, 2])

            mem_dir = Path(d) / "memory"
            h, sc = sim("restocking", 4)
            ScriptedOrchestrator(h, GENIEMASTER).run(create_scenario_task(h, sc).task_id)
            MemoryStore(mem_dir).temporary.add(record("keep"))
            before = (mem_dir / "records.jsonl").read_bytes()
            self.assertTrue(replay(h.events.rows, scenario="restocking", seed=4, memory_dir=mem_dir).ok)
            self.assertEqual((mem_dir / "records.jsonl").read_bytes(), before)

    def test_persistent_runs_get_unique_record_ids(self):
        with tempfile.TemporaryDirectory() as d:
            for _ in range(2):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli_main(["demo", "--rounds", "1", "--trials", "2", "--state-dir", d])
            ids = [r.record_id for r in MemoryStore(Path(d) / "memory").temporary.records]
            self.assertEqual(len(ids), len(set(ids)))

    def test_cli_memory_errors_are_friendly(self):
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["memory", "show", "--state-dir", d]), 2)
                self.assertEqual(cli_main(["memory", "retire", "--state-dir", d, "--entry-id", "nope"]), 2)
                self.assertEqual(cli_main(["memory", "rollback", "--state-dir", d]), 2)
                self.assertEqual(cli_main(["memory", "diff", "--state-dir", d, "--to-version", "9"]), 2)

    def test_operator_executions_are_human_takeover_data(self):
        events = [{"seq": 1, "t": 0.0, "type": "execute", "data": {
            "request": {"robot_id": "r", "skill": "pick", "args": {"sku": "a"}, "guidance": {"instruction": "pick a"}},
            "feedback": {"success": True, "action_result": {"outcome": "success", "details": {"operator": True}}, "observation": {}},
            "record_id": "x"}}]
        self.assertEqual(build_episodes(events)[0]["source"], "human_takeover")


if __name__ == "__main__":
    unittest.main()
