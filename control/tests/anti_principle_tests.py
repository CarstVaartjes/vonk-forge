"""Added-line tests guard; patch supplied by the workflow."""

from .added_line_guards import main

if __name__ == "__main__":
    raise SystemExit(main(modes=("tests",)))
