#!/usr/bin/env python3
"""Create .env from example.env (never overwrites an existing .env)."""
import shutil
import stat
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
src, dst = root / "example.env", root / ".env"

if dst.exists():
    print(f".env already exists at {dst}. Leaving it alone.")
    sys.exit(0)
if not src.exists():
    sys.exit(f"example.env not found at {src}")

shutil.copyfile(src, dst)
try:
    dst.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600: owner read/write only
except OSError:
    pass  # e.g. Windows
print(f"Created {dst}. Now edit it and fill in your values.")
