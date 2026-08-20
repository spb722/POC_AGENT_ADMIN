"""The six admin-editing tools, as plain Python functions wrapped with @tool.

Each tool that needs to know "which conversation is this" (to key the
in-memory draft) takes a `config: RunnableConfig` parameter -- LangChain
auto-injects this and hides it from the LLM-visible tool schema, so the model
never has to pass a thread_id itself. We use the LangGraph thread_id (== the
admin's session_id, see agent.py/main.py) as the key.

In-memory draft model
----------------------
`apply_field_edit` never writes to disk; it mutates a per-(thread, screen)
draft copy of the `fields` array held in the module-level `_drafts` dict.
`load_screen_fields` is the only thing that (re)seeds a screen's draft from
disk -- calling it again mid-conversation intentionally discards any
uncommitted draft for that screen, which is what "classify a brand new
request" should do. The human-in-the-loop "edit/feedback" path in agent.py
deliberately skips load_screen_fields so the draft (and its accumulated diff)
survives across a refinement round.
"""

import copy
import json
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from deepagents.backends import FilesystemBackend

from models import FieldCandidate, FieldEdit, FieldMatch, ScreenClassification, ShowWhen

# (thread_id, screen_id) -> mutable draft of the `fields` array
_drafts: dict[tuple[str, str], list[dict[str, Any]]] = {}
# thread_id -> accumulated diffs (each entry carries its own screen_id)
_diffs: dict[str, list[dict[str, Any]]] = {}


def _thread_id(config: RunnableConfig) -> str:
    return config["configurable"]["thread_id"]


def _screen_path(screen_id: str) -> str:
    return f"/{screen_id}.json"


def _read_screen(backend: FilesystemBackend, screen_id: str) -> dict[str, Any]:
    result = backend.read(_screen_path(screen_id))
    if result.error:
        raise ValueError(f"Could not read {screen_id}: {result.error}")
    payload = json.loads(result.file_data["content"])
    # Fixed contract: file is a one-element JSON array wrapping the screen object.
    return payload[0]


def list_known_screens(backend: FilesystemBackend) -> list[str]:
    result = backend.glob("screen_*.json", "/")
    if result.error or not result.matches:
        return []
    return [m["path"].strip("/").removesuffix(".json") for m in result.matches]


def reset_screen_state(thread_id: str, screen_id: str) -> None:
    _drafts.pop((thread_id, screen_id), None)
    _diffs[thread_id] = [d for d in _diffs.get(thread_id, []) if d["screen_id"] != screen_id]


def get_pending_diffs(thread_id: str) -> list[dict[str, Any]]:
    return list(_diffs.get(thread_id, []))


def get_draft_fields(thread_id: str, screen_id: str) -> list[dict[str, Any]] | None:
    """The staged (not yet written) fields array for a screen, or None if
    that session has no draft for it (nothing staged, already written, or
    already discarded). Used to preview a pending edit before confirming.
    """
    draft = _drafts.get((thread_id, screen_id))
    return copy.deepcopy(draft) if draft is not None else None


def clear_thread_state(thread_id: str) -> None:
    _diffs.pop(thread_id, None)
    for key in [k for k in _drafts if k[0] == thread_id]:
        _drafts.pop(key, None)


def reset_drafts_for_screen(screen_id: str) -> None:
    """Clear any in-memory draft/diff state referencing this screen, across
    every session. Used after resetting a screen's file on disk, so a stale
    preview (see get_draft_fields) can't show edits drafted against fields
    that no longer match the reset baseline.
    """
    for key in [k for k in _drafts if k[1] == screen_id]:
        _drafts.pop(key, None)
    for thread_id in list(_diffs):
        _diffs[thread_id] = [d for d in _diffs[thread_id] if d["screen_id"] != screen_id]


