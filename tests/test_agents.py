import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from opengeniemaster.agents.llm_agent import FALLBACK_BETA, ClaudeAgent
from opengeniemaster.agents.mcp_server import MCPServer
from opengeniemaster.agents.tools import ToolRegistry

from helpers import sim

ROOT = Path(__file__).resolve().parents[1]


class MCPServerTest(unittest.TestCase):
    def setUp(self):
        self.h, self.sc = sim("restocking", 3)
        self.server = MCPServer(self.h)
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        return self.server.handle({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})

    def call(self, name, args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return res.get("structuredContent"), res["isError"]

    def test_initialize_negotiates_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["capabilities"])
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-11-25")
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_have_valid_names_and_schemas(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertIn("eep_execute", names)
        self.assertIn("store_inspect_shelf", names)
        for t in tools:
            self.assertRegex(t["name"], r"^[a-zA-Z0-9_-]{1,64}$")
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_full_task_through_tools(self):
        out, err = self.call("eep_task_create", {"goal": "restock", "domain": "store", "workflow": "restock",
                                                 "params": {"items": ["cola-330"]}})
        self.assertFalse(err)
        tid = out["task"]["task_id"]
        self.call("eep_dispatch", {"task_id": tid})
        for _ in range(10):
            ctx, _ = self.call("eep_task_get", {"task_id": tid, "steps": False})
            if not ctx["context"]["next_steps"]:
                break
            st = ctx["context"]["next_steps"][0]
            sg, _ = self.call("eep_suggest_guidance", {"task_id": tid, "step_id": st["step_id"]})
            fb, err = self.call("eep_execute", {"robot_id": sg["robot_id"], "skill": st["skill"], "args": sg["args"],
                                                "guidance": sg["guidance"], "task_id": tid, "step_id": st["step_id"]})
            self.assertFalse(err)
            for key in ("observation", "action_result", "continuity"):
                self.assertIn(key, fb)
        final, _ = self.call("eep_task_get", {"task_id": tid})
        self.assertIn(final["task"]["status"], ("completed", "active"))

    def test_errors_are_tool_errors_not_protocol_errors(self):
        out, err = self.call("eep_execute", {"robot_id": "ghost", "skill": "pick"})
        self.assertTrue(err)
        self.assertEqual(out["error"]["code"], "not_found")
        out, err = self.call("eep_execute", {"skill": "pick"})
        self.assertTrue(err)
        self.assertIn("robot_id", out["error"]["message"])
        r = self.rpc("tools/call", {"name": "does_not_exist", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertEqual(self.rpc("nope/nope")["error"]["code"], -32601)
        self.assertEqual(self.server.handle({"id": 1})["error"]["code"], -32600)

    def test_resources_and_prompts(self):
        uris = [r["uri"] for r in self.rpc("resources/list")["result"]["resources"]]
        self.assertIn("ogm://capabilities", uris)
        content = self.rpc("resources/read", {"uri": "ogm://memory/long-term"})["result"]["contents"][0]
        self.assertEqual(json.loads(content["text"])["entries"], [])
        prompts = [p["name"] for p in self.rpc("prompts/list")["result"]["prompts"]]
        self.assertEqual(prompts, ["geniemaster_operator", "geniemaster_reflect"])
        msg = self.rpc("prompts/get", {"name": "geniemaster_operator", "arguments": {"goal": "restock the hooks"}})["result"]
        self.assertIn("restock the hooks", msg["messages"][0]["content"]["text"])

    def test_agent_led_reflection_through_tools(self):
        from opengeniemaster.harness.orchestrator import GENIEMASTER, ScriptedOrchestrator

        from helpers import create_scenario_task

        ScriptedOrchestrator(self.h, GENIEMASTER).run(create_scenario_task(self.h, self.sc).task_id)
        recs, _ = self.call("memory_records", {"limit": 50})
        ids = [r["record_id"] for r in recs["records"]][:2]
        out, err = self.call("memory_propose", {"proposals": [{
            "scope": "domain", "kind": "guidance", "text": "Restock hook items before shelf items when both are missing.",
            "when": {"domain": "store", "skill": "hang"}, "evidence": ids}]})
        self.assertFalse(err)
        self.assertEqual(out["decisions"][0]["action"], "admit")
        found, _ = self.call("memory_search", {"query": "hook items"})
        self.assertEqual(len(found["entries"]), 1)

    def test_stdio_serve_loop(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "not json",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        out = io.StringIO()
        self.server.serve(io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)

    def test_subprocess_server(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.run([sys.executable, "-m", "opengeniemaster", "mcp", "--scenario", "cleaning"],
                              input=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n",
                              capture_output=True, text=True, timeout=60, env=env, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reply = json.loads(proc.stdout.splitlines()[0])
        self.assertEqual(reply["id"], 7)
        self.assertGreater(len(reply["result"]["tools"]), 15)


# ---------------------------------------------------------------------------
# Built-in Claude agent with a fake Messages API client
# ---------------------------------------------------------------------------


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)(kwargs)


class FakeClient:
    def __init__(self, script):
        self.beta = SimpleNamespace(messages=FakeMessages(script))


class ClaudeAgentTest(unittest.TestCase):
    def test_tool_loop_runs_tools_and_returns_text(self):
        h, _ = sim("restocking", 0)

        def turn1(kw):
            return SimpleNamespace(stop_reason="tool_use", content=[
                block("thinking", thinking="", signature="sig"),
                block("tool_use", id="tu1", name="eep_capabilities", input={}),
                block("tool_use", id="tu2", name="eep_observe", input={"robot_id": "ghost"}),
            ])

        def turn2(kw):
            results = kw["messages"][-1]["content"]
            assert [r["tool_use_id"] for r in results] == ["tu1", "tu2"]
            assert results[1]["is_error"] is True
            return SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Three robots are docked.")])

        client = FakeClient([turn1, turn2])
        run = ClaudeAgent(h, client=client).run("What robots do we have?")
        self.assertEqual(run.final_text, "Three robots are docked.")
        self.assertEqual((run.turns, run.tool_calls), (2, 2))
        first = client.beta.messages.calls[0]
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["betas"], [FALLBACK_BETA])
        self.assertEqual(first["output_config"], {"effort": "high"})
        self.assertNotIn("thinking", first)
        self.assertEqual({t["name"] for t in first["tools"]}, set(ToolRegistry(h).tools))
        # the assistant turn is appended unchanged (thinking blocks included)
        second = client.beta.messages.calls[1]
        self.assertEqual(second["messages"][1]["content"][0].type, "thinking")

    def test_refusal_stops_the_loop(self):
        h, _ = sim("restocking", 0)
        client = FakeClient([lambda kw: SimpleNamespace(stop_reason="refusal", content=[],
                                                        stop_details=SimpleNamespace(category="cyber"))])
        events = []
        run = ClaudeAgent(h, client=client, on_event=lambda k, d: events.append((k, d))).run("x")
        self.assertEqual(run.stop_reason, "refusal")
        self.assertEqual(events, [("refusal", {"category": "cyber"})])

    def test_fallbacks_can_be_disabled(self):
        h, _ = sim("restocking", 0)
        client = FakeClient([lambda kw: SimpleNamespace(stop_reason="end_turn", content=[block("text", text="ok")])])
        ClaudeAgent(h, client=client, server_fallbacks=False).run("x")
        self.assertNotIn("fallbacks", client.beta.messages.calls[0])


if __name__ == "__main__":
    unittest.main()
