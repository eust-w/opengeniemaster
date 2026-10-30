"""Home / hospitality domain: serving guests and setting the table."""

from __future__ import annotations

from typing import Any

from ..eep.types import StepTemplate
from .base import Domain, Workflow, item_chain, lookup_tool, orders_tool
from .records import BusinessRecords

SAFETY = [
    "Guests move around: pause when a person or hand enters the workspace, then recheck.",
    "Hand objects to guests only when they are reaching for them.",
]


def build_serve(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    steps: list[StepTemplate] = []
    for i, order in enumerate(params["orders"], 1):
        sku = order["sku"]
        item = rec.item(sku)
        steps += item_chain(
            f"g{i}",
            sku=sku,
            name=item.name,
            source=item.home,
            candidates=item.likely_locations or None,
            destination=order["location"],
            final_skill="handover",
            final_args={"sku": sku, "location": order["location"], "recipient": order.get("guest")},
            final_criteria=["delivered"],
        )
    return steps


def build_spread(params: dict[str, Any], rec: BusinessRecords) -> list[StepTemplate]:
    cloth = params.get("cloth", "tablecloth")
    table = params.get("table", "dining_table")
    item = rec.item(cloth)
    return [
        StepTemplate("c-go-src", "navigate_to", {"location": item.home}, f"Go to {item.home}", requires=["mobile"], group="c"),
        StepTemplate("c-pick", "pick", {"sku": cloth, "location": item.home}, f"Take the {item.name}", depends_on=["c-go-src"], group="c"),
        StepTemplate("c-go-dst", "navigate_to", {"location": table}, f"Carry it to {table}", depends_on=["c-pick"], requires=["mobile"],
                     group="c"),
        StepTemplate("c-spread", "spread", {"location": table, "sku": cloth}, f"Spread the {item.name} over {table}",
                     depends_on=["c-go-dst"], requires=["dual_arm"], criteria=["cloth_coverage"], group="c"),
    ]


def make_home_domain(records: BusinessRecords) -> Domain:
    d = Domain(
        name="home",
        description="Home with a pantry, fridge, kitchen counter, sofa, dining table and a linen cabinet.",
        records=records,
        vocabulary=["guest", "pantry", "dining table", "tablecloth"],
        safety=list(SAFETY),
    )
    d.workflows["serve_guests"] = Workflow(
        "serve_guests",
        "Bring each guest the drink or snack they asked for.",
        {"type": "object",
         "properties": {"orders": {"type": "array", "items": {"type": "object", "properties": {
             "sku": {"type": "string"}, "location": {"type": "string"}, "guest": {"type": "string"}}, "required": ["sku", "location"]}}},
         "required": ["orders"]},
        build_serve,
    )
    d.workflows["spread_tablecloth"] = Workflow(
        "spread_tablecloth",
        "Fetch the tablecloth and spread it over a table with both arms.",
        {"type": "object", "properties": {"cloth": {"type": "string"}, "table": {"type": "string"}}},
        build_spread,
    )
    d.tools["lookup"] = lookup_tool("home")
    d.tools["orders"] = orders_tool()
    return d