def _find_field(fields: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    return next((f for f in fields if f.get("path") == path), None)


def _find_option(field: dict[str, Any], option_value: str) -> dict[str, Any] | None:
    return next((o for o in field.get("values", []) or [] if o.get("value") == option_value), None)


def _show_when_json(sw: ShowWhen) -> dict[str, Any]:
    out: dict[str, Any] = {"path": sw.path, "op": sw.op, "value": sw.value}
    if sw.screen_id:
        out["screenId"] = sw.screen_id
    return out


def _screen_summary(backend: FilesystemBackend, screen_id: str) -> str:
    """One line describing what's actually on a screen, built from its live
    fieldLabels -- not a hardcoded description, so it can never drift out of
    sync with the real JSON (which admins can change via this very tool).
    Lists every field, not a truncated sample: a field left out of the
    summary is invisible to classify_screen, which then wrongly reports it
    as not present on any screen instead of matching it.
    """
    try:
        fields = _read_screen(backend, screen_id)["fields"]
    except ValueError:
        return screen_id
    labels = [f.get("fieldLabel", "") for f in fields]
    return f"{screen_id}: {', '.join(labels)}"


# Which of FieldEdit's optional attributes each op is actually allowed to use.
# Anything set outside this list is a sign the model is trying to change
# something this tool has no support for (e.g. `required`, `controlType`,
# `dataType` on an EXISTING field) -- silently ignoring it would let the
# agent report a false "no change needed" or a no-op success instead of
# telling the admin the edit isn't possible. See _unsupported_edit_fields.
_ALLOWED_EDIT_FIELDS: dict[str, set[str]] = {
    "add_field": {"field_label", "control_type", "data_type", "required", "default_value", "origin", "show_when", "options"},
    "delete_field": set(),
    "rename_field": {"field_label"},
    "add_option": {"option_value", "option_label"},
    "rename_option": {"option_value", "option_label", "new_option_value"},
    "remove_option": {"option_value"},
    "set_default_value": {"default_value"},
    "set_show_when": {"show_when"},
    "clear_show_when": set(),
}
_EDIT_ATTR_NAMES = [
    "field_label", "control_type", "data_type", "required", "default_value",
    "option_value", "option_label", "new_option_value", "origin", "show_when", "options",
]


def _unsupported_edit_fields(edit: FieldEdit) -> list[str]:
    allowed = _ALLOWED_EDIT_FIELDS.get(edit.op, set())
    return [name for name in _EDIT_ATTR_NAMES if name not in allowed and getattr(edit, name) is not None]


def build_tools(backend: FilesystemBackend, model) -> list:
    """Build the six agent tools, closing over `backend` (real disk I/O) and
    `model` (used only for the two structured-output classification calls).
    """

    @tool
    def classify_screen(message: str, config: RunnableConfig) -> str:
        """Given the admin's raw message, decide which known screen file(s) it targets.

        A single message can span multiple screens (e.g. "rename Postal Code to
        Zip Code on screens 1 and 2"). Always call this before touching any
        specific screen's fields.
        """
        screens = list_known_screens(backend)
        screen_summaries = [_screen_summary(backend, s) for s in screens]
        prompt = (
            "You are matching an admin's field-edit request to the screen(s) it targets.\n"
            "Known screens (id: every field actually on that screen):\n"
            + "\n".join(f"- {s}" for s in screen_summaries) + "\n"
            f"Admin message: {message!r}\n"
            "Priority order, in this exact order:\n"
            "1. If the admin explicitly names a screen number or id (e.g. 'screen 1', "
            "'screen_2', 'screen 3'), that ALWAYS wins -- use exactly the screen(s) "
            "named, even if the field or topic mentioned sounds like it would fit a "
            "different screen better. An admin adding an unusual field to a screen on "
            "purpose is their call, not something to second-guess.\n"
            "2. Only when NO explicit screen number/id is given anywhere in the message, "
            "match by the field list above -- either a specific field name (e.g. "
            "'service start date' matching a fieldLabel) or the overall topic (e.g. "
            "'the customer identity screen').\n"
            "Return every screen the message plausibly targets. If the message is "
            "generic and could apply to any screen, say so via low confidence."
        )
        result: ScreenClassification = model.with_structured_output(ScreenClassification).invoke(prompt, config=config)
        return json.dumps(result.model_dump())

    @tool
    def load_screen_fields(screen_id: str, config: RunnableConfig) -> str:
        """Load the current `fields` array for one screen straight from disk.

        This (re)seeds that screen's in-memory draft -- call it once per screen
        per *new* admin request, but do not call it again mid-refinement (after
        the admin gives follow-up feedback on a pending diff), or the draft's
        accumulated edits will be discarded.
        """
        thread_id = _thread_id(config)
        try:
            screen = _read_screen(backend, screen_id)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        reset_screen_state(thread_id, screen_id)
        _drafts[(thread_id, screen_id)] = copy.deepcopy(screen["fields"])
        return json.dumps({"screen_id": screen_id, "fields": screen["fields"]})

    @tool
    def classify_field(screen_id: str, message: str, config: RunnableConfig) -> str:
        """Match the admin's message to exactly one field on an already-loaded screen.

        Returns found=False with a `candidates` shortlist (label + controlType)
        when the model isn't confident -- the agent must then stop and ask the
        admin to pick, never guess or proceed on a partial match.
        """
        thread_id = _thread_id(config)
        fields = _drafts.get((thread_id, screen_id))
        if fields is None:
            return json.dumps({"error": f"{screen_id} not loaded yet; call load_screen_fields first"})

        summary = [
            {
                "path": f["path"],
                "fieldLabel": f.get("fieldLabel"),
                "controlType": f.get("controlType"),
                "options": [v.get("label") for v in f.get("values", []) or []],
            }
            for f in fields
        ]
        prompt = (
            "You are matching an admin's request to exactly one field on a screen.\n"
            f"Fields on {screen_id}: {json.dumps(summary)}\n"
            f"Admin message: {message!r}\n"
            "If the request is about adding a brand-new field (not editing an existing "
            "one), that's still 'not found' here -- set found=false with an empty "
            "candidates list; the agent will use add_field directly.\n"
            "If you cannot identify a single confident match among EXISTING fields, "
            "set found=false and list plausible candidates (path, fieldLabel, controlType). "
            "Never guess."
        )
        result: FieldMatch = model.with_structured_output(FieldMatch).invoke(prompt, config=config)
        return json.dumps(result.model_dump())

    @tool
    def check_noop(screen_id: str, edit: FieldEdit, config: RunnableConfig) -> str:
        """Check whether applying `edit` would leave the field in its current state.

        A direct comparison, no LLM involved. If noop is True, the agent should
        tell the admin no change is needed and skip confirmation for this field
        entirely, rather than showing a pointless diff.
        """
        unsupported = _unsupported_edit_fields(edit)
        if unsupported:
            return json.dumps({
                "error": (
                    f"'{edit.op}' cannot change {', '.join(unsupported)} -- this tool has no "
                    "way to edit that attribute on an existing field. This is not a noop and "
                    "not supported: tell the admin this specific change isn't possible, don't "
                    "report success or 'no change needed'."
                )
            })

        thread_id = _thread_id(config)
        fields = _drafts.get((thread_id, screen_id), [])
        field = _find_field(fields, edit.path)

        if edit.op == "add_field":
            noop = field is not None
            reason = "a field with this path already exists" if noop else "new field, not a noop"
        elif field is None:
            noop = edit.op in ("delete_field", "remove_option")
            reason = "field already absent from the draft" if noop else "field not found in draft"
        elif edit.op == "rename_field":
            noop = field.get("fieldLabel") == edit.field_label
            reason = "fieldLabel already matches" if noop else "fieldLabel differs"
        elif edit.op == "delete_field":
            noop = False
            reason = "field exists and would be removed"
        elif edit.op == "set_default_value":
            noop = field.get("value", "") == (edit.default_value or "")
            reason = "value already matches" if noop else "value differs"
        elif edit.op in ("add_option", "rename_option", "remove_option"):
            existing = _find_option(field, edit.option_value) if edit.option_value else None
            if edit.op == "add_option":
                noop = existing is not None and existing.get("label") == edit.option_label
                reason = "option already present with that label" if noop else "option missing or label differs"
            elif edit.op == "rename_option":
                target_value = edit.new_option_value or edit.option_value
                noop = existing is not None and existing.get("label") == edit.option_label and target_value == edit.option_value
                reason = "option already has that label" if noop else "option label/value differs"
            else:  # remove_option
                noop = existing is None
                reason = "option already absent" if noop else "option exists and would be removed"
        elif edit.op == "set_show_when":
            target = _show_when_json(edit.show_when) if edit.show_when else None
            noop = field.get("showWhen") == target
            reason = "condition already matches" if noop else "condition differs"
        elif edit.op == "clear_show_when":
            noop = "showWhen" not in field
            reason = "field is already unconditional" if noop else "field has a condition"
        else:
            noop = False
            reason = "unrecognized op"

        return json.dumps({"noop": noop, "reason": reason})

    @tool
    def apply_field_edit(screen_id: str, edit: FieldEdit, config: RunnableConfig) -> str:
        """Apply one field edit to the in-memory draft (never touches disk).

        Supports add_field (optionally with its full values[] passed via the
        `options` attribute, for dropdown/radio fields whose options are
        already known), delete_field, rename_field (fieldLabel only -- path
        never changes), set_default_value (the field's stored 'value'),
        add_option/rename_option/remove_option on an existing dropdown/radio
        field's values[], and set_show_when/clear_show_when (a field's
        `showWhen` visibility condition). Returns a before/after diff for
        just the field touched.
        """
        unsupported = _unsupported_edit_fields(edit)
        if unsupported:
            return json.dumps({
                "error": (
                    f"'{edit.op}' cannot change {', '.join(unsupported)} -- this tool has no "
                    "way to edit that attribute on an existing field. Tell the admin this "
                    "specific change isn't possible; do not apply a partial or unrelated edit "
                    "instead."
                )
            })

        thread_id = _thread_id(config)
        key = (thread_id, screen_id)
        if key not in _drafts:
            return json.dumps({"error": f"{screen_id} not loaded yet; call load_screen_fields first"})
        fields = _drafts[key]
        field = _find_field(fields, edit.path)
        before = copy.deepcopy(field) if field else None

        if edit.op == "add_field":
            if field is not None:
                return json.dumps({"error": f"field already exists at path {edit.path}"})
            control_type = edit.control_type or "text"
            new_field: dict[str, Any] = {
                "type": "set",
                "path": edit.path,
                "fieldLabel": edit.field_label or edit.path.rsplit(".", 1)[-1],
                "controlType": control_type,
                "dataType": edit.data_type or ("list" if control_type in ("dropdown", "radio") else "text"),
                "required": bool(edit.required) if edit.required is not None else False,
                "value": edit.default_value or "",
                "origin": edit.origin or "admin_added",
            }
            if control_type in ("dropdown", "radio"):
                new_field["values"] = [{"label": o.label, "value": o.value} for o in edit.options] if edit.options else []
            if edit.show_when is not None:
                new_field["showWhen"] = _show_when_json(edit.show_when)
            fields.append(new_field)
            after: dict[str, Any] | None = new_field

        elif field is None:
            return json.dumps({"error": f"no field at path {edit.path} in current draft"})

        elif edit.op == "delete_field":
            fields.remove(field)
            after = None

        elif edit.op == "rename_field":
            if edit.field_label is None:
                return json.dumps({"error": "rename_field requires field_label"})
            field["fieldLabel"] = edit.field_label
            after = field

        elif edit.op == "set_default_value":
            if edit.default_value is None:
                return json.dumps({"error": "set_default_value requires default_value"})
            field["value"] = edit.default_value
            after = field

        elif edit.op in ("add_option", "rename_option", "remove_option"):
            values = field.setdefault("values", [])
            existing = _find_option(field, edit.option_value) if edit.option_value else None
            if edit.op == "add_option":
                if existing is not None:
                    return json.dumps({"error": f"option {edit.option_value} already exists"})
                values.append({"label": edit.option_label, "value": edit.option_value})
            elif edit.op == "rename_option":
                if existing is None:
                    return json.dumps({"error": f"no option {edit.option_value} to rename"})
                if edit.option_label is not None:
                    existing["label"] = edit.option_label
                if edit.new_option_value is not None:
                    existing["value"] = edit.new_option_value
            else:  # remove_option
                if existing is None:
                    return json.dumps({"error": f"no option {edit.option_value} to remove"})
                values.remove(existing)
            after = field
        elif edit.op == "set_show_when":
            if edit.show_when is None:
                return json.dumps({"error": "set_show_when requires show_when"})
            field["showWhen"] = _show_when_json(edit.show_when)
            after = field
        elif edit.op == "clear_show_when":
            field.pop("showWhen", None)
            after = field
        else:
            return json.dumps({"error": f"unrecognized op {edit.op}"})

        diff = {
            "screen_id": screen_id,
            "op": edit.op,
            "path": edit.path,
            "before": before,
            "after": after,
        }
        _diffs.setdefault(thread_id, []).append(diff)
        return json.dumps({"diff": diff})

    @tool
    def write_screen_json(screen_id: str, config: RunnableConfig) -> str:
        """Persist the current in-memory draft for one screen to screen_N.json on disk.

        This is the only tool that touches disk, and it is gated behind human
        approval -- never call it expecting silent execution; the runtime will
        pause here for the admin's decision.
        """
        thread_id = _thread_id(config)
        key = (thread_id, screen_id)
        if key not in _drafts:
            return json.dumps({"error": f"{screen_id} not loaded yet; nothing to write"})

        screen = _read_screen(backend, screen_id)
        screen["fields"] = _drafts[key]
        result = backend.write(_screen_path(screen_id), json.dumps([screen], indent=2))
        if result.error:
            return json.dumps({"error": result.error})
        return json.dumps({
            "screen_id": screen_id,
            "written_path": result.path,
            "diff": [d for d in _diffs.get(thread_id, []) if d["screen_id"] == screen_id],
        })

    return [
        classify_screen,
        load_screen_fields,
        classify_field,
        check_noop,
        apply_field_edit,
        write_screen_json,
    ]
