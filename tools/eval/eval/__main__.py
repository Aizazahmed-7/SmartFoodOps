"""`make eval`.

The body sits behind the `__main__` guard so importing this module is
harmless — which is what lets the coverage gate measure the package at all
without a `python -m` run.
"""

from .report import main

if __name__ == "__main__":  # pragma: no cover — the CLI shell
    raise SystemExit(main())
