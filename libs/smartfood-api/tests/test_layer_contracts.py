"""The three layer contracts (repo-structure.md §2), enforced as a source scan
in the house grep-ban idiom (cf. order's test_no_raw_status_updates):

1. services never import each other — cross-service interaction is HTTP,
   Temporal, or Kafka only;
2. domain/ never imports fastapi — domain code stays framework-free;
3. api/ never imports adapters (relative or absolute) — routes go through
   the domain service, never straight to repos/session code.

Lives in smartfood-api's tests because root pytest discovery (testpaths =
libs/services/tools) does not pick up a top-level tests/ directory.
"""

import pathlib
import re

SERVICES = pathlib.Path(__file__).resolve().parents[3] / "services"
SERVICE_PACKAGES = {"identity", "catalog", "inventory", "order", "payment", "edge_bff"}

# One dotted target per `import x.y` / `from x.y import z` line (first name only
# for comma imports — lint-grade, like the rest of the grep-ban suite).
IMPORT_TARGET = re.compile(r"^\s*(?:from\s+([.\w]+)\s+import\s|import\s+([.\w]+))", re.MULTILINE)


def service_imports():  # (service package name, source path, dotted import target)
    for svc_dir in sorted(SERVICES.iterdir()):
        package = svc_dir / svc_dir.name.replace("-", "_")
        if not package.is_dir():
            continue
        for path in sorted(package.rglob("*.py")):
            for match in IMPORT_TARGET.finditer(path.read_text()):
                yield package.name, path, match.group(1) or match.group(2)


def test_services_never_import_each_other():
    offenders = []
    for own, path, target in service_imports():
        root = target.lstrip(".").split(".")[0]
        if not target.startswith(".") and root in SERVICE_PACKAGES and root != own:
            offenders.append(f"{path.relative_to(SERVICES)}: {target}")
    assert offenders == [], f"cross-service imports: {offenders}"


def test_domain_never_imports_fastapi():
    offenders = []
    for _, path, target in service_imports():
        in_domain = "domain" in path.relative_to(SERVICES).parts
        if in_domain and target.split(".")[0] == "fastapi":
            offenders.append(f"{path.relative_to(SERVICES)}: {target}")
    assert offenders == [], f"fastapi imported from domain/: {offenders}"


def test_api_never_imports_adapters():
    offenders = []
    for _, path, target in service_imports():
        in_api = "api" in path.relative_to(SERVICES).parts
        if in_api and "adapters" in target.lstrip(".").split("."):
            offenders.append(f"{path.relative_to(SERVICES)}: {target}")
    assert offenders == [], f"api/ reaching past domain into adapters: {offenders}"
