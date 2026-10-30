"""Domain layer: task semantics, composed workflows, business records, criteria.

A domain describes *what* a task means in a business setting (a convenience
store, an office, a home) independently of which robot executes it.
Workflows expand task parameters into robot-agnostic step templates; the
embodiment layer later binds each step to a capable robot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..eep.errors import InvalidRequest, NotFound
from ..eep.schema import SchemaError, validate
from ..eep.types import DomainDescriptor, StepTemplate, ToolDescriptor, WorkflowDescriptor
from .records import BusinessRecords

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.harness import Harness

# Completion criteria evaluated after each skill unless a step overrides them.
DEFAULT_CRITERIA: dict[str, list[str]] = {
    "navigate_to": ["at_location"],
    "pick": ["holding_target"],
    "place": ["on_slot"],
    "hang": ["on_hook"],
    "handover": ["delivered"],
    "wipe": ["surface_clean"],
    "spread": ["cloth_coverage"],
}


@dataclass
class Workflow:
    name: str
    description: str
    params: dict[str, Any]
    build: Callable[[dict[str, Any], BusinessRecords], list[StepTemplate]]

    def descriptor(self, domain: str) -> WorkflowDescriptor:
        return WorkflowDescriptor(name=self.name, domain=domain, description=self.description, params=self.params)


@dataclass
class ComposedTool:
    name: str
    description: str
    params: dict[str, Any]
    fn: Callable[["Harness", "Domain", dict[str, Any]], dict[str, Any]]

    def descriptor(self, domain: str) -> ToolDescriptor:
        return ToolDescriptor(name=self.name, domain=domain, description=self.description, params=self.params)


@dataclass
class Domain:
    name: str
    description: str
    records: BusinessRecords
    workflows: dict[str, Workflow] = field(default_factory=dict)
    tools: dict[str, ComposedTool] = field(default_factory=dict)
    vocabulary: list[str] = field(default_factory=list)
    safety: list[str] = field(default_factory=list)

    def describe(self) -> DomainDescriptor:
        return DomainDescriptor(
            name=self.name,
            description=self.description,
            vocabulary=list(self.vocabulary),
            safety=list(self.safety),
            workflows=[w.descriptor(self.name) for w in self.workflows.values()],
            tools=[t.descriptor(self.name) for t in self.tools.values()],
        )

    def workflow(self, name: str) -> Workflow:
        try:
            return self.workflows[name]
        except KeyError:
            raise NotFound(f"domain '{self.name}' has no workflow '{name}'", data={"workflows": sorted(self.workflows)}) from None

    def expand(self, workflow: str, params: dict[str, Any]) -> list[StepTemplate]:
        wf = self.workflow(workflow)
        try:
            validate(params, wf.params)
        except SchemaError as exc:
            raise InvalidRequest(f"invalid params for {self.name}.{workflow}: {exc}", data={"errors": exc.errors}) from None
        try:
            steps = wf.build(params, self.records)
        except KeyError as exc:
            raise InvalidRequest(f"{self.name}.{workflow}: {exc}") from None
        for s in steps:
            if not s.criteria and s.skill in DEFAULT_CRITERIA:
                s.criteria = list(DEFAULT_CRITERIA[s.skill])
        return steps

    def context_tags(self, args: dict[str, Any]) -> list[str]:
        """Agent-visible context tags for a step, derived from business records."""
        tags: set[str] = set()
        sku = args.get("sku")
        if isinstance(sku, str) and sku in self.records.catalog:
            item = self.records.catalog[sku]
            tags.update(item.tags)
            tags.add(f"category:{item.category}")
            if self.records.lookalikes(sku):
                tags.add("look_alike")
        loc = args.get("location")
        if isinstance(loc, str):
            place = self.records.place(loc)
            if place is not None:
                tags.update(place.tags)
                tags.add(f"place:{place.kind}")
        slot = args.get("slot")
        if isinstance(slot, str) and slot.startswith("hook"):
            tags.add("hook")
        return sorted(tags)


def lookup_tool(domain_name: str) -> ComposedTool:
    def fn(harness: "Harness", domain: Domain, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("sku") or "")
        items = domain.records.lookup(query, limit=int(args.get("limit", 8)))
        return {
            "query": query,
            "items": [
                dict(it.to_dict(), lookalikes=domain.records.lookalikes(it.sku))
                for it in items
            ],
        }

    return ComposedTool(
        name="lookup",
        description=f"Search the {domain_name} business records (catalog: names, attributes, reference images, "
        "planogram home/slot, look-alike families, likely locations).",
        params={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "sku, name, or attribute words"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
        fn=fn,
    )


def orders_tool() -> ComposedTool:
    def fn(harness: "Harness", domain: Domain, args: dict[str, Any]) -> dict[str, Any]:
        return {"orders": [o.to_dict() for o in domain.records.open_orders()]}

    return ComposedTool(
        name="orders",
        description="List open orders/requests recorded for this domain.",
        params={"type": "object", "properties": {}},
        fn=fn,
    )


def item_chain(prefix: str, *, sku: str, name: str, source: str | None, candidates: list[str] | None,
               destination: str, final_skill: str, final_args: dict[str, Any], final_criteria: list[str],
               depends_on: list[str] | None = None) -> list[StepTemplate]:
    """navigate/search -> pick -> navigate -> final skill, all on one robot."""
    steps: list[StepTemplate] = []
    deps = list(depends_on or [])
    if source:
        steps.append(StepTemplate(f"{prefix}-go-src", "navigate_to", {"location": source}, f"Go to {source} for the {name}",
                                  depends_on=deps, requires=["mobile"], group=prefix))
        pick_loc: Any = source
        prev = f"{prefix}-go-src"
    else:
        steps.append(StepTemplate(f"{prefix}-search", "search", {"sku": sku, "candidates": list(candidates or [])},
                                  f"Find the {name}", depends_on=deps, requires=["mobile"], group=prefix, criteria=[]))
        pick_loc = f"@{prefix}-search.location"
        prev = f"{prefix}-search"
    steps.append(StepTemplate(f"{prefix}-pick", "pick", {"sku": sku, "location": pick_loc}, f"Pick up the {name}",
                              depends_on=[prev], group=prefix))
    steps.append(StepTemplate(f"{prefix}-go-dst", "navigate_to", {"location": destination}, f"Carry the {name} to {destination}",
                              depends_on=[f"{prefix}-pick"], requires=["mobile"], group=prefix))
    steps.append(StepTemplate(f"{prefix}-{final_skill}", final_skill, dict(final_args), f"{final_skill.capitalize()} the {name} at {destination}",
                              depends_on=[f"{prefix}-go-dst"], criteria=list(final_criteria), group=prefix))
    return steps
