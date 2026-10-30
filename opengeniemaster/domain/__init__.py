"""Domain layer: task semantics, workflows, business records and criteria."""

from .base import DEFAULT_CRITERIA, ComposedTool, Domain, Workflow
from .home import make_home_domain
from .office import make_office_domain
from .records import BusinessRecords, CatalogItem, Order, PlaceInfo
from .store import make_store_domain

DOMAIN_FACTORIES = {
    "store": make_store_domain,
    "office": make_office_domain,
    "home": make_home_domain,
}


def make_domain(name: str, records: BusinessRecords) -> Domain:
    try:
        return DOMAIN_FACTORIES[name](records)
    except KeyError:
        raise KeyError(f"unknown domain '{name}'; choose from {sorted(DOMAIN_FACTORIES)}") from None


__all__ = [
    "DEFAULT_CRITERIA",
    "DOMAIN_FACTORIES",
    "BusinessRecords",
    "CatalogItem",
    "ComposedTool",
    "Domain",
    "Order",
    "PlaceInfo",
    "Workflow",
    "make_domain",
    "make_home_domain",
    "make_office_domain",
    "make_store_domain",
]
