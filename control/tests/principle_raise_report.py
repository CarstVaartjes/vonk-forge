"""Report builtin/HTTP raise sites outside the existing blocker classifier.

This inventory is intentionally report-only: each owner must classify security,
contract and bookkeeping raises before an enforcement baseline can be honest.
"""

from .principle_guards import scan_sites


def main() -> int:
    sites = scan_sites("raises")
    for site in sites:
        print(f"{site.path}:{site.line}: {site.function}: {site.kind}")
    print(f"builtin/http raises: {len(sites)} (report-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
