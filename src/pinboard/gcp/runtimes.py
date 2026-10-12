"""List and delete Agent Platform runtimes (the instances sandboxes attach to)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from pinboard.gcp.sandbox import RUNTIME_DISPLAY_NAME


@dataclass
class RuntimeInfo:
    name: str
    display_name: str
    created: str
    ours: bool
    in_use: bool


def list_runtimes(client, in_use: str | None = None) -> list[RuntimeInfo]:
    out = []
    for rt in client.runtimes.list():
        res = getattr(rt, "api_resource", rt)
        labels = getattr(res, "labels", None) or {}
        display = getattr(res, "display_name", "") or ""
        out.append(
            RuntimeInfo(
                name=res.name,
                display_name=display,
                created=str(getattr(res, "create_time", "") or ""),
                ours=labels.get("created-by") == "pinboard-agent" or display == RUNTIME_DISPLAY_NAME,
                in_use=bool(in_use) and res.name == in_use,
            )
        )
    return out


def delete_runtimes(client, names: list[str], in_use: str | None, confirm: Callable[[RuntimeInfo], bool]) -> dict[str, str]:
    """Delete exactly the named runtimes (and their sandboxes/templates). Never touches the in-use one."""
    known = {r.name: r for r in list_runtimes(client, in_use)}
    report: dict[str, str] = {}
    for name in names:
        info = known.get(name)
        if info is None:
            report[name] = "skipped (not found in this project/location)"
        elif info.in_use:
            report[name] = "skipped (this is SANDBOX_RUNTIME, the one in use)"
        elif not confirm(info):
            report[name] = "skipped (not confirmed)"
        else:
            client.runtimes.delete(name=name, force=True)
            report[name] = "deleted"
    return report
