# AARYA Admin Chat API — Testing & Integration Guide

This is a practical, copy-paste guide for testing `POST /admin/chat` and
wiring up the frontend flow: send a message → show a preview of the pending
edit → confirm → refresh the screen. Base URL below assumes
`uvicorn main:app --reload` running on the default port.

```
http://127.0.0.1:8000
```

Everything here can be run via `curl`, Postman, or FastAPI's own Swagger UI
at `http://127.0.0.1:8000/docs`.

---

## 1. The four endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/admin/chat` | Send an admin message, get back a reply + status |
| GET | `/admin/screens/{screen_id}` | Read a screen — live, or a preview of a pending edit |
| POST | `/admin/screens/{screen_id}/reset` | Reset one screen to its seed state (testing only) |
| POST | `/admin/screens/reset` | Reset every screen to its seed state (testing only) |

---

## 2. How to test `POST /admin/chat`

Request body is always the same shape:

```json
{ "session_id": "any-string-you-pick", "message": "what the admin typed" }
```

`session_id` is the conversation thread — reuse the same one for a whole
back-and-forth (stage a change, then confirm it), and use a new one to start
an unrelated conversation.

### Response shape

```json
{
  "reply": "string — plain language, safe to show directly to the admin",
  "pending_diff": { "edits": [...], "actions": [...], "preview_screens": {...} } | null,
  "awaiting_confirmation": true | false,
  "status": "ok" | "pending_confirmation" | "rejected" | "info",
  "screen_ids": ["screen_1", ...],
  "run_id": "uuid — grep logs/agent.log with this to debug a specific turn"
}
```

### Example: staging a change

```bash
curl -X POST http://127.0.0.1:8000/admin/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "demo-1", "message": "rename Postal Code to Zip Code on screen 1"}'
```

