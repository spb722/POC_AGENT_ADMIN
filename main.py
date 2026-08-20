"""FastAPI app: one conversational endpoint, one read-only inspection endpoint.

Resume-vs-new-message detection: a session with a pending write_screen_json
interrupt is tracked in `_awaiting_confirmation`. The admin's next message is
classified as approve / reject / respond (see classify_decision) and always
delivered via `Command(resume=...)` -- LangGraph requires resuming an
interrupted thread this way; a plain new-message invoke against an
interrupted thread_id is not supported by the underlying checkpointer/graph.
"""

import json
import os
import re
import shutil
import uuid
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.types import Command

load_dotenv()

from agent import DATA_DIR, build_agent  # noqa: E402  (must run after load_dotenv)
from logging_setup import RunLoggingHandler, configure_logging, logger  # noqa: E402
from models import ChatRequest, ChatResponse, ConditionalPreviewRequest, ConditionalPreviewResponse  # noqa: E402
from preview import apply_conditional_rules, clear_recorded_values, filter_visible, resolve_screen_fields  # noqa: E402
from tools import clear_thread_state, get_draft_fields, get_pending_diffs, reset_drafts_for_screen  # noqa: E402

configure_logging()

app = FastAPI(title="AARYA Admin Field-Editor")

# POC: wide open for any frontend origin to call this during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # must be False when allow_origins is "*"
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pristine copies of the seed screens, kept outside data/ so the agent's
# FilesystemBackend (rooted at data/) never sees or lists them as a screen.
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_data")

_agent = None
_backend = None


def get_agent():
    global _agent, _backend
    if _agent is None:
        _agent, _backend = build_agent()
    return _agent


def get_backend():
    global _agent, _backend
    if _backend is None:
        _agent, _backend = build_agent()
    return _backend


# session_id -> True while a write_screen_json interrupt is pending for it
_awaiting_confirmation: set[str] = set()
# session_id -> how many action_requests are pending in that interrupt batch
_pending_action_count: dict[str, int] = {}

AFFIRMATIVE = {"y", "yes", "yep", "yeah", "yup", "confirm", "confirmed", "approve", "approved", "ok", "okay", "proceed", "sure", "go"}
NEGATIVE = {"n", "no", "nope", "cancel", "cancelled", "reject", "rejected", "stop", "abort", "discard", "don't", "dont"}


def classify_decision(message: str) -> str:
    # Only treat a short reply as a bare approve/reject -- a longer message is
    # feedback ("no, call it something else", "no, make it 600"), not a flat
    # rejection, even though it contains a negative word.
    tokens = re.findall(r"[a-z']+", message.lower())
    if len(tokens) <= 3:
        words = set(tokens)
        if words & NEGATIVE:
            return "reject"
        if words & AFFIRMATIVE:
            return "approve"
    return "respond"


def build_decision(decision_type: str, message: str) -> dict:
    if decision_type == "approve":
        return {"type": "approve"}
    if decision_type == "reject":
        return {"type": "reject", "message": f"Admin declined this write: {message}"}
    return {
        "type": "respond",
        "message": (
            f"The admin did not approve or reject this write -- they gave feedback instead: {message!r}. "
            "Do not treat this as though the write happened. Revise the draft with apply_field_edit based on "
            "this feedback (do not call load_screen_fields again, it would discard the draft), then propose "
            "write_screen_json again."
        ),
    }


def _screen_display(screen_id: str) -> str:
    parts = screen_id.split("_")
    if parts[0] == "screen" and parts[-1].isdigit():
        return f"Screen {parts[-1]}"
    return screen_id


def _strip_for_customer(fields: list[dict]) -> list[dict]:
    """Drop API-origin fields unless explicitly customer-visible, and strip
    build-time visibility metadata from the rest. Never mutates the input --
    returns fresh copies.
    """
    out = []
    for f in fields:
        if f.get("origin") == "api" and not f.get("customerVisible", False):
            continue
        out.append(
            {
                k: v
                for k, v in f.items()
                if k not in ("showWhen", "customerVisible")
            }
        )
    return out


def _option_change(before: dict, after: dict) -> tuple[str | None, str | None]:
    """Isolate the single values[] entry that changed between before/after.

    Matches by `value` key first (covers add/remove/relabel-in-place), then
    falls back to a positional diff for the case where the value code itself
    changed via new_option_value.
    """
    b_values = before.get("values") or []
    a_values = after.get("values") or []
    by_value_b = {v["value"]: v for v in b_values}
    by_value_a = {v["value"]: v for v in a_values}

    added = [v for v in a_values if v["value"] not in by_value_b]
    if added:
        return None, added[0]["label"]
    removed = [v for v in b_values if v["value"] not in by_value_a]
    if removed:
        return removed[0]["label"], None
    for v in a_values:
        old = by_value_b.get(v["value"])
        if old and old["label"] != v["label"]:
            return old["label"], v["label"]
    for old, new in zip(b_values, a_values):
        if old != new:
            return old.get("label"), new.get("label")
    return None, None


