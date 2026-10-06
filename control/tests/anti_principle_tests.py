"""Principle 13 assertion ratchet; negative assertions remain principle-positive."""

from .principle_guards import main, scan_sites, scan_source

__all__ = ["scan_sites", "scan_source"]

if __name__ == "__main__":
    raise SystemExit(main("tests"))
