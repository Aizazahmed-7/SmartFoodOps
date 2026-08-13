"""The CI grep-ban (ARCHITECTURE §6.2): orders.update() may exist ONLY in
domain/transitions.py — every other status write is a review reject."""

import pathlib


def test_only_transitions_module_updates_orders():
    package = pathlib.Path(__file__).parent.parent / "order"
    offenders = []
    for path in package.rglob("*.py"):
        if path.name == "transitions.py":
            continue
        if "orders.update(" in path.read_text():
            offenders.append(str(path.relative_to(package)))
    assert offenders == [], f"raw orders.update outside transitions.py: {offenders}"