def _screen_fields_for_display(screen_id: str, session_id: str | None) -> list[dict]:
    """Best-effort fields lookup for rendering a showWhen's referenced field
    as a label -- prefers a session's live draft, else reads on-disk directly.
    Deliberately not resolve_screen_fields: this is a cosmetic label lookup
    for a chat reply and shouldn't trigger conditional-rule computation as a
    side effect.
    """
    fields = get_draft_fields(session_id, screen_id) if session_id else None
    if fields is not None:
        return fields
    path = os.path.join(DATA_DIR, f"{screen_id}.json")
    try:
        with open(path) as f:
            return json.load(f)[0]["fields"]
    except (OSError, IndexError, KeyError):
        return []


def _describe_show_when(sw: dict, screen_id: str, session_id: str | None) -> str:
    target_screen_id = sw.get("screenId", screen_id)
    fields = _screen_fields_for_display(target_screen_id, session_id)
    target_field = next((f for f in fields if f.get("path") == sw["path"]), None)
    label = target_field.get("fieldLabel") if target_field else sw["path"]

    value_display = sw["value"]
    if target_field:
        option = next(
            (o for o in target_field.get("values", []) or [] if o.get("value") == sw["value"]), None
        )
        if option:
            value_display = option.get("label", value_display)

    op = sw["op"]
    if op == "eq":
        return f"{label} is {value_display}"
    if op == "ne":
        return f"{label} is not {value_display}"
    if op == "lt":
        return f"{label} is less than {value_display}"
    if op == "gte":
        return f"{label} is {value_display} or above"
    return f"{label} {op} {value_display}"


def _describe_diff_friendly(d: dict, session_id: str | None) -> str:
    screen_id = d["screen_id"]
    screen = _screen_display(screen_id)
    op, before, after = d["op"], d.get("before"), d.get("after")

    if op == "add_field":
        sentence = f"Added a new field, '{after.get('fieldLabel')}', on {screen}."
        options = after.get("values") or []
        if options:
            labels = ", ".join(o["label"] for o in options)
            sentence += f" Options: {labels}."
        show_when = after.get("showWhen")
        if show_when:
            sentence += f" It's shown when {_describe_show_when(show_when, screen_id, session_id)}."
        return sentence
    if op == "delete_field":
        return f"Removed the '{before.get('fieldLabel')}' field from {screen}."
    if op == "rename_field":
        return f"Renamed '{before.get('fieldLabel')}' to '{after.get('fieldLabel')}' on {screen}."
    if op == "set_default_value":
        field_label = after.get("fieldLabel")
        old_value, new_value = before.get("value"), after.get("value")
        return f"Changed the default value of '{field_label}' from '{old_value}' to '{new_value}' on {screen}."
    if op in ("add_option", "rename_option", "remove_option"):
        field_label = (after or before).get("fieldLabel")
        old_label, new_label = _option_change(before or {}, after or {})
        if op == "add_option":
            return f"Added a new option, '{new_label}', to {field_label} on {screen}."
        if op == "remove_option":
            return f"Removed the '{old_label}' option from {field_label} on {screen}."
        if old_label and new_label and old_label != new_label:
            return f"Renamed the '{old_label}' option to '{new_label}' on {field_label} ({screen})."
        return f"Updated an option on {field_label} ({screen})."
    if op == "set_show_when":
        field_label = after.get("fieldLabel")
        condition = _describe_show_when(after["showWhen"], screen_id, session_id)
        return f"'{field_label}' on {screen} will now only show when {condition}."
    if op == "clear_show_when":
        field_label = after.get("fieldLabel")
        return f"'{field_label}' on {screen} will now always show (visibility condition removed)."
    return f"Updated {screen}."


def _join_diffs(diffs: list[dict], session_id: str | None) -> str:
    sentences = [_describe_diff_friendly(d, session_id) for d in diffs]
    if not sentences:
        return ""
    if len(sentences) == 1:
        return sentences[0]
    return "\n".join(f"- {s}" for s in sentences)


