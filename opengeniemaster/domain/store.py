"""Convenience-store domain: restocking, item retrieval, cleaning, inspection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..eep.types import ExecutionRequest, Guidance, StepTemplate
from .base import ComposedTool, Domain, Workflow, item_chain, lookup_tool, orders_tool
from .records import BusinessRecords

if TYPE_CHECKING:  # pragma: no cover
    from ..harness.harness import Harness

SAFETY = [
    "Pause manipulation when a customer or hand enters the workspace; recheck before resuming.",
    "Never throw away an item unless it is identified as trash.",
    "Keep aisles clear: do not leave items on the floor.",
]

_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"anyOf": [{"type": "string"}, {"type": "object", "properties": {"sku": {"type": "string"}, "from": {"type": "string"}}, "required": ["sku"]}]},
        },
    },
    "required": ["items"],
}


def _sku(entry: Any) -> tuple[str, str | None]:
    if isinstance(entry, dict):
        return entry["sku"], entry.get("from")
    return str(entry), None


def build_restock(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    steps: list[StepTemplate] = []
    for i, entry in enumerate(params["items"], 1):
        sku, src = _sku(entry)
        item = rec.item(sku)
        if not item.home:
            raise KeyError(f"'{sku}' has no planogram home")
        hook = item.mount == "hook"
        steps += item_chain(
            f"i{i}",
            sku=sku,
            name=item.name,
            source=src or item.stock_location or "storage",
            candidates=None,
            destination=item.home,
            final_skill="hang" if hook else "place",
            final_args={"sku": sku, "location": item.home, "slot": item.slot},
            final_criteria=["on_hook" if hook else "on_slot"],
        )
    return steps


def build_retrieve(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    deliver = params.get("deliver_to", "counter")
    by = params.get("deliver_by", "handover")
    steps: list[StepTemplate] = []
    for i, entry in enumerate(params["items"], 1):
        sku, src = _sku(entry)
        item = rec.item(sku)
        steps += item_chain(
            f"o{i}",
            sku=sku,
            name=item.name,
            source=src or item.home,
            candidates=item.likely_locations or None,
            destination=deliver,
            final_skill=by,
            final_args={"sku": sku, "location": deliver},
            final_criteria=["delivered"] if by == "handover" else ["on_slot"],
        )
    return steps


def build_clean(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    steps: list[StepTemplate] = []
    per_loc: dict[str, list[str]] = {}
    for i, entry in enumerate(params.get("trash", []), 1):
        sku, loc = entry["sku"], entry["location"]
        name = rec.catalog[sku].name if sku in rec.catalog else sku
        chain = item_chain(
            f"t{i}",
            sku=sku,
            name=name,
            source=loc,
            candidates=None,
            destination="bin",
            final_skill="place",
            final_args={"sku": sku, "location": "bin"},
            final_criteria=["in_bin"],
        )
        steps += chain
        per_loc.setdefault(loc, []).append(chain[-1].step_id)
    for j, loc in enumerate(params["locations"], 1):
        deps = per_loc.get(loc, [])
        steps.append(StepTemplate(f"w{j}-go", "navigate_to", {"location": loc}, f"Go to {loc}", depends_on=list(deps),
                                  requires=["mobile"], group=f"w{j}"))
        steps.append(StepTemplate(f"w{j}-wipe", "wipe", {"location": loc}, f"Wipe {loc}", depends_on=[f"w{j}-go"], group=f"w{j}"))
    return steps


def _inspect_shelf(harness: "Harness", domain: Domain, args: dict[str, Any]) -> dict[str, Any]:
    """Composed command: navigate_to -> observe -> compare with the planogram."""
    loc = args["location"]
    robot_id = args.get("robot_id") or harness.pick_idle_robot("observe", location=loc)
    fb = harness.execute(ExecutionRequest(robot_id=robot_id, skill="navigate_to", args={"location": loc},
                                          guidance=Guidance(instruction=f"go to {loc} to inspect")))
    if not fb.success:
        return {"location": loc, "robot_id": robot_id, "error": fb.failure_reason.to_dict() if fb.failure_reason else "navigation failed"}
    obs = harness.observe(robot_id, camera=args.get("camera", "head"))
    seen = {d.ref_id for d in obs.detections}
    missing = []
    for slot, sku in domain.records.planogram(loc):
        if sku not in seen:
            item = domain.records.catalog[sku]
            missing.append({"sku": sku, "name": item.name, "slot": slot, "mount": item.mount})
    return {"location": loc, "robot_id": robot_id, "detections": len(obs.detections), "missing": missing,
            "note": "Detections can miss occluded items; confirm close-range before restocking if unsure."}


def make_store_domain(records: BusinessRecords) -> Domain:
    d = Domain(
        name="store",
        description="Convenience store: shelves with a planogram, a hook wall, back-room storage, checkout counter, eat-in tables.",
        records=records,
        vocabulary=["planogram", "SKU", "hook wall", "facing", "back room", "checkout counter"],
        safety=list(SAFETY),
    )
    d.workflows["restock"] = Workflow("restock", "Move items from stock to their planogram slot or hook.", _ITEMS_SCHEMA, build_restock)
    d.workflows["retrieve"] = Workflow(
        "retrieve",
        "Fetch items for a customer and hand them over at a location.",
        {**_ITEMS_SCHEMA, "properties": {**_ITEMS_SCHEMA["properties"], "deliver_to": {"type": "string"},
                                          "deliver_by": {"type": "string", "enum": ["handover", "place"]}}},
        build_retrieve,
    )
    d.workflows["clean"] = Workflow(
        "clean",
        "Bin listed trash and wipe tables.",
        {
            "type": "object",
            "properties": {
                "locations": {"type": "array", "items": {"type": "string"}},
                "trash": {"type": "array", "items": {"type": "object", "properties": {"sku": {"type": "string"}, "location": {"type": "string"}},
                                                     "required": ["sku", "location"]}},
            },
            "required": ["locations"],
        },
        build_clean,
    )
    d.tools["lookup"] = lookup_tool("store")
    d.tools["orders"] = orders_tool()
    d.tools["inspect_shelf"] = ComposedTool(
        "inspect_shelf",
        "Send a robot to a shelf or hook wall, observe it and list planogram slots whose item was not detected.",
        {"type": "object", "properties": {"location": {"type": "string"}, "robot_id": {"type": "string"}, "camera": {"type": "string"}},
         "required": ["location"]},
        _inspect_shelf,
    )
    return d
