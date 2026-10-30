"""Asynchronous reflection: consolidate execution records into instructions.

Reflection reads the temporary memory accumulated since the last pass and
proposes long-term instructions. Proposals never write memory directly:
they go through shared admission (``harness.admission``), which validates
them, checks evidence, merges duplicates and refuses unsafe changes.

Two reflectors are provided:

* :class:`RuleReflector` – deterministic mining of recurring patterns
  (recoveries that worked, contradicted success verdicts, workspace
  intrusions, transit drops, location hints, capability mismatches). It needs
  no model and is used in tests and offline benchmarks.
* :class:`LLMReflector` – asks a language model to read the records and
  propose instructions in JSON, the way a coding agent reflects.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ..harness import recovery as rec_lib
from .store import ExecutionRecord, LongTermMemory, MemoryStore, TemporaryMemory

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.admission import SharedAdmission

SALIENT_TAGS = {
    "hook", "small", "look_alike", "new", "novel_for_policy", "customer_area", "hooks", "occluded", "edge", "guests", "people",
}


def salient(tags: list[str]) -> set[str]:
    return {t for t in tags if t in SALIENT_TAGS or t.startswith("place:")}


@dataclass
class Proposal:
    scope: str
    kind: str
    text: str
    when: dict[str, Any] = field(default_factory=dict)
    patch: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    source: str = "reflection:rule"
    rationale: str = ""
    supersedes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "scope": self.scope,
            "kind": self.kind,
            "text": self.text,
            "when": self.when,
            "patch": self.patch,
            "evidence": list(self.evidence),
            "source": self.source,
        }
        if self.rationale:
            out["rationale"] = self.rationale
        if self.supersedes:
            out["supersedes"] = self.supersedes
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "agent") -> "Proposal":
        when = data.get("when") or {}
        patch = data.get("patch") or {}
        return cls(
            scope=str(data.get("scope", "domain")),
            kind=str(data.get("kind", "guidance")),
            text=str(data.get("text", "")).strip(),
            when=dict(when) if isinstance(when, dict) else {"__invalid__": when},
            patch=dict(patch) if isinstance(patch, dict) else {"__invalid__": patch},
            evidence=[str(x) for x in data.get("evidence") or [] if isinstance(x, (str, int))],
            source=source,  # set by the calling path, never by the proposal's author
            rationale=str(data.get("rationale") or ""),
            supersedes=data.get("supersedes"),
        )


@dataclass
class Retirement:
    entry_id: str
    reason: str


class Reflector(Protocol):
    name: str

    def propose(self, pending: list[ExecutionRecord], temporary: TemporaryMemory, long_term: LongTermMemory) -> list[Proposal]:
        ...

    def review(self, long_term: LongTermMemory) -> list[Retirement]:
        ...


def _context_text(tags: set[str]) -> str:
    if not tags:
        return ""
    words = []
    for t in sorted(tags):
        if t.startswith("place:"):
            words.append(f"at a {t.split(':', 1)[1].replace('_', ' ')}")
        elif t == "look_alike":
            words.append("with look-alike items")
        elif t in ("customer_area", "guests", "people"):
            words.append("near people")
        else:
            words.append(t.replace("_", " "))
    return " (" + ", ".join(words) + ")"


class RuleReflector:
    name = "rule"

    def __init__(self, min_support: int = 2, retire_after: int = 6, retire_below: float = 0.35) -> None:
        self.min_support = min_support
        self.retire_after = retire_after
        self.retire_below = retire_below

    # public ----------------------------------------------------------------

    def propose(self, pending: list[ExecutionRecord], temporary: TemporaryMemory, long_term: LongTermMemory) -> list[Proposal]:
        out: list[Proposal] = []
        out += self._recovered(pending, temporary)
        out += self._contradictions(pending)
        out += self._intrusions(pending)
        out += self._transit_drops(pending, temporary)
        out += self._location_hints(pending)
        out += self._capability_mismatch(pending)
        return out

    def review(self, long_term: LongTermMemory) -> list[Retirement]:
        out = []
        for e in long_term.admitted():
            if e.kind == "safety":
                continue
            if e.applied >= self.retire_after and e.succeeded / e.applied < self.retire_below:
                out.append(Retirement(e.entry_id, f"applied {e.applied}x with {e.succeeded} successes"))
        return out

    # patterns --------------------------------------------------------------

    def _recovered(self, pending: list[ExecutionRecord], temporary: TemporaryMemory) -> list[Proposal]:
        """A recovery that turned a failed attempt into a success, seen repeatedly."""
        groups: dict[tuple, list[tuple[ExecutionRecord, list[ExecutionRecord]]]] = defaultdict(list)
        for r in pending:
            if not r.success or not r.recovery or r.contradicted or r.kind != "execution":
                continue
            recovery = rec_lib.RECOVERIES.get(r.recovery)
            if recovery is None or not recovery.lesson:
                continue
            failures = [x for x in temporary.query(task_id=r.task_id, step_id=r.step_id)
                        if not x.success and x.attempt < r.attempt and x.kind == "execution"]
            if not failures:
                continue
            groups[(r.domain, r.skill, r.recovery)].append((r, failures))
        out = []
        for (domain, skill, name), cases in groups.items():
            steps = {(c.task_id, c.step_id) for c, _ in cases}
            if len(steps) < self.min_support:
                continue
            recovery = rec_lib.RECOVERIES[name]
            common = set.intersection(*(salient(c.tags) for c, _ in cases))
            models = {c.robot_model for c, _ in cases}
            evidence = []
            for c, fails in cases:
                evidence += [f.record_id for f in fails] + [c.record_id]
            when: dict[str, Any] = {}
            if domain:
                when["domain"] = domain
            patch = {k: v for k, v in recovery.patch.items() if k != "refresh_observation" or name == "add_target_box"}
            if name == "keep_gripper_closed":
                when.pop("domain", None)  # a gripper flaw is a property of the robot, not of the domain
                when["skill"] = "pick"
            elif name == "pause_on_intrusion":
                pass
            elif name in ("wait_for_service", "handoff"):
                patch = {"recovery_order": [name]}
            else:
                when["skill"] = skill
                if common:
                    when["tags_all"] = sorted(common)
            if recovery.scope == "embodiment" and len(models) == 1:
                when["robot_model"] = next(iter(models))
            codes = sorted({f.failure_code for _, fails in cases for f in fails if f.failure_code})
            out.append(Proposal(
                scope=recovery.scope,
                kind=recovery.kind,
                text=recovery.lesson.format(skill=skill, context=_context_text(common) if "skill" in when else ""),
                when=when,
                patch=patch,
                evidence=sorted(set(evidence)),
                rationale=f"'{name}' fixed {', '.join(codes) or 'failures'} in {len(steps)} steps",
            ))
        return out

    def _contradictions(self, pending: list[ExecutionRecord]) -> list[Proposal]:
        groups: dict[tuple, list[ExecutionRecord]] = defaultdict(list)
        for r in pending:
            if r.success and r.contradicted and not _is_drop(r.contradiction):
                groups[(r.domain, r.skill)].append(r)
        out = []
        for (domain, skill), recs in groups.items():
            if len(recs) < self.min_support:
                continue
            common = set.intersection(*(salient(r.tags) for r in recs))
            when: dict[str, Any] = {"skill": skill}
            if domain:
                when["domain"] = domain
            if common:
                when["tags_all"] = sorted(common)
            out.append(Proposal(
                scope="domain",
                kind="criteria",
                text=f"Verify {skill} targets and results{_context_text(common)} at close range before declaring success.",
                when=when,
                patch={"verify_close_range": True},
                evidence=[r.record_id for r in recs],
                rationale=f"{len(recs)} success verdicts for {skill} were later contradicted",
            ))
        return out

    def _intrusions(self, pending: list[ExecutionRecord]) -> list[Proposal]:
        by_domain: dict[str | None, list[ExecutionRecord]] = defaultdict(list)
        for r in pending:
            if r.failure_code == "workspace_intrusion":
                by_domain[r.domain].append(r)
        out = []
        for domain, recs in by_domain.items():
            when = {"domain": domain} if domain else {}
            out.append(Proposal(
                scope="domain",
                kind="safety",
                text=rec_lib.RECOVERIES["pause_on_intrusion"].lesson,
                when=when,
                patch={"pause_on_intrusion": True},
                evidence=[r.record_id for r in recs],
                rationale=f"{len(recs)} executions aborted because a person entered the workspace",
            ))
        return out

    def _transit_drops(self, pending: list[ExecutionRecord], temporary: TemporaryMemory) -> list[Proposal]:
        recs = []
        for r in pending:
            dropped = r.failure_code in ("not_holding", "dropped") and r.evidence.get("gripper_opened_in_transit")
            if dropped or (r.contradicted and _is_drop(r.contradiction)):
                recs.append(r)
        if len(recs) < self.min_support:
            return []
        models = sorted({r.robot_model for r in recs})
        when: dict[str, Any] = {"skill": "pick"}
        if len(models) == 1:
            when["robot_model"] = models[0]
        return [Proposal(
            scope="embodiment",
            kind="guidance",
            text=rec_lib.RECOVERIES["keep_gripper_closed"].lesson,
            when=when,
            patch={"keep_gripper_closed": True},
            evidence=[r.record_id for r in recs],
            rationale=f"gripper opened in transit {len(recs)} times on {', '.join(models)}",
        )]

    def _location_hints(self, pending: list[ExecutionRecord], consistency: float = 0.7) -> list[Proposal]:
        """Items repeatedly found somewhere other than where the search started."""
        found: dict[tuple, list[ExecutionRecord]] = defaultdict(list)
        for r in pending:
            if r.skill == "search" and r.success and not r.contradicted and r.evidence.get("location"):
                found[(r.domain, r.args.get("sku"))].append(r)
        groups: dict[tuple, list[ExecutionRecord]] = {}
        for (domain, sku), recs in found.items():
            by_loc: dict[str, list[ExecutionRecord]] = defaultdict(list)
            for r in recs:
                by_loc[r.evidence["location"]].append(r)
            loc, hits = max(by_loc.items(), key=lambda kv: len(kv[1]))
            if len(hits) / len(recs) < consistency:
                continue  # the item moves around; a fixed hint would mislead
            late = [r for r in hits if len(r.evidence.get("visited") or []) > 1]
            if late:
                groups[(domain, sku, loc)] = hits
        out = []
        for (domain, sku, loc), recs in groups.items():
            if not sku or len(recs) < self.min_support:
                continue
            when: dict[str, Any] = {"skill": "search", "sku": sku}
            if domain:
                when["domain"] = domain
            out.append(Proposal(
                scope="domain",
                kind="guidance",
                text=f"'{sku}' has repeatedly been found at {loc}; search there first.",
                when=when,
                patch={"search_first": [loc]},
                evidence=[r.record_id for r in recs],
                rationale=f"found at {loc} in {len(recs)} searches after checking other places first",
            ))
        return out

    def _capability_mismatch(self, pending: list[ExecutionRecord]) -> list[Proposal]:
        groups: dict[tuple, list[ExecutionRecord]] = defaultdict(list)
        for r in pending:
            if r.failure_code == "unreachable" and r.evidence.get("requires"):
                groups[(r.domain, r.skill, r.evidence["requires"])].append(r)
        out = []
        for (domain, skill, feature), recs in groups.items():
            when: dict[str, Any] = {"skill": skill}
            if domain:
                when["domain"] = domain
            out.append(Proposal(
                scope="embodiment",
                kind="coordination",
                text=f"Assign {skill} only to robots with '{feature}'.",
                when=when,
                patch={"prefer_features": [feature]},
                evidence=[r.record_id for r in recs],
                rationale=f"{len(recs)} {skill} attempts failed on robots without {feature}",
            ))
        return out


def _is_drop(reason: str | None) -> bool:
    return bool(reason) and bool(re.search(r"drop|gripper", reason or "", re.I))


# ---------------------------------------------------------------------------
# LLM reflector
# ---------------------------------------------------------------------------

REFLECTION_SYSTEM = """You are the reflection module of an embodied agent harness (GenieMaster-style).
You read execution records from robots (temporary memory) and the current long-term memory.
Propose a small number of reusable instructions that would have prevented repeated failures or
wasted recoveries. Model weights are fixed: only guidance, completion criteria, recovery and
coordination rules can change. Each proposal must cite record_ids as evidence and must not weaken
safety rules. Prefer specific, testable instructions over general advice.