def _render_pending_reply(diffs: list[dict], session_id: str | None) -> str:
    body = _join_diffs(diffs, session_id) or "There's nothing staged to confirm."
    lead = "Here's what I'm about to update — please confirm:" if len(diffs) > 1 else ""
    cta = "Reply to confirm, or tell me what you'd like to change instead."
    return "\n\n".join(p for p in (lead, body, cta) if p)


def _render_written_reply(diffs: list[dict], session_id: str | None) -> str:
    if not diffs:
        return "Done — no changes were necessary."
    body = _join_diffs(diffs, session_id)
    if len(diffs) == 1:
        return f"Done — {body}"
    return f"All set — I've made the following updates:\n\n{body}"


def _render_rejected_reply(diffs: list[dict], session_id: str | None) -> str:
    if not diffs:
        return "No problem — nothing was changed."
    body = _join_diffs(diffs, session_id)
    if len(diffs) == 1:
        return f"No problem — I discarded that change ({body}). Nothing was written to disk."
    return f"No problem — I've discarded the following and nothing was written to disk:\n\n{body}"


def _screen_view(screen_id: str, session_id: str | None = None, audience: str = "admin") -> list | None:
    """Full screen content (the same one-element-list shape as the seed
    files). If `session_id` has a staged (not yet confirmed) admin-chat draft
    for this screen, that draft's fields are shown as-is. Otherwise, any
    conditional_rules.json rules currently matching (same-screen or
    cross-screen, e.g. screen_2's connectionType affecting screen_3's billing
    fields) are baked in on top of the on-disk fields. None if the screen
    file doesn't exist on disk.

    `audience=="customer"` additionally drops any field whose `showWhen`
    doesn't currently hold. `audience=="admin"` (the default) shows every
    field regardless of showWhen state, so an admin can see/manage a
    condition right after creating it without first simulating a customer's
    choices via /preview/field-change.
    """
    path = os.path.join(DATA_DIR, f"{screen_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        screen = json.load(f)
    if session_id:
        draft_fields = get_draft_fields(session_id, screen_id)
        if draft_fields is not None:
            if audience == "customer":
                draft_fields = filter_visible(draft_fields, screen_id, session_id)
            screen[0]["fields"] = draft_fields
            return screen
    fields, _ = resolve_screen_fields(get_backend(), screen_id, session_id)
    if audience == "customer":
        fields = filter_visible(fields, screen_id, session_id)
    screen[0]["fields"] = fields
    return screen


@app.post("/admin/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    try:
        agent = get_agent()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    session_id = body.session_id
    run_id = str(uuid.uuid4())
    config = {
        "configurable": {"thread_id": session_id},
        "callbacks": [RunLoggingHandler(run_id, session_id)],
    }
    was_awaiting = session_id in _awaiting_confirmation

    logger.info("run=%s session=%s CHAT_START awaiting=%s message=%r", run_id, session_id, was_awaiting, body.message)

    if was_awaiting:
        decision_type = classify_decision(body.message)
        count = _pending_action_count.get(session_id, 1)
        decision = build_decision(decision_type, body.message)
        logger.info("run=%s session=%s RESUME decision=%s count=%d", run_id, session_id, decision_type, count)
        result = agent.invoke(
            Command(resume={"decisions": [decision] * count}),
            config=config,
            version="v2",
        )
    else:
        decision_type = None
        result = agent.invoke(
            {"messages": [{"role": "user", "content": body.message}]},
            config=config,
            version="v2",
        )

    if result.interrupts:
        interrupt_value = result.interrupts[0].value
        action_requests = interrupt_value["action_requests"]
        _awaiting_confirmation.add(session_id)
        _pending_action_count[session_id] = len(action_requests)
        diffs = get_pending_diffs(session_id)
        screen_ids = sorted({d["screen_id"] for d in diffs})
        preview_screens = {sid: _screen_view(sid, session_id) for sid in screen_ids}
        reply = _render_pending_reply(diffs, session_id)
        logger.info("run=%s session=%s CHAT_END status=pending_confirmation pending_actions=%d", run_id, session_id, len(action_requests))
        return ChatResponse(
            reply=reply,
            pending_diff={"edits": diffs, "actions": action_requests, "preview_screens": preview_screens},
            awaiting_confirmation=True,
            status="pending_confirmation",
            screen_ids=screen_ids,
            run_id=run_id,
        )

    if was_awaiting:
        diffs_at_resume = get_pending_diffs(session_id)  # capture before clear_thread_state wipes it
        _awaiting_confirmation.discard(session_id)
        _pending_action_count.pop(session_id, None)
        if decision_type == "approve":
            status = "ok"
            screen_ids = sorted({d["screen_id"] for d in diffs_at_resume})
            reply = _render_written_reply(diffs_at_resume, session_id)
            clear_thread_state(session_id)
        elif decision_type == "reject":
            status = "rejected"
            screen_ids = []
            reply = _render_rejected_reply(diffs_at_resume, session_id)
            clear_thread_state(session_id)
        else:  # respond: agent gave a clarifying follow-up instead of re-proposing a write
            status = "info"
            screen_ids = []
            messages = result.value.get("messages", []) if result.value else []
            reply = messages[-1].content if messages else ""
    else:
        status = "info"
        screen_ids = []
        messages = result.value.get("messages", []) if result.value else []
        reply = messages[-1].content if messages else ""

    logger.info("run=%s session=%s CHAT_END status=%s screen_ids=%s", run_id, session_id, status, screen_ids)
    return ChatResponse(
        reply=reply,
        pending_diff=None,
        awaiting_confirmation=False,
        status=status,
        screen_ids=screen_ids,
        run_id=run_id,
    )


@app.post("/preview/field-change", response_model=ConditionalPreviewResponse)
def preview_field_change(
    body: ConditionalPreviewRequest,
    user_id: str | None = Header(
        default=None,
        alias="userID",
        description="Mock customer id used to select a configured credit score.",
    ),
) -> ConditionalPreviewResponse:
    """Conditional Preview Adapter: given a dropdown/radio field change, return
    a preview of that screen's fields with any matching conditional_rules.json
    entry applied. Read-only -- never writes screen_N.json. Not part of the
    admin-chat agent: no LLM call, no confirm/approve step.
    """
    try:
        fields, rule_matched = apply_conditional_rules(
            get_backend(), body.screen_id, body.path, body.value, body.session_id, user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    fields = _strip_for_customer(fields)
    return ConditionalPreviewResponse(screen_id=body.screen_id, fields=fields, rule_matched=rule_matched)


@app.get("/admin/screens/{screen_id}")
def get_screen(screen_id: str, session_id: str | None = None, audience: Literal["admin", "customer"] = "admin"):
    """Returns the screen as-is from disk, unless `session_id` is given and
    that session has a staged (not yet confirmed) draft for this screen --
    in which case the draft's fields are shown instead, so a pending edit
    can be previewed before the admin confirms it.

    `audience=customer` drops any field whose `showWhen` doesn't currently
    hold, strips visibility metadata from the rest, and drops origin=='api'
    fields unless they opt in with customerVisible=true -- the shape a
    customer-facing UI should see. `audience=admin`
    (the default) shows every field, met condition or not, so the admin can
    verify/manage a condition right after creating it.
    """
    screen = _screen_view(screen_id, session_id, audience)
    if screen is None:
        raise HTTPException(status_code=404, detail=f"unknown screen '{screen_id}'")
    if audience == "customer":
        screen[0]["fields"] = _strip_for_customer(screen[0]["fields"])
    return screen


@app.post("/admin/screens/{screen_id}/reset")
def reset_screen(screen_id: str):
    """Overwrite one screen's file on disk with its pristine seed copy.

    Unlike edits made through /admin/chat, this is a direct, ungated reset
    utility for testing/demo purposes -- it does not go through the
    confirm-a-diff flow. Also clears any in-memory draft for this screen
    (across every session), so a stale preview can't show edits drafted
    against fields that no longer match the reset baseline.
    """
    seed_path = os.path.join(SEED_DIR, f"{screen_id}.json")
    if not os.path.isfile(seed_path):
        raise HTTPException(status_code=404, detail=f"no seed backup for '{screen_id}'")
    shutil.copyfile(seed_path, os.path.join(DATA_DIR, f"{screen_id}.json"))
    reset_drafts_for_screen(screen_id)
    clear_recorded_values(screen_id)
    logger.info("SCREEN_RESET screen_id=%s", screen_id)
    return get_screen(screen_id)


@app.post("/admin/screens/reset")
def reset_all_screens():
    """Reset every screen that has a seed backup. See reset_screen."""
    reset_ids = []
    for name in sorted(os.listdir(SEED_DIR)):
        if name.startswith("screen_") and name.endswith(".json"):
            screen_id = name.removesuffix(".json")
            shutil.copyfile(os.path.join(SEED_DIR, name), os.path.join(DATA_DIR, name))
            reset_drafts_for_screen(screen_id)
            clear_recorded_values(screen_id)
            reset_ids.append(screen_id)
    logger.info("SCREEN_RESET_ALL screen_ids=%s", reset_ids)
    return {"reset": reset_ids}
