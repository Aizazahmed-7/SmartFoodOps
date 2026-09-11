"""The grep-ban that replaced the circular DEFERRABLE FK (review 2026-09-10):
`restaurants.insert()` may exist ONLY in adapters/repo.py, because a branch
row and its branch_metadata row must be written together and that is the one
place doing it. "Every branch has a metadata row" has no declarative form in
SQL, so single-writer discipline is the enforcement — the same trade the
orders.status writer makes (services/order/tests/test_no_raw_status_updates).
"""

import pathlib


def test_only_the_repo_inserts_restaurants():
    package = pathlib.Path(__file__).parent.parent / "catalog"
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path.name != "repo.py" and "restaurants.insert(" in path.read_text()
    ]
    assert offenders == [], f"raw restaurants.insert outside the repo: {offenders}"
