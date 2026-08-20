"""Conditional Preview Adapter: a plain lookup-and-merge against
data/conditional_rules.json, with no LLM call and no write to disk. Completely
separate from the six admin-chat tools in tools.py -- this only ever reads
screen_N.json and conditional_rules.json, and returns a preview copy of a
screen's fields.

Live trigger state
-------------------
The UI calls apply_conditional_rules (via POST /preview/field-change) every
time the customer changes a dropdown/radio value -- that call is the only
signal this app ever gets of what the customer actually picked; there's no
separate "submit an answer" endpoint. So apply_conditional_rules records each
(session_id, screen_id, path) -> value it's given in `_current_trigger_values`,
and resolve_screen_fields (used by GET /admin/screens/{screen_id}) reads from
that same in-memory store first -- falling back to the field's on-disk default
only for a trigger the UI hasn't reported a change for yet. This is what lets
a screen_2 selection affect screen_3's GET response with no extra endpoint
and no admin-chat involvement.

After every change, values belonging to fields that are now hidden or removed
are discarded, and visibility is recalculated until stable. That generic rule
prevents stale derived state from keeping downstream fields visible (for
example, a previous credit score after Credit Check Required changes to No).
"""

import copy
import json
import random
from typing import Any

from deepagents.backends import FilesystemBackend

from logging_setup import logger

RULES_PATH = "/conditional_rules.json"
CREDIT_SCORE_MOCK_CONFIG_PATH = "/credit_score_mock.json"

# The one hardcoded trigger for this POC's credit-check demo: when the
# customer answers Yes to Credit Check Required on screen_2, stand in for
# the real credit-score API (n8n's job in production) and record a score
# back in the same request -- so the low/high-score branch shows up
# immediately instead of needing a second POST to simulate n8n.
CREDIT_CHECK_SCREEN_ID = "screen_2"
CREDIT_CHECK_TRIGGER_PATH = "serviceDetails.creditCheckRequired"
CREDIT_CHECK_TRIGGER_VALUE = "YES"
CREDIT_SCORE_PATH = "serviceDetails.creditScore"

# (session_id, screen_id, path) -> the last value reported or derived for that
# customer journey. Still in-memory only for the POC, but isolated so one
# customer's conditional choices cannot affect another customer's response.
_current_trigger_values: dict[tuple[str, str, str], str] = {}

DEFAULT_PREVIEW_SESSION = "__default_preview_session__"


def _session_key(session_id: str | None) -> str:
    return session_id or DEFAULT_PREVIEW_SESSION


def record_field_change(
    screen_id: str, path: str, value: str, session_id: str | None = None
) -> None:
    _current_trigger_values[(_session_key(session_id), screen_id, path)] = value


def clear_recorded_values(screen_id: str, session_id: str | None = None) -> None:
    """Forget any recorded live value originating from this screen. Used
    when a screen is reset back to its seed, so a stale customer selection
    can't keep overriding the freshly-reset default. With no session_id,
    clears the screen across every session; otherwise clears just that journey.
    """
    session_key = _session_key(session_id) if session_id is not None else None
    for key in [
        k
        for k in _current_trigger_values
        if k[1] == screen_id and (session_key is None or k[0] == session_key)
    ]:
        _current_trigger_values.pop(key, None)


def _read_json(backend: FilesystemBackend, path: str) -> Any:
    result = backend.read(path)
    if result.error:
        raise ValueError(f"Could not read {path}: {result.error}")
    return json.loads(result.file_data["content"])


