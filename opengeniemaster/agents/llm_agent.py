"""Built-in LLM coding agent: Claude drives the harness through the EEP tools.

This is the in-process alternative to connecting an external coding agent
over MCP. It runs a manual tool-use loop on the Anthropic Messages API (the
tool set is a JSON-schema registry routed to harness handlers, so the loop is
written out rather than using decorated tool functions).

Requires the optional dependency: ``pip install "opengeniemaster[claude]"``.
Credentials are resolved by the SDK (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``
or an ``ant auth login`` profile).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..eep.errors import EEPError
from ..memory.reflection import parse_proposals
from .prompts import OPERATOR_PROMPT, REFLECTION_PROMPT
from .tools import ToolRegistry

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.harness import Harness

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError('the built-in LLM agent needs the Anthropic SDK: pip install "opengeniemaster[claude]"') from exc
    return anthropic.Anthropic()


@dataclass
class AgentRun:
    final_text: str
    turns: int
    tool_calls: int
    stop_reason: str | None
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"final_text": self.final_text, "turns": self.turns, "tool_calls": self.tool_calls, "stop_reason": self.stop_reason}


class ClaudeAgent:
    """Manual tool-use loop over the harness tool registry.

    ``client`` defaults to ``anthropic.Anthropic()``; tests inject a fake with
    the same ``beta.messages.create`` surface.
    """

    def __init__(self, harness: "Harness", *, client: Any = None, model: str = DEFAULT_MODEL, effort: str = "high",
                 max_turns: int = 60, max_tokens: int = 16000, server_fallbacks: bool = True,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.harness = harness
        self.registry = ToolRegistry(harness)
        self.client = client
        self.model = model
        self.effort = effort
        self.max_turns = max_turns
        self.max_tokens = max_tokens
        self.server_fallbacks = server_fallbacks
        self.on_event = on_event or (lambda kind, data: None)

    def _client(self) -> Any:
        if self.client is None:
            self.client = make_client()
        return self.client

    def _create(self, system: str, messages: list[dict[str, Any]]) -> Any:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": [t.anthropic() for t in self.registry.list()],
            "messages": messages,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.server_fallbacks:
            # On a safety-classifier decline the API re-runs the request on Anthropic's recommended fallback model.
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return self._client().beta.messages.create(**params)

    def run(self, goal: str, *, system: str = OPERATOR_PROMPT) -> AgentRun:
        messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
        tool_calls = 0
        response = None
        for turn in range(1, self.max_turns + 1):
            response = self._create(system, messages)
            stop = response.stop_reason
            if stop == "refusal":
                details = getattr(response, "stop_details", None)
                self.on_event("refusal", {"category": getattr(details, "category", None)})
                return AgentRun("", turn, tool_calls, stop, messages)
            # Append the full content (thinking, fallback and tool_use blocks included) unchanged.
            messages.append({"role": "assistant", "content": response.content})
            if stop == "pause_turn":
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if stop == "tool_use" and tool_uses:
                results = []
                for block in tool_uses:
                    tool_calls += 1
                    results.append(self._run_tool(block))
                messages.append({"role": "user", "content": results})
                continue
            text = "\n".join(b.text for b in response.content if b.type == "text")
            if stop == "max_tokens":
                log.warning("response hit max_tokens; returning partial text")
            return AgentRun(text, turn, tool_calls, stop, messages)
        return AgentRun("", self.max_turns, tool_calls, getattr(response, "stop_reason", None), messages)

    def _run_tool(self, block: Any) -> dict[str, Any]:
        args = block.input if isinstance(block.input, dict) else {}
        self.on_event("tool_use", {"name": block.name, "input": args})
        try:
            result = self.registry.call(block.name, args)
            content = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
            is_error = False
        except EEPError as exc:
            content = json.dumps({"error": exc.to_dict()}, ensure_ascii=False)
            is_error = True
        except Exception as exc:  # every tool_use must get a tool_result
            content = json.dumps({"error": {"code": "tool_error", "message": f"{type(exc).__name__}: {exc}"}}, ensure_ascii=False)
            is_error = True
        self.on_event("tool_result", {"name": block.name, "is_error": is_error, "size": len(content)})
        out: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": content}
        if is_error:
            out["is_error"] = True
        return out


class ClaudeTextModel:
    """``complete(system, prompt)`` adapter for :class:`~opengeniemaster.memory.reflection.LLMReflector`."""

    def __init__(self, *, client: Any = None, model: str = DEFAULT_MODEL, effort: str = "high", server_fallbacks: bool = True) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.server_fallbacks = server_fallbacks

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4000) -> str:
        client = self.client or make_client()
        self.client = client
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self.effort},
        }
        if self.server_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        response = client.beta.messages.create(**params)
        if response.stop_reason == "refusal":
            return ""
        return "\n".join(b.text for b in response.content if b.type == "text")


def reflect_with_claude(harness: "Harness", *, client: Any = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Agent-led reflection: Claude reads pending records and proposes memory through admission."""
    agent = ClaudeAgent(harness, client=client, model=model)
    run = agent.run("Reflect on the pending execution records now.", system=OPERATOR_PROMPT + "\n\n" + REFLECTION_PROMPT)
    return {"run": run.to_dict(), "memory": harness.memory.stats()}


__all__ = ["AgentRun", "ClaudeAgent", "ClaudeTextModel", "DEFAULT_MODEL", "make_client", "parse_proposals", "reflect_with_claude"]
