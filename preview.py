"""Conditional Preview Adapter: a plain lookup-and-merge against
data/conditional_rules.json, with no LLM call and no write to disk. Completely
separate from the six admin-chat tools in tools.py -- this only ever reads
screen_N.json and conditional_rules.json, and returns a preview copy of a
screen's fields.
"""

import copy
import json
from typing import Any

from deepagents.backends import FilesystemBackend

RULES_PATH = "/conditional_rules.json"


def _read_json(backend: FilesystemBackend, path: str) -> Any:
    result = backend.read(path)
    if result.error:
        raise ValueError(f"Could not read {path}: {result.error}")
    return json.loads(result.file_data["content"])


def _find_field(fields: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next((f for f in fields if f.get("path") == path), None)


def _apply_change(fields: list[dict[str, Any]], change: dict[str, Any]) -> None:
    op = change["op"]
    if op == "add_field":
        fields.append(copy.deepcopy(change["field"]))
    elif op == "remove_field":
        field = _find_field(fields, change["path"])
        if field is not None:
            fields.remove(field)
    elif op == "update_field":
        field = _find_field(fields, change["path"])
        if field is not None:
            field.update(change["set"])


def apply_conditional_rules(
    backend: FilesystemBackend, screen_id: str, path: str, value: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Look up a matching conditional rule for (screen_id, path, value) and
    return a preview copy of that screen's fields with the rule's changes
    applied (or the fields unchanged if nothing matches).

    Read-only: never writes screen_id's file, and the returned fields are a
    deep copy so the caller can't accidentally mutate the on-disk screen.
    """
    screen = _read_json(backend, f"/{screen_id}.json")[0]
    rules = _read_json(backend, RULES_PATH)

    rule = next(
        (
            r
            for r in rules
            if r["screenId"] == screen_id
            and r["trigger"]["path"] == path
            and r["trigger"]["value"] == value
        ),
        None,
    )

    fields = copy.deepcopy(screen["fields"])
    if rule is None:
        return fields, None

    for change in rule["changes"]:
        _apply_change(fields, change)

    return fields, rule["ruleId"]
