# AARYA Admin Chat API — Testing & Integration Guide

This is a practical, copy-paste guide for testing `POST /admin/chat` and
wiring up the frontend flow: send a message → show a preview of the pending
edit → confirm → refresh the screen. It also covers the separate, non-LLM
**Conditional Preview Adapter** (`POST /preview/field-change`, section 8) that
the onboarding UI calls whenever the customer changes a dropdown/radio value.
Base URL below assumes `uvicorn main:app --reload` running on the default
port.

```
http://127.0.0.1:8000
```

Everything here can be run via `curl`, Postman, or FastAPI's own Swagger UI
at `http://127.0.0.1:8000/docs`.

**Supported edits**: add/delete/rename a field (label only), add/rename/
remove a dropdown or radio option, and set a field's default value. Not
supported: a field's `required` status, `controlType`, or `dataType` — the
agent will say so plainly instead of reporting a false success.

---

## 1. The five endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/admin/chat` | Send an admin message, get back a reply + status |
| GET | `/admin/screens/{screen_id}` | Read a screen — live (with any matching conditional rule already baked in), or a preview of a pending admin-chat edit |
| POST | `/preview/field-change` | UI calls this on every dropdown/radio change; returns that screen's fields with any matching rule applied, no LLM, no write (see section 8) |
| POST | `/admin/screens/{screen_id}/reset` | Reset one screen to its seed state and clear its recorded live selections (testing only) |
| POST | `/admin/screens/reset` | Reset every screen the same way (testing only) |

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
not part of the admin-facing chat flow. They also clear any live selection
recorded via `/preview/field-change` that originated from that screen (see
section 8) — e.g. resetting screen_2 forgets a recorded Prepaid/Postpaid
choice, so screen_3's billing fields go back to their default.

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

---

## 8. The Conditional Preview Adapter — `POST /preview/field-change`

This is a **completely separate feature from `/admin/chat`**: no LLM call, no
confirm/reject step, nothing ever written to disk. It's a plain lookup
against `data/conditional_rules.json`, built to feel instant for the
onboarding UI. Call it every time the customer changes a dropdown or radio
value:

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change \
  -H "Content-Type: application/json" \
  -d '{"screen_id": "screen_1", "path": "connectionInfo.subCategory", "value": "211"}'
```

### Response shape

```json
{
  "screen_id": "screen_1",
  "fields": [ "...that screen's full fields[] list, with any matching rule's changes applied..." ],
  "rule_matched": "student_id_upload"
}
```

If nothing in `conditional_rules.json` matches `(screen_id, path, value)`,
`rule_matched` is `null` and `fields` comes back unchanged — that's the
default state (e.g. picking "Salaried" instead of "Student" fires nothing).

### It also drives what other screens show — no extra endpoint needed

Some rules change fields on a **different** screen than the one that fired
them. For example: picking **Prepaid** for Connection Type on screen_2
removes 4 billing fields from **screen_3** (Billing Account Name, Billing
Address, Billing Email, Billing Contact Number); picking Postpaid keeps them.

The mechanism: every call to `/preview/field-change` records
`(screen_id, path) -> value` as "the last thing the UI reported for this
field." `GET /admin/screens/{screen_id}` reads that same record to decide
what to show — so the UI doesn't need to do anything extra for this to work;
just keep calling `/preview/field-change` the way it already does, and later
`GET` calls for any affected screen pick it up automatically:

```bash
BASE=http://127.0.0.1:8000

# Baseline: screen_2 defaults to Postpaid, so screen_3 has all 14 fields
curl -s "$BASE/admin/screens/screen_3" | python3 -c "import json,sys; print(len(json.load(sys.stdin)[0]['fields']))"
# -> 14

# Customer picks Prepaid on screen_2 -- the ONLY call the UI makes for this
curl -s -X POST $BASE/preview/field-change -H "Content-Type: application/json" \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"2"}'
# -> screen_2's own fields, rule_matched: null (the rule's changes target screen_3, not screen_2)

# Customer navigates to screen_3 -- plain GET, nothing else
curl -s "$BASE/admin/screens/screen_3" | python3 -c "import json,sys; print(len(json.load(sys.stdin)[0]['fields']))"
# -> 10 (all 4 billing fields gone)
```

If a trigger field has never been reported via `/preview/field-change` (e.g.
right after a server restart), `GET` falls back to that field's on-disk
default value — which is why the baseline above already reflects Postpaid
without any prior call.

**Important — this state is in-memory only**, the same way admin-chat's
staged drafts are: it resets on server restart, and `/admin/screens/{screen_id}/reset`
/ `/admin/screens/reset` clear it explicitly (see section 6). It is never
written to `screen_N.json`.

### Same-screen rules are baked into GET too

The same mechanism applies even when a rule's trigger and target are the
*same* screen — e.g. screen_1's Student ID Upload field. Since screen_1's
`subCategory` already defaults to "Student" (`211`) in the seed data, it
shows up on a plain `GET` with no preview call at all:

```bash
curl -s "$BASE/admin/screens/screen_1" | python3 -c "import json,sys; print(len(json.load(sys.stdin)[0]['fields']))"
# -> 7 (includes Student ID Card Upload)
```

### All six seeded rules (`data/conditional_rules.json`)

| `ruleId` | Trigger | Effect |
|---|---|---|
| `student_id_upload` | screen_1 Sub Category = Student (`211`) | adds Student ID Card Upload to screen_1 |
| `prepaid_no_billing_info` | screen_2 Connection Type = Prepaid (`2`) | removes 4 billing fields from **screen_3** (cross-screen) |
| `ftth_start_date_optional` | screen_2 Service Type = FTTH (`2`) | makes Service Start Date optional on screen_2 |
| `esim_no_delivery_address` | screen_2 SIM Type = eSIM | removes the SIM delivery address field from screen_2 |
| `passport_issuing_country` | screen_3 ID Type = Passport | adds Passport Issuing Country to screen_3 |
| `mnp_porting_fields` | screen_2 Onboarding Type = MNP | adds Donor Network + Porting Authorization Code to screen_2 |