Response (trimmed — `preview_screens.screen_1` in reality has all 6 of
screen_1's fields, not just the one shown here):

```json
{
  "reply": "Renamed 'Postal Code' to 'Zip Code' on Screen 1.\n\nReply to confirm, or tell me what you'd like to change instead.",
  "pending_diff": {
    "edits": [
      {
        "screen_id": "screen_1",
        "op": "rename_field",
        "path": "connectionInfo.address.postalCode",
        "before": { "fieldLabel": "Postal Code", "...": "..." },
        "after": { "fieldLabel": "Zip Code", "...": "..." }
      }
    ],
    "actions": [ { "name": "write_screen_json", "args": { "screen_id": "screen_1" } } ],
    "preview_screens": {
      "screen_1": [
        {
          "sessionId": "sess-20260731-0000",
          "stage": 1,
          "status": "STAGE_COMPLETE",
          "nextStage": 2,
          "fields": [
            { "path": "connectionInfo.category", "fieldLabel": "Category", "...": "unchanged" },
            "... every other field on screen_1, unchanged ...",
            { "path": "connectionInfo.address.postalCode", "fieldLabel": "Zip Code", "...": "<- this one changed" }
          ],
          "ask": "Connection Type",
          "say": "Connection Info is complete. Moving on to Service Details. Please provide Connection Type."
        }
      ]
    }
  },
  "awaiting_confirmation": true,
  "status": "pending_confirmation",
  "screen_ids": ["screen_1"],
  "run_id": "e706a61c-f0ce-4652-82b1-985d8f162ace"
}
```

`pending_diff.preview_screens` has one entry per id in `screen_ids`, and
each value is the **entire screen** — every field, exactly the same shape
`GET /admin/screens/{screen_id}` returns — with the staged edit(s) already
applied. The UI can hand that object straight to whatever renders the form
and get an accurate "here's what it'll look like" preview, with zero extra
API calls. `pending_diff.edits` is still there too, for a smaller per-field
diff view if that's all you need.

### Example: confirming it (same `session_id`)

```bash
curl -X POST http://127.0.0.1:8000/admin/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "demo-1", "message": "yes"}'
```

Response:

```json
{
  "reply": "Done — Renamed 'Postal Code' to 'Zip Code' on Screen 1.",
  "pending_diff": null,
  "awaiting_confirmation": false,
  "status": "ok",
  "screen_ids": ["screen_1"],
  "run_id": "6fdb0adc-07f4-40b4-a736-aa3ac87b0307"
}
```

Any reply works for confirm/decline — the wording is classified, not matched
exactly:
- **Approve**: "yes", "yep", "confirm", "ok", "go ahead", "sure", ...
- **Reject**: "no", "cancel", "stop", "don't", "discard", ...
- **Anything else** (while a diff is pending): treated as follow-up
  feedback — the agent revises the draft and proposes a new diff, still
  under `status: "pending_confirmation"`. Nothing is written until you
  actually say yes.

---

## 3. What the UI needs to check — `status` decision table

This is the only field the frontend needs to branch on. Ignore `reply`
entirely for control flow — it's for display only.

| `status` | What happened | What the UI should do |
|---|---|---|
| `pending_confirmation` | A diff is staged, nothing written yet | Show `reply`, then render `pending_diff.preview_screens[screen_id]` directly for each affected screen — no extra call needed. Wait for the admin's next message. |
| `ok` | Diff(s) approved and **written to disk** | Show `reply`. For each id in `screen_ids`, call `GET /admin/screens/{screen_id}` (no `session_id`) to refresh that screen's real data. |
| `rejected` | Admin declined; nothing written | Show `reply`. `screen_ids` is empty — nothing to refetch. |
| `info` | No diff exists this turn (clarifying question, candidate list, or "no change needed") | Just show `reply` as a chat message. `screen_ids` is empty — nothing to refetch. |

**Important:** `screen_ids` is only a "go refetch this" signal when
`status == "ok"`. On `pending_confirmation` it's just telling you which
screens `preview_screens` has an entry for — nothing has changed on disk
yet, so plain `GET /admin/screens/{screen_id}` (without `session_id`) would
still show stale data at that point.

---

## 4. Showing a preview before confirmation

**Preferred: use what's already in the response.** While
`status == "pending_confirmation"`, `pending_diff.preview_screens` already
has a full copy of every affected screen with the staged edit(s) applied —
see the worked example in section 2. Just render
`pending_diff.preview_screens.screen_1` (etc.) directly. No extra request.

**Alternative: fetch it yourself.** Useful if you want to re-render the
preview later without holding onto the original chat response (e.g. after
a page reload) — call the same screen-read endpoint you'd use normally, but
add `?session_id=<the same session_id>`:

```bash
curl "http://127.0.0.1:8000/admin/screens/screen_1?session_id=demo-1"
```

This returns the screen in the exact same shape as a normal `GET`, except
the fields reflect the **staged draft**, not what's on disk yet — the same
content you'd already have gotten from `preview_screens`. Example — after
staging "add a Retired option to Sub Category on screen 1":

```bash
# Plain GET (no session_id) -- still shows the OLD data, nothing written yet:
curl "http://127.0.0.1:8000/admin/screens/screen_1"
# -> values: Student, Salaried, Self Employed

# Preview GET (with session_id) -- shows the staged draft:
curl "http://127.0.0.1:8000/admin/screens/screen_1?session_id=demo-1"
# -> values: Student, Salaried, Self Employed, Retired
```

If that `session_id` has nothing staged for that screen (wrong id, already
confirmed, already discarded), the `session_id` param is simply ignored and
you get the plain disk content — safe to call unconditionally.

---

## 5. Once confirmed — which APIs to call

After a `POST /admin/chat` reply comes back with `status: "ok"`:

1. Read `screen_ids` from that same response.
2. For **each** id in that list, call:
   ```bash
   curl "http://127.0.0.1:8000/admin/screens/{screen_id}"
   ```
   (no `session_id` needed — the change is now permanent on disk, this is
   just the normal read.)
3. Replace whatever the UI was showing for that screen with this response.

That's the entire post-confirmation step — no other endpoint is involved.
If the admin's message touched multiple screens in one turn, `screen_ids`
will list all of them, so call the GET once per id.

---

## 6. Resetting test data between runs

Since edits actually get written to disk, running through these examples
repeatedly will drift the JSON files away from their original seed state.
Reset when you need a clean slate:

```bash
# one screen
curl -X POST http://127.0.0.1:8000/admin/screens/screen_1/reset

# every screen
curl -X POST http://127.0.0.1:8000/admin/screens/reset
```

These are direct, ungated resets (no confirm step) meant for testing only —
not part of the admin-facing chat flow.

---

## 7. A complete test script, start to finish

```bash
BASE=http://127.0.0.1:8000
SID=walkthrough-1

# 1. Stage a change -- the response's pending_diff.preview_screens.screen_1
#    already shows Sub Category with "Retired" added, no extra call needed
curl -s -X POST $BASE/admin/chat -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SID\", \"message\": \"add a Retired option to Sub Category on screen 1\"}"
# -> status: pending_confirmation, screen_ids: ["screen_1"]

# 1b. (optional) fetch the same preview again independently, e.g. after a reload
curl -s "$BASE/admin/screens/screen_1?session_id=$SID"
# -> Sub Category now includes "Retired" (draft only)

# 2. Confirm it
curl -s -X POST $BASE/admin/chat -H "Content-Type: application/json" \
  -d "{\"session_id\": \"$SID\", \"message\": \"yes\"}"
# -> status: ok, screen_ids: ["screen_1"]

# 3. Refetch the real screen (per step 2's screen_ids)
curl -s "$BASE/admin/screens/screen_1"
# -> Sub Category now includes "Retired" for real

# 4. Clean up
curl -s -X POST $BASE/admin/screens/screen_1/reset
```
