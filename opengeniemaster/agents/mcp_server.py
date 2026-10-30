"""Model Context Protocol server (stdio, JSON-RPC 2.0) exposing EEP as tools.

GenieMaster's EEP is described as inspired by MCP's tool abstraction; here
EEP is served *over* MCP so that any MCP-capable coding agent (Claude Code,
Codex, ...) can act as the harness's coding agent. Implemented with the
standard library only: newline-delimited JSON-RPC messages on stdin/stdout,
logs on stderr.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import IO, TYPE_CHECKING, Any

from .. import __version__
from ..eep.errors import EEPError
from .prompts import OPERATOR_PROMPT, REFLECTION_PROMPT, SERVER_INSTRUCTIONS
from .tools import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.harness import Harness

log = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST = SUPPORTED_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    def __init__(self, harness: "Harness", *, name: str = "opengeniemaster") -> None:
        self.harness = harness
        self.registry = ToolRegistry(harness)
        self.name = name
        self.protocol_version = LATEST
        self.initialized = False

    # ------------------------------------------------------------- dispatch

    def handle(self, message: Any) -> Any:
        """Handle one decoded JSON-RPC message (or batch); returns the response or None."""
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            responses = [r for r in (self.handle(m) for m in message) if r is not None]
            return responses or None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message
        if method is None:
            if "result" in message or "error" in message:
                return None  # a response to a server-initiated request; nothing to do
            return _error(msg_id, INVALID_REQUEST, "missing method")
        if not isinstance(method, str):
            return None if is_notification else _error(msg_id, INVALID_REQUEST, "method must be a string")
        params = message.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if is_notification else _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            result = self._dispatch(method, params)
        except RPCError as exc:
            return None if is_notification else _error(msg_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("internal error in %s: %s\n%s", method, exc, traceback.format_exc())
            return None if is_notification else _error(msg_id, INTERNAL_ERROR, str(exc))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or LATEST)
            self.protocol_version = requested if requested in SUPPORTED_VERSIONS else LATEST
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                    "logging": {},
                },
                "serverInfo": {"name": self.name, "title": "OpenGenieMaster EEP", "version": __version__},
                "instructions": SERVER_INSTRUCTIONS,
            }
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "logging/setLevel":
            level = str(params.get("level", "info")).upper()
            logging.getLogger("opengeniemaster").setLevel(getattr(logging, level, logging.INFO))
            return {}
        if method == "tools/list":
            return {"tools": [t.mcp() for t in self.registry.list()]}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": self._resources()}
        if method == "resources/templates/list":
            return {"resourceTemplates": [{"uriTemplate": "ogm://tasks/{task_id}", "name": "task", "title": "Task continuity state",
                                           "mimeType": "application/json"}]}
        if method == "resources/read":
            return self._read_resource(str(params.get("uri", "")))
        if method == "prompts/list":
            return {"prompts": [
                {"name": "geniemaster_operator", "title": "Operate robots through EEP",
                 "description": "Operating procedure for a coding agent driving the harness.",
                 "arguments": [{"name": "goal", "description": "What the robots should accomplish", "required": False}]},
                {"name": "geniemaster_reflect", "title": "Reflect into long-term memory",
                 "description": "Review recent execution records and propose evidence-backed instructions.", "arguments": []},
            ]}
        if method == "prompts/get":
            return self._get_prompt(str(params.get("name", "")), params.get("arguments") or {})
        if method == "completion/complete":
            return {"completion": {"values": [], "hasMore": False}}
        raise RPCError(METHOD_NOT_FOUND, f"method not found: {method}")

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise RPCError(INVALID_PARAMS, "tools/call needs a tool name")
        if name not in self.registry.tools:
            raise RPCError(INVALID_PARAMS, f"unknown tool: {name}")
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            raise RPCError(INVALID_PARAMS, "tool arguments must be an object")
        try:
            result = self.registry.call(name, arguments)
        except EEPError as exc:
            payload = {"error": exc.to_dict()}
            return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload,
                    "isError": True}
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
        out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
        if isinstance(result, dict):
            out["structuredContent"] = result
        return out

    def _resources(self) -> list[dict[str, Any]]:
        res = [
            {"uri": "ogm://capabilities", "name": "capabilities", "title": "Capability contract", "mimeType": "application/json"},
            {"uri": "ogm://memory/long-term", "name": "long-term-memory", "title": "Long-term memory", "mimeType": "application/json"},
            {"uri": "ogm://tasks", "name": "tasks", "title": "Tasks", "mimeType": "application/json"},
        ]
        for task in self.harness.list_tasks():
            res.append({"uri": f"ogm://tasks/{task.task_id}", "name": task.task_id, "title": task.goal[:80], "mimeType": "application/json"})
        return res

    def _read_resource(self, uri: str) -> dict[str, Any]:
        h = self.harness
        if uri == "ogm://capabilities":
            data: Any = h.capabilities()
        elif uri == "ogm://memory/long-term":
            data = {"version": h.memory.long_term.version, "entries": [e.brief() for e in h.memory.long_term.admitted()]}
        elif uri == "ogm://tasks":
            data = {"tasks": [t.summary() for t in h.list_tasks()]}
        elif uri.startswith("ogm://tasks/"):
            try:
                task = h.get_task(uri.rsplit("/", 1)[-1])
            except EEPError as exc:
                raise RPCError(INVALID_PARAMS, exc.message) from None
            data = task.to_dict()
        else:
            raise RPCError(INVALID_PARAMS, f"unknown resource: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(data, ensure_ascii=False, default=str)}]}

    def _get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "geniemaster_operator":
            text = OPERATOR_PROMPT
            if arguments.get("goal"):
                text += f"\n\nGoal: {arguments['goal']}"
            return {"description": "Operate robots through EEP", "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if name == "geniemaster_reflect":
            return {"description": "Reflect into long-term memory",
                    "messages": [{"role": "user", "content": {"type": "text", "text": REFLECTION_PROMPT}}]}
        raise RPCError(INVALID_PARAMS, f"unknown prompt: {name}")

    # ---------------------------------------------------------------- serve

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        inp = stdin or sys.stdin
        out = stdout or sys.stdout
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response: Any = _error(None, PARSE_ERROR, f"parse error: {exc}")
            else:
                response = self.handle(message)
            if response is not None:
                out.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
                out.flush()


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}
