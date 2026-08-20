# Postpaid Credit-Check — Manual Test Cases

Runbook for testing the `showWhen` field-visibility feature end to end. Assumes
the server is running locally:

```bash
uvicorn main:app --reload
```

Default base URL: `http://127.0.0.1:8000`.

---

## 0. Start clean

```bash
curl -X POST http://127.0.0.1:8000/admin/screens/reset
```

**Look for:** `{"reset":["screen_1","screen_2","screen_3"]}` — wipes any leftover
demo fields and clears recorded customer selections.

---

## 1. Build the credit-check fields (admin chat)

Each of these is a two-step exchange: send the request, get a diff back, then
send `"yes"` to confirm.

**Turn 1 — the trigger field**

```bash
curl -X POST http://127.0.0.1:8000/admin/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-1","message":"On screen 2, add a Credit Check Required radio button with options Yes and No, and only show it when Connection Type is Postpaid."}'
```

**Look for:** `status: "pending_confirmation"`, and `reply` says something like
*"Added a new field, 'Credit Check Required', on Screen 2. It's shown when
Connection Type is Postpaid."* — plain English, no raw field paths.

```bash
curl -X POST http://127.0.0.1:8000/admin/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-1","message":"yes"}'
```

**Look for:** `status: "ok"` — now it's actually on disk.

**Turn 2 — the score holder**

```bash
curl -X POST http://127.0.0.1:8000/admin/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-2","message":"On screen 2, add a hidden Credit Score field. It should be filled in by an external system, not the customer. Only show it when Credit Check Required is Yes."}'
```

Confirm with `"yes"` under `"session_id":"demo-2"` same as above.

**Turn 3 — low-score branch**

```bash
curl -X POST http://127.0.0.1:8000/admin/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-3","message":"Add a Deposit Amount text field on screen 2, shown when Credit Score is less than 500."}'
```

Confirm with `"yes"` under `"session_id":"demo-3"`.

**Turn 4 — high-score branch**

```bash
curl -X POST http://127.0.0.1:8000/admin/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-4","message":"On screen 2, add Bank Name and Bank Account Number text fields, both shown when Credit Score is 500 or above."}'
```

Confirm with `"yes"` under `"session_id":"demo-4"`.

---

## 2. Check the admin sees everything (even unmet conditions)

```bash
curl "http://127.0.0.1:8000/admin/screens/screen_2?audience=admin"
```

**Look for:** all 5 new fields present — `creditCheckRequired`, `creditScore`
(with `"origin": "api"`), `depositAmount`, `bankName`, `bankAccountNumber` —
each with its own `"showWhen"` block, even though no customer has triggered
anything yet. This is the "admin bypasses filtering" behavior we agreed on.

---

## 3. Play the customer journey (no chat, no LLM — just this one endpoint)

The credit-score lookup is now mocked **inside the backend** — the moment a
customer answers `creditCheckRequired = "YES"`, the backend itself generates a
score and records it in the same request. You no longer need a separate curl
call pretending to be n8n.

**Step 1 — picks Postpaid**

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"1"}'
```

**Look for:** `fields` now includes `serviceDetails.creditCheckRequired` —
nothing else new. No `showWhen` key anywhere in the response (customer-facing
shape).

**Step 2 — picks Credit Check = Yes (this is the "dynamic injection" moment)**

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.creditCheckRequired","value":"YES"}'
```

**Look for:** the response contains `serviceDetails.creditScore` with the
generated score and either `serviceDetails.depositAmount` *or*
`serviceDetails.bankName` + `serviceDetails.bankAccountNumber` — decided for
you in this one call.

Since the score is random, run this a few times (with a fresh `connectionType`
POST first each time, or just repeat step 2) and you'll see it land in either
branch across different attempts.

**To see what score was picked:**

```bash
grep MOCK_CREDIT_CHECK logs/agent.log | tail -5
```

**To force a particular branch on demand** (e.g. for a demo where you want to
guarantee the low-score path), edit the config — no restart needed, it's read
fresh on every request:

```bash
cat data/credit_score_mock.json
# { "min_score": 300, "max_score": 900 }

# force every mock lookup into the deposit-amount branch:
echo '{ "min_score": 300, "max_score": 450 }' > data/credit_score_mock.json

# force every mock lookup into the bank-fields branch:
echo '{ "min_score": 550, "max_score": 900 }' > data/credit_score_mock.json

# put it back when you're done:
echo '{ "min_score": 300, "max_score": 900 }' > data/credit_score_mock.json
```

> **Note:** trigger values (including the mock score) are stored globally per
> `(screen, path)` for this whole POC — not per customer session (documented
> limitation). If you want to test the "No" path cleanly after having already
> triggered a score, restart the server first (this state is in-memory only; a
> restart clears it without touching the JSON files already written to disk):

```bash
# restart uvicorn, then:
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"1"}'
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.creditCheckRequired","value":"NO"}'
```

**Look for:** none of Credit Score, Deposit Amount, or the bank fields ever
appear — because `creditCheckRequired` never became `"YES"`, so the mock
lookup never ran.

---

## 4. Confirm the customer never sees the metadata

```bash
curl "http://127.0.0.1:8000/admin/screens/screen_2?audience=customer"
```

**Look for:** no `"showWhen"` or `"customerVisible"` key on any field. Credit
Score remains marked `origin: "api"`, but is included because its admin screen
definition explicitly opts into customer visibility.

---

## 5. Regression checks (make sure old stuff still works)

```bash
# Prepaid should never get Credit Check Required, and should still strip screen_3's billing fields
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"2"}'
curl "http://127.0.0.1:8000/admin/screens/screen_3?audience=customer"
```

**Look for:** first response has no `creditCheckRequired`; second response is
missing the 4 billing fields it always used to drop for Prepaid.

---

When you're done:

```bash
curl -X POST http://127.0.0.1:8000/admin/screens/reset
```

to wipe the demo fields back to seed.
