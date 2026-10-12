import json
import sys

data = json.load(sys.stdin)
print(f"# {data.get('title', 'Market brief')}\n")
print("## Headlines")
for h in data.get("headlines", []):
    print(f"- {h}")
print("\n## What to watch")
for w in data.get("watch", []):
    print(f"- {w}")
print("\n## Sources")
for s in data.get("sources", []):
    print(f"- {s}")
