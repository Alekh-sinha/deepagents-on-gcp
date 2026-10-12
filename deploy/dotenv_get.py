#!/usr/bin/env python3
"""Print one value from .env to stdout (no newline), so it can be piped into `gcloud secrets versions add --data-file=-`
without ever appearing in a command line. Exits 1 (printing nothing) if it is missing or still a placeholder."""
import sys
from pathlib import Path

from dotenv import dotenv_values

from pinboard.gcp.vault import is_placeholder


def main() -> int:
    name = sys.argv[1]
    path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".env")
    value = dotenv_values(path).get(name)
    if is_placeholder(value) or str(value).startswith("secret://"):
        print(f"{name} is not set to a real value in {path}", file=sys.stderr)
        return 1
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
