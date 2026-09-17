# Standard fields to ask about

Ask this list after keep-term matches are confirmed. Show masked
previews from `extract.json` (`pii[].preview`, last-4). Never echo the
raw `label` line in chat if it still contains the full number.

Recommended default is **blank** unless noted.

| Field id | What it is | Recommend |
|---|---|---|
| `account_number` | Account / acct # | blank |
| `routing_number` | ABA / routing | blank |
| `bank_name` | Bank or credit union name | ask |
| `bank_address` | Bank street / city / ZIP | ask |
| `account_holder_name` | Customer name | blank if the scan detected it (name tokens leak from header into verify) |
| `account_holder_address` | Customer street / city / ZIP | blank if the scan detected an address block |
| `phone` | Bank or customer phone | blank |
| `email` | Email addresses | blank |
| `member_number` | Member / CIF number | blank |
| `ssn_itin` | SSN or ITIN | blank |
| `statement_period` | From / to dates in the header | ask |
| `beginning_balance` | Opening / previous balance | blank |
| `ending_balance` | Closing / new balance | blank |
| `period_totals` | Total debits / credits | blank |
| `running_balance` | Per-row balance column | blank (already on unless they keep it) |

`other_transactions` is implied by the keep search. The user can still
add keep terms, drop a match by index, or extra-blank a string; see the
add/remove table in SKILL.md.

## Ask script (use this wording)

> I found N matching transactions for "{term}". I will keep date,
> description, and the debit or credit on those rows, and blank the
> running balance plus every other transaction.
>
> Also blank these? Account number, routing number, beginning/ending
> balance, and period totals are recommended.
> - Account number (…1234)
> - Routing number (…0021)
> - Bank name
> - Bank address
> - Account holder name
> - Account holder address
> - Phone
> - Email
> - Member number
> - Statement period
>
> You can add or remove anything: another merchant to keep, a match
> index to blank, a field to leave visible, or extra text to black out.

Pass every **yes** as `--redact-pii` ids from the table.

Address hits may land in `pii[].field = address_block`. If they said yes
to bank address, holder address, or both, include `bank_address` and/or
`account_holder_address` in `--redact-pii` so those blocks are boxed.

## Why totals must go

Leaving "Ending Balance: $7,225.45" next to a $12.99 Hostinger debit
proves other activity. Same for beginning balance and period totals.
