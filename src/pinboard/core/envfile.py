"""Small helper to write a non-secret value into .env."""
from __future__ import annotations

import re
from pathlib import Path


def persist_env_value(path: str | Path, key: str, value: str) -> bool:
    """Set KEY=value in the env file if KEY is absent or empty. Returns True if the file changed."""
    p = Path(path)
    if not p.exists():
        return False
    lines = p.read_text().splitlines()
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=(.*)$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            if m.group(1).strip().strip("\"'"):
                return False  # already has a value: never overwrite
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + "\n")
    return True