def _find_field(fields: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next((f for f in fields if f.get("path") == path), None)


def _target_screen_id(rule: dict[str, Any]) -> str:
    """The screen a rule's changes[] apply to -- its own screenId (the
    trigger's screen) unless targetScreenId says otherwise, e.g. a
    screen_2 connectionType trigger whose changes affect screen_3.
    """
    return rule.get("targetScreenId", rule["screenId"])


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


def _evaluate(
    cond: dict[str, Any], default_screen_id: str, session_id: str | None = None
) -> bool:
    """True if this showWhen condition currently holds.

    Looks up `_current_trigger_values` directly -- NOT via the on-disk-default
    fallback that rule triggers use (see resolve_screen_fields's
    _current_trigger_value). An unanswered trigger must mean hidden: a blank
    creditScore must not accidentally satisfy `lt "500"` just because that's
    the field's stored default.
    """
    screen_id = cond.get("screenId", default_screen_id)
    actual = _current_trigger_values.get(
        (_session_key(session_id), screen_id, cond["path"])
    )
    if actual is None:
        return False

    op = cond["op"]
    if op == "eq":
        return actual == cond["value"]
    if op == "ne":
        return actual != cond["value"]
    if op in ("lt", "gte"):
        try:
            a, b = float(actual), float(cond["value"])
        except (TypeError, ValueError):
            return False
        return a < b if op == "lt" else a >= b
    return True


def filter_visible(
    fields: list[dict[str, Any]], screen_id: str, session_id: str | None = None
) -> list[dict[str, Any]]:
    """Drop every field whose showWhen does not currently hold."""
    return [
        f
        for f in fields
        if "showWhen" not in f or _evaluate(f["showWhen"], screen_id, session_id)
    ]


def _mock_credit_score(backend: FilesystemBackend, user_id: str | None) -> str:
    """Stand-in for the real credit-score API.

    A user-specific fixed score in credit_score_mock.json wins when present;
    otherwise the existing min/max range supplies a backwards-compatible
    default for callers that omit userID or send an unconfigured id.
    """
    config = _read_json(backend, CREDIT_SCORE_MOCK_CONFIG_PATH)
    configured_score = config.get("user_scores", {}).get(user_id)
    if configured_score is not None:
        return str(configured_score)
    return str(random.randint(config["min_score"], config["max_score"]))


def _maybe_run_mock_credit_check(
    backend: FilesystemBackend,
    screen_id: str,
    path: str,
    value: str,
    session_id: str | None,
    user_id: str | None,
) -> None:
    if screen_id != CREDIT_CHECK_SCREEN_ID or path != CREDIT_CHECK_TRIGGER_PATH or value != CREDIT_CHECK_TRIGGER_VALUE:
        return
    score = _mock_credit_score(backend, user_id)
    record_field_change(CREDIT_CHECK_SCREEN_ID, CREDIT_SCORE_PATH, score, session_id)
    logger.info(
        "MOCK_CREDIT_CHECK session=%s user_id=%s screen_id=%s score=%s",
        _session_key(session_id),
        user_id,
        CREDIT_CHECK_SCREEN_ID,
        score,
    )


def _known_screen_ids(backend: FilesystemBackend) -> list[str]:
    result = backend.glob("screen_*.json", "/")
    if result.error or not result.matches:
        return []
    return [m["path"].strip("/").removesuffix(".json") for m in result.matches]


def _discard_hidden_values(
    backend: FilesystemBackend, session_id: str | None
) -> None:
    """Remove recorded values for fields that are hidden or removed.

    Repeat until stable because clearing one hidden value can make fields
    farther down the dependency chain hidden too. This is intentionally
    generic: it follows each field's showWhen metadata instead of naming any
    particular business field or journey.
    """
    session_key = _session_key(session_id)

    while True:
        removed_any = False
        for screen_id in _known_screen_ids(backend):
            fields, _ = resolve_screen_fields(backend, screen_id, session_id)
            fields_by_path = {f.get("path"): f for f in fields}
            state_keys = [
                key
                for key in _current_trigger_values
                if key[0] == session_key and key[1] == screen_id
            ]

            for key in state_keys:
                field = fields_by_path.get(key[2])
                is_hidden = field is None or (
                    "showWhen" in field
                    and not _evaluate(field["showWhen"], screen_id, session_id)
                )
                if is_hidden:
                    _current_trigger_values.pop(key, None)
                    removed_any = True

        if not removed_any:
            return


def apply_conditional_rules(
    backend: FilesystemBackend,
    screen_id: str,
    path: str,
    value: str,
    session_id: str | None = None,
    user_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Look up a matching conditional rule for (screen_id, path, value) and
    return a preview copy of that screen's fields with the rule's changes
    applied (or the fields unchanged if nothing matches). The field at `path`
    itself has its `value` set to the submitted `value` in the response, so
    the one field the caller just answered reflects their own answer rather
    than its on-disk seed default.

    Read-only: never writes screen_id's file, and the returned fields are a
    deep copy so the caller can't accidentally mutate the on-disk screen. As
    a side effect, records (screen_id, path) -> value as the live trigger
    state other screens' resolve_screen_fields calls will read. If this
    particular change is "Credit Check Required = Yes", also runs the mock
    credit-score check and records its result the same way, in the same
    call -- see _maybe_run_mock_credit_check.
    """
    record_field_change(screen_id, path, value, session_id)
    _maybe_run_mock_credit_check(
        backend, screen_id, path, value, session_id, user_id
    )
    _discard_hidden_values(backend, session_id)

    rules = _read_json(backend, RULES_PATH)

    rule = next(
        (
            r
            for r in rules
            if r["screenId"] == screen_id
            and _target_screen_id(r) == screen_id
            and r["trigger"]["path"] == path
            and r["trigger"]["value"] == value
        ),
        None,
    )

    fields, _ = resolve_screen_fields(backend, screen_id, session_id)
    return filter_visible(fields, screen_id, session_id), rule["ruleId"] if rule else None


def resolve_screen_fields(
    backend: FilesystemBackend, screen_id: str, session_id: str | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fields for screen_id with every currently-matching conditional rule
    baked in -- same-screen or cross-screen -- evaluated by reading each
    rule's trigger value fresh from its own screen's file on disk.

    Read-only, and used for GET-time display (e.g. the onboarding UI landing
    on screen_3 after a connectionType choice reported via a prior
    POST /preview/field-change on screen_2), as opposed to
    apply_conditional_rules which previews a single hypothetical field change.

    A trigger's current value comes from the live `_current_trigger_values`
    store (whatever the UI last reported for that field) if present, falling
    back to that field's on-disk default for a trigger the UI hasn't reported
    a change for yet.
    """
    screen_fields_cache: dict[str, list[dict[str, Any]]] = {}

    def _fields_of(sid: str) -> list[dict[str, Any]]:
        if sid not in screen_fields_cache:
            screen_fields_cache[sid] = _read_json(backend, f"/{sid}.json")[0]["fields"]
        return screen_fields_cache[sid]

    def _current_trigger_value(sid: str, path: str) -> str | None:
        state_key = (_session_key(session_id), sid, path)
        if state_key in _current_trigger_values:
            return _current_trigger_values[state_key]
        field = _find_field(_fields_of(sid), path)
        return field.get("value") if field else None

    fields = copy.deepcopy(_fields_of(screen_id))
    rules = _read_json(backend, RULES_PATH)
    matched_rule_ids = []

    for rule in rules:
        if _target_screen_id(rule) != screen_id:
            continue
        if _current_trigger_value(rule["screenId"], rule["trigger"]["path"]) != rule["trigger"]["value"]:
            continue
        for change in rule["changes"]:
            _apply_change(fields, change)
        matched_rule_ids.append(rule["ruleId"])

    # Echo every answer recorded for this session onto the returned screen so
    # POST and subsequent GET responses remain self-consistent.
    session_key = _session_key(session_id)
    for field in fields:
        state_key = (session_key, screen_id, field.get("path"))
        if state_key in _current_trigger_values:
            field["value"] = _current_trigger_values[state_key]

    return fields, matched_rule_ids