Return ONLY a JSON object: {"proposals": [{"scope": "domain|embodiment",
"kind": "guidance|criteria|recovery|coordination|safety", "text": "...",
"when": {"domain": "...", "skill": "...", "robot_model": "...", "tags_all": ["..."], "sku": "..."},
"patch": {...optional machine-readable guidance patch...}, "evidence": ["record ids"],
"rationale": "..."}]}

Allowed patch keys: camera ("head"|"wrist"), arm, approach, constraints (list of strings),
detailed (string), options (object), use_reference, use_box, use_placement_point,
verify_close_range, pause_on_intrusion, keep_gripper_closed, prefer_features (list),
max_attempts (int), recovery_order (list of recovery names), search_first (list of locations)."""


class TextModel(Protocol):
    def complete(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        ...


class LLMReflector:
    name = "llm"

    def __init__(self, model: TextModel, *, max_records: int = 80) -> None:
        self.model = model
        self.max_records = max_records

    def build_prompt(self, pending: list[ExecutionRecord], long_term: LongTermMemory) -> str:
        interesting = [r for r in pending if not r.success or r.recovery or r.contradicted or r.kind != "execution"]
        if len(interesting) < self.max_records:
            rest = [r for r in pending if r not in interesting]
            interesting += rest[: self.max_records - len(interesting)]
        records = [r.brief() for r in interesting[-self.max_records:]]
        memory = [e.brief() for e in long_term.admitted()]
        return (
            "Current long-term memory:\n" + json.dumps(memory, ensure_ascii=False, indent=1)
            + "\n\nNew execution records since the last reflection:\n" + json.dumps(records, ensure_ascii=False, indent=1)
            + "\n\nPropose instructions as specified."
        )

    def propose(self, pending: list[ExecutionRecord], temporary: TemporaryMemory, long_term: LongTermMemory) -> list[Proposal]:
        if not pending:
            return []
        text = self.model.complete(REFLECTION_SYSTEM, self.build_prompt(pending, long_term), max_tokens=3000)
        return parse_proposals(text, source="reflection:llm")

    def review(self, long_term: LongTermMemory) -> list[Retirement]:
        return []


def parse_proposals(text: str, source: str = "reflection:llm") -> list[Proposal]:
    """Extract proposals from a model response (tolerates code fences and prose)."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("proposals", []) if isinstance(data, dict) else []
    return [Proposal.from_dict(p, source=source) for p in items if isinstance(p, dict)]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class ReflectionReport:
    reviewed: int
    decisions: list[dict[str, Any]]
    retired: list[str]
    version_before: int
    version_after: int

    @property
    def admitted(self) -> list[str]:
        return [d["entry_id"] for d in self.decisions if d["action"] == "admit"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed": self.reviewed,
            "admitted": self.admitted,
            "decisions": self.decisions,
            "retired": self.retired,
            "version_before": self.version_before,
            "version_after": self.version_after,
        }


def reflect(memory: MemoryStore, admission: "SharedAdmission", reflector: Reflector | None = None, *, t: float = 0.0,
            dry_run: bool = False, advance: bool = True) -> ReflectionReport:
    reflector = reflector or RuleReflector(min_support=admission.min_support)
    pending = memory.temporary.pending()
    before = memory.long_term.version
    decisions = []
    for proposal in reflector.propose(pending, memory.temporary, memory.long_term):
        decision = admission.check_proposal(proposal, memory) if dry_run else admission.admit(proposal, memory, t=t)
        decisions.append(dict(decision.to_dict(), proposal=proposal.to_dict()))
    retired = []
    if not dry_run:
        for r in reflector.review(memory.long_term):
            memory.long_term.retire(r.entry_id, reason=r.reason, t=t)
            retired.append(r.entry_id)
        if advance:
            memory.temporary.advance()
    return ReflectionReport(reviewed=len(pending), decisions=decisions, retired=retired, version_before=before,
                            version_after=memory.long_term.version)
