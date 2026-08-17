# AARYA Admin Field-Editor (POC)

A small chat-driven backend that lets an admin edit the onboarding screen JSON
files (`data/screen_N.json`) in plain language instead of a code/PR/redeploy
cycle. Admin describes a change, the agent identifies the exact field, stages
a diff in memory, and only writes to disk once the admin confirms.

This is a proof-of-concept: no auth, no Docker, no database, flat JSON files
on disk, one FastAPI process, run locally.

## Stack

- Python 3.11+, FastAPI
- `deepagents` (`create_deep_agent`) for the agent harness, with a real
  `FilesystemBackend` pointed at `./data` (not the ephemeral in-memory
  default) so edits are actual files the user-facing agent can read.
- `langchain-openrouter` (`ChatOpenRouter`) as the model provider, reading
  `OPENROUTER_API_KEY` from env.
- `langgraph`'s `InMemorySaver` checkpointer for conversation + human-in-the-
  loop interrupt state, keyed by `session_id` as `thread_id`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set OPENROUTER_API_KEY (OPENROUTER_MODEL defaults to
# anthropic/claude-sonnet-4.6 if left unset)
```

Run:

```bash
uvicorn main:app --reload
```

## API

- `POST /admin/chat`
  Body: `{"session_id": "string", "message": "string"}`
  Response: `{"reply": "string", "pending_diff": {...} | null, "awaiting_confirmation": bool, "status": "ok" | "pending_confirmation" | "rejected" | "info", "screen_ids": ["screen_1", ...], "run_id": "string"}`

  `reply` is always plain, non-technical language (no field paths, control
  types, or raw diffs) meant to be shown to the admin as-is.

  `status` drives frontend behavior:
  - `"ok"` — a change was just written to disk. `screen_ids` lists which
    screens changed; **this is the only status that means "go refetch."**
  - `"pending_confirmation"` — a diff is staged for review, nothing written
    yet. `screen_ids` here is just for labeling the review panel, not a
    refetch signal — the admin still needs to confirm.
  - `"rejected"` — the admin declined; nothing was written. `screen_ids` is
    empty.
  - `"info"` — no diff exists this turn (a clarifying question, a candidate
    shortlist, or "no change needed"). `screen_ids` is empty; just show
    `reply`.

  On `"ok"`, the frontend should call `GET /admin/screens/{screen_id}` for
  each id in `screen_ids` to refresh its view of that screen.

  On `"pending_confirmation"`, `pending_diff.preview_screens` already
  contains a full, ready-to-render copy of every touched screen (all
  fields, not just the ones that changed) with the staged edits applied —
  same shape as `GET /admin/screens/{screen_id}` returns, keyed by screen
  id (e.g. `pending_diff.preview_screens.screen_1`). No extra API call is
  needed to show a live preview of the pending change; `pending_diff.edits`
  is still there alongside it for a smaller, per-field diff view if you
  want one instead.

- `GET /admin/screens/{screen_id}`
  Returns the current `screen_N.json` content straight from disk (e.g.
  `screen_id=screen_1`). Useful for verifying a change landed without going
  through chat.

  Optional query param `?session_id=...`: if that session has a staged
  (not yet confirmed) edit for this screen, the response shows the *draft*
  instead of what's on disk — same JSON shape either way, and the same
  content already embedded in `pending_diff.preview_screens` above. Handy
  if you want to re-render the preview later without holding onto the
  original chat response (e.g. after a page reload). Once approved, plain
  `GET /admin/screens/{screen_id}` (no `session_id` needed) reflects the
  same thing permanently. If nothing is staged for that session/screen, the
  `session_id` param is simply ignored and disk content is returned as usual.

- `POST /admin/screens/{screen_id}/reset`
  Overwrites that screen's file with its pristine seed copy (kept in
  `seed_data/`, populated once from the original `data/screen_N.json`
  files). This is a direct, ungated reset for testing/demo purposes — it
  does not go through the confirm-a-diff flow like edits made via chat do.
  Also clears any in-memory draft for that screen. Returns the reset screen
  content.

- `POST /admin/screens/reset`
  Same, but resets every screen that has a seed backup. Returns
  `{"reset": ["screen_1", "screen_2", ...]}`.

## Worked example

```bash
curl -X POST localhost:8000/admin/chat -H 'content-type: application/json' \
  -d '{"session_id": "demo-1", "message": "rename Postal Code to Zip Code on screen 1"}'
```

The agent classifies the screen, matches the field by `path`
(`connectionInfo.address.postalCode`), confirms it isn't already labeled that
way, stages the rename in an in-memory draft, and responds with
`awaiting_confirmation: true` and a `pending_diff` showing the before/after.

```bash
curl -X POST localhost:8000/admin/chat -H 'content-type: application/json' \
  -d '{"session_id": "demo-1", "message": "yes"}'
```

This resumes the paused graph with an `approve` decision, which runs
`write_screen_json` and persists the change to `data/screen_1.json`.

```bash
curl localhost:8000/admin/screens/screen_1
```

Confirms `fieldLabel` is now `"Zip Code"` and `path` is unchanged
(`connectionInfo.address.postalCode` — paths are never renamed, only labels).

Try, on the same or a fresh session: adding an option to a dropdown ("add a
'Retired' option to Sub Category on screen 1"), asking for a field that
doesn't exist clearly enough to trigger the candidate-shortlist path, or
replying with something other than yes/no after a diff is shown (e.g. "no,
call it something else") to see the feedback loop continue without a fresh
disk write.

## Design notes / deviations from the original prompt

The prompt was written as "last known API shape, verify against current
docs." The installed versions here are `deepagents==0.7.6` and
`langchain-openrouter==0.2.8`; the actual `deepagents`/LangGraph source and
current docs (via Context7) were used to confirm or correct the following:

- **Interrupt/resume shape.** `agent.invoke(..., version="v2")` returns a
  `GraphOutput` with `.value` (the graph's state at that point) and
  `.interrupts` (a tuple of `Interrupt` objects, each with `.value` holding
  `{"action_requests": [...], "review_configs": [...]}`). Resuming uses
  `agent.invoke(Command(resume={"decisions": [...]}), config=config,
  version="v2")` — one decision dict per pending `action_request`, not a bare
  scalar. This matches current `deepagents` docs closely; the prompt's
  "look for an `__interrupt__` key" guess was for the older `v1` invoke path,
  which still exists but the `v2`/`GraphOutput` shape is what's documented
  and used here.
- **`allowed_decisions` uses `respond`, not `edit`.** deepagents' literal
  `"edit"` decision lets the admin hand-edit a tool call's *arguments*.
  `write_screen_json`'s only argument is `screen_id` — there's nothing
  meaningful for an admin to edit there (the actual field-level changes live
  in `apply_field_edit` calls made *before* the write, not in the write
  itself). So `write_screen_json`'s `interrupt_on` config here is
  `{"allowed_decisions": ["approve", "reject", "respond"]}`, and the
  free-form-feedback case (prompt step 6/7's "anything else") is implemented
  as a `respond` decision carrying a synthetic tool message that tells the
  model not to treat it as a successful write and to keep refining the draft.
  This was verified necessary, not just a style choice: a plain new
  `agent.invoke({"messages": ...})` call against a `thread_id` that has a
  pending interrupt is not how LangGraph resumption works — resuming via
  `Command` is required before the thread will accept further input, so
  *every* reply while `awaiting_confirmation` is true (approve, reject, or
  other feedback) goes through `Command(resume=...)` in `main.py`, just with
  a different decision `type`.
- **Built-in filesystem tools are permission-denied, not omitted.** Passing a
  `backend` to `create_deep_agent` unconditionally attaches deepagents' own
  `ls`/`read_file`/`write_file`/`edit_file`/`glob`/`grep` tools backed by that
  same `FilesystemBackend` — there's no flag to leave them off. To keep the
  "never write to disk outside `write_screen_json`" rule airtight,
  `agent.py` passes
  `permissions=[FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]`,
  which hard-blocks the built-in write/edit/delete tools across the whole
  backend root. Reads stay available (unused by the six custom tools, which
  each talk to `backend` directly instead of going through the generic
  tools) but nothing can write except through the gated tool.
- **In-memory draft storage.** The spec says `apply_field_edit` mutates "an
  in-memory draft" without specifying where that draft lives. Threading a
  custom field through deepagents' `state_schema` was more machinery than a
  flat POC warranted, so `tools.py` keeps a plain module-level
  `dict[(thread_id, screen_id), fields]` instead, seeded by
  `load_screen_fields` and mutated by `apply_field_edit`. This does not
  survive a process restart — acceptable here since there's no database in
  scope either. `load_screen_fields` intentionally re-seeds (discards) that
  screen's draft from disk, so the agent's own system prompt is relied on to
  call it only for a genuinely new request, not mid-refinement of a pending
  diff (see the "edit/feedback" bullet above).

## File layout

- `main.py` — FastAPI app, the two endpoints, resume-vs-new-message routing.
- `agent.py` — `create_deep_agent` wiring: model, backend, permissions,
  `interrupt_on`, checkpointer, system prompt.
- `tools.py` — the six tools (`classify_screen`, `load_screen_fields`,
  `classify_field`, `check_noop`, `apply_field_edit`, `write_screen_json`)
  and the in-memory draft/diff store.
- `models.py` — every Pydantic schema (tool structured-output shapes, the
  field-edit instruction shape, the API request/response shapes).
- `data/screen_1.json`, `data/screen_2.json` — the seed state; more
  `screen_N.json` files can be dropped in later without any code change,
  since screens are discovered via `glob` rather than hardcoded.
