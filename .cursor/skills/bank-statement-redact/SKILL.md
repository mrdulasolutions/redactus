---
name: bank-statement-redact
description: >-
  Interactively redacts bank-statement PDFs by keep-search. Always scans
  the specific statement first (layout map: header, columns, date style,
  quirks, masked PII) then one-shots from that map. Asks what to find,
  keeps matching transaction date/description/debit/credit, blanks every
  other transaction plus running balance, then asks about account number,
  routing, bank info, and similar fields. The user can add or remove keep
  terms, match indexes, PII fields, and extra strings at any time. Use
  when the user wants to blur, blank, black out, or redact a PDF bank
  statement, keep only certain merchants (for example Hostinger), or map
  a USAA/Chase-style layout before burning.
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
- **Scan before one-shot.** A new PDF is mapped first (`scan` writes
  `map.json`). Do not burn until `ready: yes`. This is the USAA-style
  layout pass: header, columns, date style, split-amount quirks,
  continuation merchants, masked fields.
- Treat the keep list, PII list, and extra blanks as **editable**. If the
  user adds or removes an item after preview or after a first PDF, update
  the flags and re-plan. Do not tell them they are stuck with the first pass.

## CLI

```bash
SKILL=".cursor/skills/bank-statement-redact"
REPO="$(cd "$SKILL/../../.." && pwd)"
PY="$SKILL/scripts/.venv/bin/python"
CLI="$PY -m redactus"

if [ ! -x "$PY" ]; then
  python3.12 -m venv "$SKILL/scripts/.venv"
fi
"$PY" -c "import redactus" 2>/dev/null || "$PY" -m pip install -e "$REPO"
```

Prefer `python3.12` (PyMuPDF wheels). Commands below assume `$CLI`
(`python -m redactus`). If the repo venv is already on PATH, `redactus`
is the same command.

## Workflow

Copy and track:

```
- [ ] 1. Locate the PDF
- [ ] 2. Scan / map this statement (required)
- [ ] 3. Ask what to search for (keep terms), unless already given
- [ ] 4. find on the scan extract; show matches with indexes
- [ ] 5. Ask the checklist using fields the map actually found
- [ ] 6. Offer add/remove
- [ ] 7. One-shot redact --map (or plan + preview + apply)
- [ ] 8. If they revise, re-plan from the same extract.json
- [ ] 9. Hand back the redacted PDF path
```

### 1. Locate the PDF

Use the file the user attached or named. If several statements, ask which.

### 2. Scan this statement

Do this **before** burning, on every new PDF. Same mapping pass we used
on USAA: learn the table, do not guess.

```bash
$CLI scan "$PDF" --workdir "$WORK" -o "$WORK/map.json"
# if they already named a keep term:
$CLI scan "$PDF" --keep Hostinger --workdir "$WORK" -o "$WORK/map.json"
```

Show the user the map, not the raw extract: profile, header, columns,
date style, quirks, transaction count, masked fields, sample rows.
If they passed `--keep`, also show match indexes.

Stop if `ready` is false or scan exits 2/4.

Reuse `$WORK/extract.json` and `$WORK/map.json` for find / plan / redact.
Do not re-scan unless the source PDF changed.

### 3. Ask what to search for

If they have not said the keep term, ask:

> What should stay visible on this statement? Name the merchant, payer,
> or text to search for (example: Hostinger). Everything else in the
> transaction list will be blanked. You can add or remove items after
> you see the matches.

Accept multiple terms. Repeatable `--keep` flags.

Kept matching rows always include **date, description, debit or credit**.
**Running balance on those rows is blanked** unless they pass
`--keep-running-balance`.

### 4. Find on the scan extract

Use the extract the scan already wrote. Do not re-extract.

```bash
$CLI find "$WORK/extract.json" --keep Hostinger --json
```

Show the user only: **index**, page, date, description, debit/credit of
matches, plus a count of non-matching rows that will be blanked.

If match_count is 0, ask for another term. Do not continue.

### 5. Standard field checklist

Ask once, as a list. Prefer the fields this map actually found
(`detected_fields` / `ask_pii` / `recommended_redact_pii` in map.json).
Fall back to [pii-fields.md](pii-fields.md) for anything the map missed.
Do not skip this step.

Always recommend **yes** for: account number, routing number, running
balance, beginning balance, ending balance, period totals. Those totals
would otherwise leak the redacted activity. If the map detected
`account_holder_name` or an address block, recommend those too: city and
name tokens from blanked rows otherwise survive in the header and fail
verify.

Also ask (no default yes unless the map or [pii-fields.md](pii-fields.md)
says so): bank name, bank address, account holder name, account holder
address, phone, email, member number, statement period.

Pass confirmed fields as a comma list:

`--redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals`

### 6. Add or remove items

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
$CLI plan "$WORK/extract.json" \
  --keep Hostinger --keep Cursor \
  --unkeep-index 84 \
  --also-redact Larnaka \
  --keep-columns date,description,debit,credit \
  --redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals \
  -o "$WORK/plan.json"
```

### 7. One-shot from the map

After the user has confirmed terms and fields, burn from the map. That
reuses the extract and will not re-guess the layout.

```bash
$CLI redact "$PDF" --map "$WORK/map.json" --keep Hostinger \
  --unkeep-index 84 \
  --also-redact Larnaka \
  --redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals \
  -o "$OUT" --workdir "$WORK"
```

If they want to see boxes first:

```bash
$CLI plan "$WORK/extract.json" \
  --keep Hostinger \
  --redact-pii account_number,routing_number,running_balance,beginning_balance,ending_balance,period_totals \
  -o "$WORK/plan.json"
$CLI preview "$PDF" "$WORK/plan.json" -o "$WORK/preview"
$CLI apply "$PDF" "$WORK/plan.json" -o "$OUT" --fill black --engine pymupdf
$CLI verify "$OUT" "$WORK/plan.json"
```

`verify` must exit 0. If it fails, do not deliver the file. Inspect
`missing_kept_terms` / `leaked_tokens` and fix the plan.

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
| scan / map | ~0.2–0.5 s |
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

If find returns 0 on a text PDF, inspect `header_row` in map.json.
It must look like `Date Description Debits Credits Balance`, not a
credit-card banner. Re-run `scan` if the source PDF changed.

## Example

User: "Redact this statement so only Hostinger is visible."

1. Scan / map the PDF. Show profile, header, quirks, ready.
2. Ask nothing extra for the keep term; they already said Hostinger.
3. Find hits (`HOSTINGER.COM`, `HOSTINGER *DOMAINS`) and show indexes.
4. Ask the checklist using fields the map found.
5. Offer add/remove.
6. One-shot `redact --map`. Blank other transactions, balances, chosen PII.
7. Hostinger rows keep date, description, and the debit; balance gone.
8. If they then say "also keep Apple, and blank Larnaka", re-plan with
   `--keep Hostinger --keep Apple --also-redact Larnaka`.

## Additional resources

- Field catalog and ask-script: [pii-fields.md](pii-fields.md)
