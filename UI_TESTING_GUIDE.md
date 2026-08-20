# Credit-Check Fields — Testing Guide for the UI Team

This explains the new "Postpaid Credit Check" behavior from your side (the
onboarding UI), and gives you copy-paste API calls to test your integration
against.

You don't need to know how the backend decides anything. You only need to
know two things:

1. **One rule your rendering code follows.**
2. **One required change to how you call an endpoint you already use.**

Everything else is just walking through the flow to confirm it works.

---

## The one rule: render whatever's in the list, in order

Every screen's fields come back as a plain array, e.g.:

```json
[
  { "fieldLabel": "Connection Type", "controlType": "dropdown", ... },
  { "fieldLabel": "Service Type", "controlType": "dropdown", ... }
]
```

**A field either is or isn't in that array.** There is no `"visible": true/false`
flag, no `"disabled"` state, nothing conditional for your code to evaluate.
If a field shouldn't show right now, it's simply **not sent to you**. If the
customer's next answer causes a new field to become relevant, it will show up
in the array the very next time you call the API — you don't need to detect
that yourself, request it, or guess.

**So: your rendering code should never contain any logic like "if Connection
Type is Postpaid, also show Credit Check Required."** Just render the array.
That logic already ran on the backend before the response reached you.

---

## The one required change: add `?audience=customer` to your GET calls

You already call:

```
GET /admin/screens/{screen_id}
```

to load a screen's fields. **From now on, always call it as:**

```
GET /admin/screens/{screen_id}?audience=customer
```

### Why this matters

That same endpoint is also used by the admin tool to review/manage fields —
including fields that aren't visible to anyone yet (e.g. right after an admin
adds a new conditional field, before any customer has triggered it). Without
`audience=customer`, you may occasionally get back extra internal fields or
metadata that were never meant for the customer-facing screen — it's not
guaranteed to be a "safe by default" response.

**`POST /preview/field-change` (the endpoint you call on every dropdown/radio
change) needs no change from you** — it always returns the customer-safe
shape automatically, no parameter needed.

---

## Walking through the Postpaid journey

Assume the server is running at `http://127.0.0.1:8000`. Every request below
is exactly what your UI should send at that point in a real customer session.

### 1. Load screen 2

```bash
curl "http://127.0.0.1:8000/admin/screens/screen_2?audience=customer"
```

You'll see the normal screen 2 fields (Connection Type, Service Type, etc.)
Nothing credit-check-related yet — the customer hasn't picked anything.

### 2. Customer selects Connection Type = Postpaid

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"1"}'
```

**Expected:** the returned `fields` array now includes a new field, **"Credit
Check Required"** (a Yes/No radio). Nothing else changed. `"1"` is the stored
value for Postpaid — you'd normally get this from the option the customer
clicked, not type it by hand.

> If the admin hasn't set this feature up yet on your test server, you won't
> see this field at all — that's expected too, it just means step 1's earlier
> setup hasn't happened. Ask whoever's running the backend to confirm the
> field exists via the admin chat first.

### 3. Customer answers Credit Check Required = Yes

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.creditCheckRequired","value":"YES"}'
```

**Expected:** in this same response, one of two things happens —
either a new **"Deposit Amount"** text field appears, **or** two new fields,
**"Bank Name"** and **"Bank Account Number"**, appear. You will never see
both, and you will never see neither (once this step happens, one branch
always fires).

This is the important one to double check: **you should never see a "Credit
Score" field anywhere in this response, or any other response, ever.** That
number gets computed and used internally, but the customer's screen never
receives it directly. If you ever see a field with a path containing
`creditScore`, something is misconfigured — flag it.

### 4. If Connection Type isn't Postpaid, nothing changes

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.connectionType","value":"2"}'
```

**Expected:** Credit Check Required never appears. The rest of the Prepaid
journey behaves exactly as it always has.

### 5. If Credit Check Required = No

```bash
curl -X POST http://127.0.0.1:8000/preview/field-change -H 'Content-Type: application/json' \
  -d '{"screen_id":"screen_2","path":"serviceDetails.creditCheckRequired","value":"NO"}'
```

**Expected:** none of Deposit Amount, Bank Name, or Bank Account Number ever
appear. The journey just continues as if this feature didn't exist.

---

## Something to know: the result in step 3 is random (for now)

Right now the actual credit-score check is a stand-in — a real score-lookup
service isn't wired up yet, so the backend picks a value in a configurable
range to decide which branch to show you. That means **running step 3
multiple times in a row may show you Deposit Amount sometimes and the bank
fields other times** — that's expected, not a bug. It's there so you can test
that your UI correctly renders *both* branches without needing a real credit
bureau.

If you need to force one branch specifically while building/testing your UI
(e.g. "I only want to see the Deposit Amount layout right now"), ask whoever
runs the backend to narrow the range for you — it's a one-line config change
on their end, no code change, and takes effect immediately without a restart.

---

## Quick checklist — is your integration correct?

- [ ] You call `GET /admin/screens/{screen_id}?audience=customer` (not without
      `audience`, not `audience=admin`).
- [ ] Your rendering code contains **no if/else logic** based on field names
      or values — it just maps over the array it receives.
- [ ] After Connection Type = Postpaid, you show Credit Check Required.
- [ ] After Credit Check Required = Yes, you show **either** Deposit Amount
      **or** Bank Name + Bank Account Number — whichever came back — never
      both, never neither.
- [ ] You never render or reference a "Credit Score" field anywhere.
- [ ] Prepaid customers, and customers who answer Credit Check Required = No,
      see no new fields at all — the journey looks identical to before this
      feature existed.

If all of these hold, your integration is done — there's nothing else to
wire up on your side for this feature.

---

## Quick reference

| You want to... | Call this |
|---|---|
| Load a screen | `GET /admin/screens/{screen_id}?audience=customer` |
| Report the customer picked/typed something | `POST /preview/field-change` with `{"screen_id", "path", "value"}` |
| Reset test data back to a clean state | `POST /admin/screens/reset` (wipes any admin-added test fields too — only use between test runs, never in production) |

`path` values you'll need for this feature specifically:

| Field | `path` |
|---|---|
| Connection Type | `serviceDetails.connectionType` (`"1"` = Postpaid, `"2"` = Prepaid) |
| Credit Check Required | `serviceDetails.creditCheckRequired` (`"YES"` / `"NO"`) |

You don't need a `path` for Deposit Amount or the bank fields — you only ever
*read* those from the response, you never POST a value for them yourself in
this flow.
