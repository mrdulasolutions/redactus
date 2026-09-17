---
name: bank-statement-redact
description: >-
  Interactively redacts bank-statement PDFs by keep-search. Asks what to
  find, keeps matching transaction date/description/debit/credit, blanks
  every other transaction plus running balance, then asks about account
  number, routing, bank info, addresses, and similar fields. The user can
  add or remove keep terms, match indexes, PII fields, and extra strings
  at any time and re-run. Use when the user wants to blur, blank, black
  out, or redact a PDF bank statement, keep only certain merchants (for
  example Hostinger), hide account and routing numbers, or revise a
  previous redaction.
---

# Bank statement keep-search redaction

True redaction: text is removed from the PDF content stream, not covered.
Do not "blur" pixels; blur is reversible. Default fill is black.

**Engine: PyMuPDF**, not JEV. `page.add_redact_annot` + `page.apply_redactions`
burns the boxes. There is no `jev` CLI on this machine. `--engine jev` is an
unwired stub; do not select it.

## Non-negotiable conversation rules

- Never paste full account numbers, routing numbers, SSNs, or non-kept
  transaction details into chat. Use last-4 masks from the extract.
- Ask before applying. Extract and find are read-only; apply is
  destructive on the copy you write.
- If find returns zero matches, stop. Do not redact.
- If `scanned_pages` is non-empty, stop and say the page is image-only.
  OCR is out of band for this skill.
- Treat the keep list, PII list, and extra blanks as **editable**. If the
  user adds or removes an item after preview or after a first PDF, update
  the flags and re-plan. Do not tell them they are stuck with the first pass.

## Script

```bash
SKILL=".cursor/skills/bank-statement-redact"
PY="$SKILL/scripts/.venv/bin/python"
SCRIPT="$SKILL/scripts/redact_statement.py"

if [ ! -x "$PY" ]; then
  python3.12 -m venv "$SKILL/scripts/.venv"
  "$SKILL/scripts/.venv/bin/pip" install -r "$SKILL/scripts/requirements.txt"
fi
```

Prefer `python3.12` (PyMuPDF wheels). Commands below assume `$PY` and `$SCRIPT`.

## Workflow

Copy and track:

```
- [ ] 1. Locate the PDF
- [ ] 2. Ask what to search for (keep terms)
- [ ] 3. extract + find; show matches with indexes
- [ ] 4. Ask the standard field checklist
- [ ] 5. Offer add/remove (keep, indexes, fields, extra text)
- [ ] 6. plan + preview; confirm boxes
- [ ] 7. apply + verify
- [ ] 8. If they revise, re-plan from the same extract.json
- [ ] 9. Hand back the redacted PDF path
```

### 1. Locate the PDF

Use the file the user attached or named. If several statements, ask which.

### 2. Ask what to search for

If they have not said the keep term, ask:

> What should stay visible on this statement? Name the merchant, payer,
> or text to search for (example: Hostinger). Everything else in the
> transaction list will be blanked. You can add or remove items after
> you see the matches.

Accept multiple terms. Repeatable `--keep` flags.

Kept matching rows always include **date, description, debit or credit**.
**Running balance on those rows is blanked** unless they pass
`--keep-running-balance`.

### 3. Extract and find

```bash
$PY $SCRIPT extract "$PDF" -o "$WORK/extract.json"
$PY $SCRIPT find "$WORK/extract.json" --keep Hostinger --json
```

Show the user only: **index**, page, date, description, debit/credit of
matches, plus a count of non-matching rows that will be blanked.

If match_count is 0, ask for another term. Do not continue.

### 4. Standard field checklist

Ask once, as a list. Recommended defaults are in
[pii-fields.md](pii-fields.md). Do not skip this step.

Always recommend **yes** for: account number, routing number, running
balance, beginning balance, ending balance, period totals. Those totals
would otherwise leak the redacted activity.

Also ask (no default yes unless [pii-fields.md](pii-fields.md) says so):
bank name, bank address, account holder name, account holder address,
phone, email, member number, statement period.

Pass confirmed fields as a comma list:

`--redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals`

### 5. Add or remove items

After matches and the checklist, and again after preview or a finished
PDF, ask:

> Add or remove anything before I burn the file?

Map their answer to flags. Re-run **plan** (and apply) on the same
`extract.json`. Do not re-extract unless the source PDF changed.

