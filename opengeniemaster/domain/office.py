"""Office domain: searching for supplies and personal items."""

from __future__ import annotations

from typing import Any

from ..eep.types import StepTemplate
from .base import Domain, Workflow, item_chain, lookup_tool, orders_tool
from .records import BusinessRecords

SAFETY = [
    "Pause when a person or hand enters the workspace, then recheck before resuming.",
    "Do not take items from a desk unless they match the request.",
]


def build_fetch(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    deliver = params.get("deliver_to", "user_desk")
    all_places = sorted(p for p, info in rec.places.items() if info.kind not in ("dock",))
    steps: list[StepTemplate] = []
    for i, sku in enumerate(params["items"], 1):
        item = rec.item(sku)
        likely = list(item.likely_locations)
        candidates = likely + [p for p in all_places if p not in likely and p != deliver]
        steps += item_chain(
            f"f{i}",
            sku=sku,
            name=item.name,
            source=None,
            candidates=candidates,
            destination=deliver,
            final_skill="place",
            final_args={"sku": sku, "location": deliver},
            final_criteria=["on_slot"],
        )
    return steps


def make_office_domain(records: BusinessRecords) -> Domain:
    d = Domain(
        name="office",
        description="Open-plan office: desks, a pantry bar counter, a supply cabinet and a meeting table.",
        records=records,
        vocabulary=["desk", "bar counter", "supply cabinet", "meeting table"],
        safety=list(SAFETY),
    )
    d.workflows["fetch_items"] = Workflow(
        "fetch_items",
        "Find each requested item (records may be stale) and bring it to a desk.",
        {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}, "deliver_to": {"type": "string"}},
         "required": ["items"]},
        build_fetch,
    )
    d.tools["lookup"] = lookup_tool("office")
    d.tools["orders"] = orders_tool()
    return d