| User says | Flag change |
|---|---|
| Also keep Cursor / Apple | add `--keep Cursor` (repeatable) |
| Don't keep that one / blank index 84 | `--unkeep-index 84` |
| Also blank Larnaka, CONF#, a city | `--also-redact Larnaka` (repeatable; boxes that text even on kept rows) |
| Leave the bank name visible | drop `bank_name` from `--redact-pii` |
| Blank the statement period too | add `statement_period` to `--redact-pii` |
| Keep running balance on kept rows | `--keep-running-balance` |
| Keep another column | `--keep-columns date,description,debit,credit,balance` |

Keep a running list in the conversation and pass the **full current**
`--keep` / `--redact-pii` / `--also-redact` / `--unkeep-index` set every
re-plan. Removing an item means omitting it, not passing a negate flag
except `--unkeep-index` and `--keep-running-balance`.

```bash
$PY $SCRIPT plan "$WORK/extract.json" \
  --keep Hostinger --keep Cursor \
  --unkeep-index 84 \
  --also-redact Larnaka \
  --keep-columns date,description,debit,credit \
  --redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals \
  -o "$WORK/plan.json"
```

### 6. Plan and preview

```bash
$PY $SCRIPT preview "$PDF" "$WORK/plan.json" -o "$WORK/preview"
```

Read the preview PNGs. Confirm kept rows still show date/description/amount
and that other rows are boxed.

### 7. Apply and verify

```bash
$PY $SCRIPT apply "$PDF" "$WORK/plan.json" -o "$OUT" --fill black --engine pymupdf
$PY $SCRIPT verify "$OUT" "$WORK/plan.json"
```

`verify` must exit 0. If it fails, do not deliver the file. Inspect
`missing_kept_terms` / `leaked_tokens` and fix the plan.

After the user has confirmed terms and fields, this one-shot is allowed:

```bash
$PY $SCRIPT redact "$PDF" --keep Hostinger \
  --unkeep-index 84 \
  --also-redact Larnaka \
  --redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals \
  -o "$OUT" --workdir "$WORK"
```

### 8. Deliver

Give the output path. Mention it is a copy; the original is untouched.
Do not reopen and dump the redacted text into chat. Remind them they can
still add or remove items and you will re-plan.

## Keep vs blank (defaults)

| Region | Default |
|---|---|
| Matching row date, description, debit, credit | keep |
| Matching row running balance | blank |
| Every other transaction row | blank |
| Account number, routing, phone, email, SSN | ask; recommend blank |
| Bank name / bank address / holder name / address | ask |
| Beginning balance, ending balance, period totals | ask; recommend blank |

## Speed

Timed on the 14-page USAA statement (~177 transactions, 7 keep hits):

| Step | Wall time |
|---|---|
| extract | ~0.2–0.4 s |
| find | ~0.1 s |
| plan | ~0.1 s |
| apply (PyMuPDF burn) | ~0.2 s |
| verify | ~0.1 s |
| one-shot `redact` | ~0.3–0.7 s |

Revising keep/PII/extra text without re-extracting is plan + apply, about
**0.3 s**. The PDF is local; nothing is uploaded.

## Statement layouts

USAA Classic Checking (and similar) split one visual row across PDF
text objects: the `0` placeholder in Debits/Credits sits ~1.5pt above
the date line, dates are `MM/DD`, and the merchant (`hostinger.com`) is
a continuation under `DEBIT CARD PURCHASE`. The script clusters by
Y-midpoint so those still parse as one transaction.

If find returns 0 on a text PDF, inspect `header_row` in extract.json.
It must look like `Date Description Debits Credits Balance`, not a
credit-card banner.

## Example

User: "Redact this statement so only Hostinger is visible."

1. Ask nothing extra for the keep term; they already said Hostinger.
2. Find hits (`HOSTINGER.COM`, `HOSTINGER *DOMAINS`) and show indexes.
3. Ask the checklist.
4. Offer add/remove.
5. Blank other transactions, balances, and chosen PII.
6. Hostinger rows keep date, description, and the debit; balance gone.
7. If they then say "also keep Apple, and blank Larnaka", re-plan with
   `--keep Hostinger --keep Apple --also-redact Larnaka`.

## Additional resources

- Field catalog and ask-script: [pii-fields.md](pii-fields.md)
