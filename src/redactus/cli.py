#!/usr/bin/env python3
"""Bank-statement keep-search redaction.

True redaction (content removed from the PDF stream) via PyMuPDF.
Do not print full account numbers, routing numbers, or non-kept
transaction details to stdout in human mode; JSON plan files may
contain them for verify-only use and should stay on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pymupdf

from . import __version__

MONEY_RE = re.compile(
    r"^\$?\(?-?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?(?:\s*(?:CR|DR))?$|^\$?\d+\.\d{2}$|^0$"
)
MONTHS = (
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"^("
    rf"\d{{1,2}}[/-]\d{{1,2}}(?:[/-]\d{{2,4}})?"
    rf"|{MONTHS}\.?\s+\d{{1,2}},?(?:\s+\d{{2,4}})?"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf")$",
    re.I,
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]\d{3}[-.\s](?:\d{4}|[A-Z]{4}))(?!\d)"
)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
BARE_ZERO_RE = re.compile(r"^0$")

DEBIT_HEADERS = {"debit", "debits", "withdrawal", "withdrawals", "paid", "charges"}
CREDIT_HEADERS = {"credit", "credits", "deposit", "deposits"}
BALANCE_HEADERS = {"balance"}
AMOUNT_HEADERS = {"amount"}
DATE_HEADERS = {"date", "posted"}
DESC_HEADERS = {"description", "narrative", "particulars", "details", "memo"}

ACCOUNT_LABELS = (
    "account number",
    "account #",
    "acct #",
    "acct no",
    "account no",
    "acct nbr",
)
ROUTING_LABELS = (
    "routing number",
    "routing #",
    "routing no",
    "aba",
    "aba number",
    "rt number",
)
BANK_NAME_LABELS = ("bank name", "financial institution")
MEMBER_LABELS = ("member number", "member #", "member no")
HOLDER_LABELS = ("account holder", "account name", "customer name", "primary account holder")

SUMMARY_HINTS = (
    "beginning balance",
    "ending balance",
    "closing balance",
    "opening balance",
    "previous balance",
    "new balance",
    "total debit",
    "total credit",
    "total withdrawal",
    "total deposit",
    "total paid",
    "average daily",
    "service charge",
    "deposits/credits",
    "withdrawals/debits",
)

VERIFY_STOPWORDS = {
    "debit",
    "credit",
    "debits",
    "credits",
    "card",
    "purchase",
    "purch",
    "recurring",
    "withdrawal",
    "transfer",
    "funds",
    "checking",
    "payment",
    "receipt",
    "from",
    "beginning",
    "balance",
    "online",
    "mobile",
    "page",
    "description",
    "date",
    "conf",
    "store",
    "about",
    "dollar",
    "ending",
    "amount",
    "general",
}
LINE_Y_TOL = 3.2
CONTINUATION_GAP = 20.0
CHROME_SKIP = (
    r"^page\s+\d+\s+of\s+\d+",
    r"^online:",
    r"^mobile:",
    r"^phone:.*\b(usaa|tty)\b",
)

DEFAULT_KEEP_COLUMNS = ("date", "description", "debit", "credit")
DEFAULT_REDACT_ON_KEEP = ("running_balance",)
RECOMMENDED_PII = (
    "account_number",
    "routing_number",
    "phone",
    "email",
    "ssn_itin",
    "beginning_balance",
    "ending_balance",
    "period_totals",
    "running_balance",
)
ASK_PII = (
    "account_number",
    "routing_number",
    "bank_name",
    "bank_address",
    "account_holder_name",
    "account_holder_address",
    "phone",
    "email",
    "member_number",
    "ssn_itin",
    "statement_period",
    "beginning_balance",
    "ending_balance",
    "period_totals",
    "running_balance",
)

PAD_X = 1.6
PAD_Y = 0.35


@dataclass
class Word:
    page: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    line: int
    n: int

    @property
    def bbox(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    page: int
    words: list[Word]
    y0: float
    y1: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def bbox(self) -> list[float]:
        return [
            min(w.x0 for w in self.words),
            min(w.y0 for w in self.words),
            max(w.x1 for w in self.words),
            max(w.y1 for w in self.words),
        ]

    @property
    def y_mid(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Transaction:
    page: int
    lines: list[Line]
    date: str | None
    description: str
    debit: str | None
    credit: str | None
    balance: str | None
    keep: bool = False
    match_terms: list[str] = field(default_factory=list)

    @property
    def bbox(self) -> list[float]:
        boxes = [ln.bbox for ln in self.lines]
        return [
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        ]

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines)


@dataclass
class PiiHit:
    field: str
    page: int
    bbox: list[float]
    preview: str
    label: str | None = None


@dataclass
class Region:
    page: int
    bbox: list[float]
    reason: str
    field: str

    def padded(self, pad_x: float = PAD_X, pad_y: float = PAD_Y) -> list[float]:
        x0, y0, x1, y1 = self.bbox
        return [x0 - pad_x, y0 - pad_y, x1 + pad_x, y1 + pad_y]


def mask_secret(value: str, keep: int = 4) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) >= keep:
        return f"…{digits[-keep:]}"
    if len(value) <= 4:
        return "…"
    return value[:1] + "…" + value[-1:]


def is_money(text: str) -> bool:
    t = text.strip().replace(" ", "")
    if DATE_RE.match(t):
        return False
    return bool(MONEY_RE.match(t))


def is_date_token(text: str) -> bool:
    return bool(DATE_RE.match(text.strip()))


def line_starts_with_date(line: Line) -> bool:
    if not line.words:
        return False
    first = line.words[0].text
    if is_date_token(first):
        return True
    if len(line.words) >= 2 and is_date_token(f"{first} {line.words[1].text}"):
        return True
    return False


def union_bbox(boxes: Iterable[list[float]]) -> list[float]:
    boxes = list(boxes)
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def word_rect(words: list[Word]) -> list[float]:
    return union_bbox(w.bbox for w in words)


def extract_words(doc: pymupdf.Document) -> list[Word]:
    out: list[Word] = []
    for i, page in enumerate(doc):
        for x0, y0, x1, y1, text, block, line, n in page.get_text("words"):
            if not str(text).strip():
                continue
            out.append(
                Word(i, str(text), float(x0), float(y0), float(x1), float(y1), int(block), int(line), int(n))
            )
    return out


def group_lines(words: list[Word]) -> list[Line]:
    """Cluster by vertical midpoint so split PDF text objects still form one row."""
    lines: list[Line] = []
    for page in sorted({w.page for w in words}):
        page_words = sorted((w for w in words if w.page == page), key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
        buckets: list[list[Word]] = []
        for w in page_words:
            mid = (w.y0 + w.y1) / 2
            placed = False
            for bucket in reversed(buckets[-4:]):
                bmid = (min(x.y0 for x in bucket) + max(x.y1 for x in bucket)) / 2
                if abs(mid - bmid) <= LINE_Y_TOL:
                    bucket.append(w)
                    placed = True
                    break
            if not placed:
                buckets.append([w])
        for bucket in buckets:
            bucket.sort(key=lambda w: w.x0)
            lines.append(Line(page, bucket, min(w.y0 for w in bucket), max(w.y1 for w in bucket)))
    lines.sort(key=lambda ln: (ln.page, ln.y0, ln.words[0].x0))
    return lines


def token_key(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def find_header_row(lines: list[Line]) -> Line | None:
    best: tuple[int, int, Line] | None = None  # score, page, line
    for ln in lines:
        tokens = {token_key(w.text) for w in ln.words}
        if "ending" in tokens and "in" in tokens:
            continue
        if "credit" in tokens and "card" in tokens:
            continue
        score = 0
        if tokens & DATE_HEADERS:
            score += 3
        if tokens & DESC_HEADERS:
            score += 3
        if tokens & (DEBIT_HEADERS | CREDIT_HEADERS | AMOUNT_HEADERS):
            score += 2
        if tokens & BALANCE_HEADERS:
            score += 2
        if not (tokens & DATE_HEADERS and tokens & DESC_HEADERS):
            continue
        if score >= 6:
            if best is None or score > best[0] or (score == best[0] and ln.page < best[1]):
                best = (score, ln.page, ln)
    return None if best is None else best[2]


def is_column_header(line: Line) -> bool:
    tokens = {token_key(w.text) for w in line.words}
    return bool(tokens & DATE_HEADERS) and bool(tokens & DESC_HEADERS)


def is_chrome_line(line: Line) -> bool:
    t = line.text.strip().lower()
    return any(re.search(p, t) for p in CHROME_SKIP)


def column_bands(header: Line | None) -> dict[str, tuple[float, float]]:
    bands: dict[str, tuple[float, float]] = {}
    if header is None:
        return bands
    labeled: list[tuple[str, Word]] = []
    for w in header.words:
        key = token_key(w.text)
        if key in DATE_HEADERS:
            labeled.append(("date", w))
        elif key in DESC_HEADERS:
            labeled.append(("description", w))
        elif key in DEBIT_HEADERS:
            labeled.append(("debit", w))
        elif key in CREDIT_HEADERS:
            labeled.append(("credit", w))
        elif key in BALANCE_HEADERS:
            labeled.append(("balance", w))
        elif key in AMOUNT_HEADERS:
            labeled.append(("amount", w))
    labeled.sort(key=lambda item: item[1].x0)
    for i, (name, word) in enumerate(labeled):
        left = word.x0 - 8
        if i + 1 < len(labeled):
            right = (word.x1 + labeled[i + 1][1].x0) / 2
        else:
            right = word.x1 + 90
        bands[name] = (left, right)
    return bands


def is_placeholder_amount(text: str) -> bool:
    return bool(BARE_ZERO_RE.match(text.strip()))


def money_words(line: Line) -> list[Word]:
    return [w for w in line.words if is_money(w.text)]


def assign_amounts(line: Line, bands: dict[str, tuple[float, float]]) -> dict[str, Word]:
    found: dict[str, Word] = {}
    candidates = money_words(line)
    for w in candidates:
        placed = False
        for name in ("debit", "credit", "amount", "balance"):
            if name not in bands:
                continue
            left, right = bands[name]
            if left <= w.cx <= right:
                found[name] = w
                placed = True
                break
        if not placed and bands:
            # Rightmost dollar amount is almost always running balance.
            continue
        if not placed:
            continue
    if "amount" in found and "debit" not in found and "credit" not in found:
        found["debit"] = found["amount"]
    real_money = [w for w in candidates if not is_placeholder_amount(w.text)]
    if "balance" not in found and len(real_money) >= 2:
        found["balance"] = max(real_money, key=lambda w: w.x0)
    elif "balance" not in found and bands.get("balance") is None and len(real_money) >= 2:
        found["balance"] = max(real_money, key=lambda w: w.x0)
    if not bands and real_money:
        ordered = sorted(real_money, key=lambda w: w.x0)
        if len(ordered) == 1:
            found.setdefault("debit", ordered[0])
        elif len(ordered) >= 2:
            found["balance"] = ordered[-1]
            found.setdefault("debit", ordered[-2])
    return found


def stored_amount(word: Word | None) -> str | None:
    if word is None or is_placeholder_amount(word.text):
        return None
    return word.text


def description_from(line: Line, bands: dict[str, tuple[float, float]], amounts: dict[str, Word]) -> str:
    skip = list(amounts.values())
    desc_band = bands.get("description")
    debit_left = (bands.get("debit") or bands.get("amount") or (9999.0, 0.0))[0]
    parts: list[str] = []
    for w in line.words:
        if w in skip:
            continue
        if is_date_token(w.text):
            continue
        if is_placeholder_amount(w.text):
            continue
        if is_money(w.text) and w.x0 >= debit_left - 10:
            continue
        if desc_band:
            left, right = desc_band
            if not (left - 24 <= w.cx <= right + 48) and w.x0 >= debit_left - 10:
                continue
        parts.append(w.text)
    return " ".join(parts).strip()


def is_summary_line(line: Line) -> bool:
    t = line.text.lower()
    return any(h in t for h in SUMMARY_HINTS)


def is_amount_only_line(line: Line) -> bool:
    if not line.words or line_starts_with_date(line):
        return False
    return all(is_money(w.text) or is_placeholder_amount(w.text) for w in line.words)


def attach_split_amount_rows(lines: list[Line]) -> list[Line]:
    """USAA draws Debits/Credits `0` ~1.5pt above the date row. Glue those back."""
    out: list[Line] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if (
            nxt is not None
            and is_amount_only_line(ln)
            and nxt.page == ln.page
            and line_starts_with_date(nxt)
            and nxt.y0 - ln.y1 < 8
        ):
            words = sorted(ln.words + nxt.words, key=lambda w: w.x0)
            out.append(Line(nxt.page, words, min(ln.y0, nxt.y0), max(ln.y1, nxt.y1)))
            i += 2
            continue
        out.append(ln)
        i += 1
    return out


def detect_transactions(lines: list[Line], bands: dict[str, tuple[float, float]]) -> list[Transaction]:
    txs: list[Transaction] = []
    current: Transaction | None = None
    for ln in lines:
        if is_column_header(ln) or is_chrome_line(ln):
            current = None
            continue
        if is_summary_line(ln) and not line_starts_with_date(ln):
            current = None
            continue
        if line_starts_with_date(ln):
            amounts = assign_amounts(ln, bands)
            date = ln.words[0].text
            if len(ln.words) >= 2 and is_date_token(f"{ln.words[0].text} {ln.words[1].text}"):
                date = f"{ln.words[0].text} {ln.words[1].text}"
            current = Transaction(
                page=ln.page,
                lines=[ln],
                date=date,
                description=description_from(ln, bands, amounts),
                debit=stored_amount(amounts.get("debit")),
                credit=stored_amount(amounts.get("credit")),
                balance=stored_amount(amounts.get("balance")),
            )
            txs.append(current)
        elif (
            current is not None
            and ln.page == current.page
            and ln.y0 - current.lines[-1].y1 < CONTINUATION_GAP
            and not is_amount_only_line(ln)
        ):
            current.lines.append(ln)
            extra = description_from(ln, bands, assign_amounts(ln, bands))
            if extra:
                current.description = f"{current.description} {extra}".strip()
            amounts = assign_amounts(ln, bands)
            if stored_amount(amounts.get("debit")) and not current.debit:
                current.debit = stored_amount(amounts.get("debit"))
            if stored_amount(amounts.get("credit")) and not current.credit:
                current.credit = stored_amount(amounts.get("credit"))
            if stored_amount(amounts.get("balance")):
                current.balance = stored_amount(amounts.get("balance"))
        else:
            current = None
    return txs


def line_contains_label(line: Line, labels: tuple[str, ...]) -> bool:
    t = re.sub(r"\s+", " ", line.text.lower())
    return any(label in t for label in labels)


def value_words_after_label(line: Line, labels: tuple[str, ...]) -> list[Word]:
    tokens = [w.text.lower().strip(":#") for w in line.words]
    for label in labels:
        parts = label.split()
        for i in range(len(tokens) - len(parts) + 1):
            window = " ".join(tokens[i : i + len(parts)])
            if window == label or window.rstrip(":#") == label:
                return line.words[i + len(parts) :]
    return []


def detect_label_values(lines: list[Line], field: str, labels: tuple[str, ...]) -> list[PiiHit]:
    hits: list[PiiHit] = []
    for ln in lines:
        if not line_contains_label(ln, labels):
            continue
        values = [w for w in value_words_after_label(ln, labels) if re.search(r"[\dA-Za-z]", w.text)]
        # Drop the label words themselves.
        values = [w for w in values if w.text.lower().strip(":#") not in " ".join(labels)]
        if not values:
            # numeric tokens on the same line
            values = [w for w in ln.words if re.search(r"\d", w.text) and w.text.lower() not in {"account", "number", "routing"}]
        if not values:
            continue
        raw = " ".join(w.text for w in values)
        hits.append(
            PiiHit(
                field=field,
                page=ln.page,
                bbox=word_rect(values),
                preview=mask_secret(raw),
                label=ln.text,
            )
        )
    return hits


def looks_like_address_line(line: Line) -> bool:
    t = line.text
    if ZIP_RE.search(t) and re.search(r"\b[A-Z]{2}\b", t):
        return True
    if re.search(r"\b(st|street|ave|avenue|rd|road|blvd|suite|ste|po box)\b", t, re.I):
        return True
    return False


def summary_amount_words(lines: list[Line], index: int) -> list[Word]:
    ln = lines[index]
    moneys = [w for w in money_words(ln) if not is_placeholder_amount(w.text)]
    if moneys:
        return moneys
    if index + 1 < len(lines):
        nxt = lines[index + 1]
        if nxt.page == ln.page and nxt.y0 - ln.y1 < 18:
            nxt_money = [w for w in money_words(nxt) if not is_placeholder_amount(w.text)]
            if nxt_money and len(nxt.words) <= 4:
                return nxt_money
    return []


def looks_like_person_name(text: str) -> bool:
    if re.search(r"\d", text):
        return False
    banned = ("bank", "usaa", "checking", "savings", "federal", "classic", "statement", "account", "page", "online", "phone", "mobile", "activity", "transaction")
    low = text.lower()
    if any(b in low for b in banned):
        return False
    parts = [p for p in re.split(r"\s+", text.strip()) if p]
    if not 2 <= len(parts) <= 5:
        return False
    return all(re.fullmatch(r"[A-Z][A-Za-z'.-]*", p) or re.fullmatch(r"[A-Z]\.?", p) for p in parts)


def detect_pii(lines: list[Line], words: list[Word], txs: list[Transaction], page_heights: dict[int, float] | None = None) -> list[PiiHit]:
    hits: list[PiiHit] = []
    hits.extend(detect_label_values(lines, "account_number", ACCOUNT_LABELS))
    hits.extend(detect_label_values(lines, "routing_number", ROUTING_LABELS))
    hits.extend(detect_label_values(lines, "member_number", MEMBER_LABELS))
    hits.extend(detect_label_values(lines, "account_holder_name", HOLDER_LABELS))
    hits.extend(detect_label_values(lines, "bank_name", BANK_NAME_LABELS))

    for w in words:
        if EMAIL_RE.fullmatch(w.text) or EMAIL_RE.search(w.text):
            hits.append(PiiHit("email", w.page, w.bbox, mask_secret(w.text)))
        if SSN_RE.search(w.text):
            hits.append(PiiHit("ssn_itin", w.page, w.bbox, mask_secret(w.text)))
        if PHONE_RE.search(w.text) and not is_money(w.text):
            hits.append(PiiHit("phone", w.page, w.bbox, mask_secret(w.text)))

    first_tx_y = txs[0].bbox[1] if txs else None
    first_tx_page = txs[0].page if txs else None
    header_lines = [
        ln
        for ln in lines
        if first_tx_y is None or ln.page < first_tx_page or (ln.page == first_tx_page and ln.y1 < first_tx_y - 8)
    ]

    for ln in header_lines:
        if re.search(r"\b(bank|credit union|federal savings)\b", ln.text, re.I):
            hits.append(PiiHit("bank_name", ln.page, ln.bbox, ln.text[:24], ln.text))
        if looks_like_address_line(ln):
            hits.append(
                PiiHit("address_block", ln.page, ln.bbox, ln.text[:24] + ("…" if len(ln.text) > 24 else ""), ln.text)
            )
        if looks_like_person_name(ln.text):
            hits.append(PiiHit("account_holder_name", ln.page, ln.bbox, mask_secret(ln.text), ln.text))

    heights = page_heights or {}
    for w in words:
        bottom = heights.get(w.page)
        if not bottom or w.y0 < bottom * 0.90:
            continue
        digits = re.sub(r"\D", "", w.text)
        if len(digits) >= 8 and digits.isdigit() and not is_money(w.text):
            hits.append(PiiHit("account_number", w.page, w.bbox, mask_secret(w.text), "footer"))

    for i, ln in enumerate(lines):
        low = ln.text.lower()
        if "beginning balance" in low or "opening balance" in low or "previous balance" in low:
            for w in summary_amount_words(lines, i):
                hits.append(PiiHit("beginning_balance", ln.page, w.bbox, mask_secret(w.text), "beginning_balance"))
        if "ending balance" in low or "closing balance" in low or "new balance" in low:
            for w in summary_amount_words(lines, i):
                hits.append(PiiHit("ending_balance", ln.page, w.bbox, mask_secret(w.text), "ending_balance"))
        if any(k in low for k in ("total debit", "total credit", "total withdrawal", "total deposit", "total paid", "deposits/credits", "withdrawals/debits")):
            for w in summary_amount_words(lines, i):
                hits.append(PiiHit("period_totals", ln.page, w.bbox, mask_secret(w.text), "period_totals"))
        if "statement period" in low or "period ending" in low or "period:" in low:
            values = [w for w in ln.words if is_date_token(w.text) or w.text.lower() in {"-", "to", "through"}]
            if values:
                hits.append(PiiHit("statement_period", ln.page, word_rect(values), "period dates", ln.text))

    return hits


def match_terms(tx: Transaction, terms: list[str], regexes: list[str]) -> list[str]:
    hay = tx.text
    hits: list[str] = []
    for term in terms:
        if term.lower() in hay.lower():
            hits.append(term)
    for pattern in regexes:
        if re.search(pattern, hay, re.I):
            hits.append(pattern)
    return hits


def extract_payload(pdf_path: Path) -> dict[str, Any]:
    doc = pymupdf.open(pdf_path)
    try:
        words = extract_words(doc)
        lines = attach_split_amount_rows(group_lines(words))
        header = find_header_row(lines)
        bands = column_bands(header)
        txs = detect_transactions(lines, bands)
        page_heights = {i: float(page.rect.height) for i, page in enumerate(doc)}
        pii = detect_pii(lines, words, txs, page_heights)
        scanned = []
        for i, page in enumerate(doc):
            page_words = [w for w in words if w.page == i]
            if not page_words:
                continue
            if len(page_words) < 20 and page.get_images():
                scanned.append(i)
        return {
            "source": str(pdf_path),
            "pages": doc.page_count,
            "scanned_pages": scanned,
            "columns": {k: list(v) for k, v in bands.items()},
            "header_row": header.text if header else None,
            "header_bbox": header.bbox if header else None,
            "header_bboxes": [{"page": ln.page, "bbox": ln.bbox} for ln in lines if is_column_header(ln)],
            "transactions": [
                {
                    "index": i,
                    "page": t.page,
                    "date": t.date,
                    "description": t.description,
                    "debit": t.debit,
                    "credit": t.credit,
                    "balance": t.balance,
                    "bbox": t.bbox,
                    "text": t.text,
                    "line_bboxes": [ln.bbox for ln in t.lines],
                    "words": [asdict(w) for ln in t.lines for w in ln.words],
                }
                for i, t in enumerate(txs)
            ],
            "pii": [asdict(h) for h in pii],
            "header_lines": [
                {"page": ln.page, "text": ln.text, "bbox": ln.bbox}
                for ln in lines
                if not txs or ln.page < txs[0].page or (ln.page == txs[0].page and ln.y1 < txs[0].bbox[1] - 8)
            ],
        }
    finally:
        doc.close()


def _date_style(dates: list[str]) -> str:
    mmdd = sum(1 for d in dates if re.fullmatch(r"\d{1,2}/\d{1,2}", d or ""))
    ymd = sum(1 for d in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""))
    mdY = sum(1 for d in dates if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", d or ""))
    if mmdd >= mdY and mmdd >= ymd and mmdd:
        return "MM/DD"
    if mdY >= ymd and mdY:
        return "MM/DD/YYYY"
    if ymd:
        return "YYYY-MM-DD"
    return "unknown"


def _scrub_preview(text: str, width: int = 56) -> str:
    cleaned = re.sub(r"\d{5,}", "#####", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:width]


def build_statement_map(payload: dict[str, Any], extract_path: str | None = None) -> dict[str, Any]:
    """Layout fingerprint for this specific PDF. Masked. Safe to show the user."""
    txs = list(payload.get("transactions") or [])
    pii = list(payload.get("pii") or [])
    header = payload.get("header_row") or ""
    columns = list((payload.get("columns") or {}).keys())
    dates = [str(t.get("date") or "") for t in txs[:30]]
    date_style = _date_style(dates)
    quirks: list[str] = []
    if txs:
        multi = sum(1 for t in txs if len(t.get("line_bboxes") or []) > 1)
        if multi / len(txs) >= 0.25:
            quirks.append("continuation_merchants")
        zero_rows = 0
        for t in txs[:50]:
            if any(str(w.get("text") or "") == "0" for w in t.get("words") or []):
                zero_rows += 1
        if zero_rows >= 8 and "debit" in columns and "credit" in columns:
            quirks.append("split_amount_placeholders")
    if any((p.get("label") or "") == "footer" for p in pii):
        quirks.append("footer_account_digits")
    if date_style == "MM/DD":
        quirks.append("mm_dd_dates")
    header_l = header.lower()
    if "debits" in header_l and "credits" in header_l and date_style == "MM/DD":
        profile = "usaa_like"
    elif "date" in header_l and "description" in header_l:
        profile = "generic_table"
    else:
        profile = "unknown"
    by_field: dict[str, dict[str, Any]] = {}
    for hit in pii:
        field = hit.get("field") or "unknown"
        bucket = by_field.setdefault(field, {"count": 0, "previews": []})
        bucket["count"] += 1
        prev = hit.get("preview")
        if prev and prev not in bucket["previews"] and len(bucket["previews"]) < 3:
            bucket["previews"].append(prev)
    detected_fields = sorted(by_field)
    recommended = [f for f in RECOMMENDED_PII if f in detected_fields or f == "running_balance"]
    if "running_balance" not in recommended:
        recommended.append("running_balance")
    if "account_holder_name" in detected_fields and "account_holder_name" not in recommended:
        recommended.append("account_holder_name")
    if "address_block" in detected_fields or "account_holder_address" in detected_fields:
        if "account_holder_address" not in recommended:
            recommended.append("account_holder_address")
    asked = [f for f in ASK_PII if f in detected_fields or f == "running_balance"]
    n = len(txs)
    with_amount = sum(1 for t in txs if t.get("debit") or t.get("credit"))
    with_balance = sum(1 for t in txs if t.get("balance"))
    scanned = list(payload.get("scanned_pages") or [])
    ready = bool(
        profile != "unknown"
        and n > 0
        and "date" in columns
        and ({"debit", "credit", "amount"} & set(columns))
        and not scanned
    )
    samples = []
    for t in txs[:4]:
        samples.append(
            {
                "index": t.get("index"),
                "page": (t.get("page") or 0) + 1,
                "date": t.get("date"),
                "has_description": bool((t.get("description") or "").strip()),
                "has_debit": bool(t.get("debit")),
                "has_credit": bool(t.get("credit")),
                "has_balance": bool(t.get("balance")),
                "line_count": len(t.get("line_bboxes") or []),
            }
        )
    bank_guess = None
    for line in payload.get("header_lines") or []:
        text = str(line.get("text") or "")
        if re.search(r"\b(usaa|chase|bank of america|wells fargo|capital one)\b", text, re.I):
            bank_guess = re.search(
                r"\b(USAA(?: Federal Savings Bank)?|Chase|Bank of America|Wells Fargo|Capital One)\b",
                text,
                re.I,
            )
            bank_guess = bank_guess.group(0) if bank_guess else "labeled"
            break
        if re.search(r"\b(federal savings bank|credit union)\b", text, re.I) and not re.search(
            r"account holder", text, re.I
        ):
            bank_guess = "labeled"
            break
    return {
        "source": payload.get("source"),
        "extract_path": extract_path,
        "pages": payload.get("pages"),
        "profile": profile,
        "bank_guess": bank_guess,
        "header_row": header,
        "columns": columns,
        "date_style": date_style,
        "quirks": quirks,
        "transactions": n,
        "with_debit_or_credit": with_amount,
        "with_balance": with_balance,
        "scanned_pages": scanned,
        "ready": ready,
        "fields": by_field,
        "detected_fields": detected_fields,
        "recommended_redact_pii": recommended,
        "ask_pii": asked,
        "sample_rows": samples,
        "engine": "pymupdf",
    }


def print_map_human(mp: dict[str, Any]) -> None:
    ready = "yes" if mp.get("ready") else "NO — inspect header_row / scanned_pages"
    print("Statement map")
    print(f"  profile:     {mp.get('profile')}")
    print(f"  bank:        {mp.get('bank_guess') or 'unlabeled'}")
    print(f"  pages:       {mp.get('pages')}   transactions: {mp.get('transactions')}")
    print(f"  header:      {mp.get('header_row')}")
    print(f"  columns:     {', '.join(mp.get('columns') or [])}")
    print(f"  date style:  {mp.get('date_style')}")
    print(f"  quirks:      {', '.join(mp.get('quirks') or []) or 'none'}")
    print(f"  amounts:     {mp.get('with_debit_or_credit')}/{mp.get('transactions')} debit|credit  "
          f"{mp.get('with_balance')}/{mp.get('transactions')} balance")
    print(f"  ready:       {ready}")
    print("  fields (masked):")
    sensitive_preview = {
        "address_block",
        "account_holder_name",
        "account_holder_address",
        "bank_address",
        "email",
    }
    for field, info in sorted((mp.get("fields") or {}).items()):
        if field in sensitive_preview:
            print(f"    {field}: {info.get('count')}")
        else:
            print(f"    {field}: {info.get('count')}  {', '.join(info.get('previews') or [])}")
    print("  sample rows (structure only):")
    for row in mp.get("sample_rows") or []:
        bits = []
        if row.get("has_description"):
            bits.append("desc")
        if row.get("has_debit"):
            bits.append("debit")
        if row.get("has_credit"):
            bits.append("credit")
        if row.get("has_balance"):
            bits.append("balance")
        print(
            f"    [{row.get('index')}] p{row.get('page')}  {row.get('date')}  "
            f"{'+'.join(bits) or 'empty'}  lines={row.get('line_count')}"
        )
    print(f"  recommended --redact-pii {','.join(mp.get('recommended_redact_pii') or [])}")
    if mp.get("scanned_pages"):
        print(f"  STOP: image-only pages {[p + 1 for p in mp['scanned_pages']]}")


def tx_from_payload(item: dict[str, Any]) -> Transaction:
    rebuilt: list[Word] = []
    for raw in item.get("words") or []:
        rebuilt.append(Word(**raw))
    if rebuilt:
        lines = attach_split_amount_rows(group_lines(rebuilt))
    else:
        lines = []
        for bbox in item.get("line_bboxes") or [item["bbox"]]:
            dummy = Word(item["page"], "", *bbox, 0, 0, 0)
            lines.append(Line(item["page"], [dummy], bbox[1], bbox[3]))
    return Transaction(
        page=item["page"],
        lines=lines,
        date=item.get("date"),
        description=item.get("description") or "",
        debit=item.get("debit"),
        credit=item.get("credit"),
        balance=item.get("balance"),
    )


def regions_for_transaction(tx: Transaction, keep: bool, keep_columns: set[str], bands: dict[str, tuple[float, float]]) -> list[Region]:
    if not keep:
        return [Region(tx.page, tx.bbox, "non_matching_transaction", "other_transactions")]
    regions: list[Region] = []
    drop_balance = "running_balance" not in keep_columns and "balance" not in keep_columns
    for ln in tx.lines:
        amounts = assign_amounts(ln, bands)
        if drop_balance:
            bal = amounts.get("balance")
            if bal:
                regions.append(Region(tx.page, bal.bbox, "kept_row_balance", "running_balance"))
            elif len(money_words(ln)) >= 2:
                rightmost = max(money_words(ln), key=lambda w: w.x0)
                if rightmost is not amounts.get("debit") and rightmost is not amounts.get("credit"):
                    regions.append(Region(tx.page, rightmost.bbox, "kept_row_balance", "running_balance"))
        for name, word in amounts.items():
            mapped = {"debit": "debit", "credit": "credit", "amount": "debit", "balance": "running_balance"}[name]
            keep_name = "debit" if mapped == "debit" else "credit" if mapped == "credit" else "running_balance"
            if keep_name not in keep_columns and not (mapped == "running_balance" and drop_balance):
                regions.append(Region(tx.page, word.bbox, f"kept_row_{name}", mapped))
    return regions


def merge_regions(regions: list[Region]) -> list[Region]:
    """Merge overlapping boxes on the same line only. Never merge across rows."""
    by_page: dict[int, list[Region]] = {}
    for r in regions:
        by_page.setdefault(r.page, []).append(Region(r.page, r.padded(), r.reason, r.field))
    merged: list[Region] = []
    for page, group in by_page.items():
        boxes = group[:]
        changed = True
        while changed:
            changed = False
            out: list[Region] = []
            for r in boxes:
                hit = None
                for i, o in enumerate(out):
                    if _same_line(r.bbox, o.bbox) and _overlaps(r.bbox, o.bbox, gap=0.5):
                        hit = i
                        break
                if hit is None:
                    out.append(r)
                else:
                    o = out[hit]
                    out[hit] = Region(
                        page,
                        union_bbox([r.bbox, o.bbox]),
                        f"{o.reason}+{r.reason}",
                        o.field if o.field == r.field else "mixed",
                    )
                    changed = True
            boxes = out
        merged.extend(boxes)
    return merged


def _same_line(a: list[float], b: list[float], tol: float = 3.5) -> bool:
    return abs(((a[1] + a[3]) / 2) - ((b[1] + b[3]) / 2)) <= tol


def _overlaps(a: list[float], b: list[float], gap: float = 2.0) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _center(bbox: list[float]) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _contains_point(bbox: list[float], point: tuple[float, float], inset: float = 0.8) -> bool:
    x, y = point
    return bbox[0] + inset <= x <= bbox[2] - inset and bbox[1] + inset <= y <= bbox[3] - inset


def drop_regions_hitting_protect(
    regions: list[Region], protect: list[tuple[int, list[float]]]
) -> list[Region]:
    kept: list[Region] = []
    for r in regions:
        if any(page == r.page and _contains_point(r.bbox, _center(bbox)) for page, bbox in protect):
            continue
        kept.append(r)
    return kept


def build_plan(
    payload: dict[str, Any],
    keep_terms: list[str],
    keep_regexes: list[str],
    redact_pii: list[str],
    keep_columns: list[str],
    unkeep_indexes: set[int] | None = None,
    also_redact: list[str] | None = None,
    also_redact_re: list[str] | None = None,
) -> dict[str, Any]:
    bands = {k: tuple(v) for k, v in (payload.get("columns") or {}).items()}
    keep_col_set = set(keep_columns)
    skip_idx = unkeep_indexes or set()
    extra_terms = also_redact or []
    extra_re = also_redact_re or []
    regions: list[Region] = []
    matches: list[dict[str, Any]] = []
    must_die: list[str] = []
    must_survive: list[str] = list(keep_terms)

    txs = list(payload.get("transactions") or [])
    if not txs:
        raise SystemExit("No transactions detected. Stop and inspect the PDF (scanned/image-only pages need OCR).")

    kept_count = 0
    protect: list[tuple[int, list[float]]] = []
    for item in txs:
        tx = tx_from_payload(item)
        hits = match_terms(tx, keep_terms, keep_regexes)
        keep = bool(hits) and item["index"] not in skip_idx
        if keep:
            kept_count += 1
            matches.append(
                {
                    "index": item["index"],
                    "page": tx.page + 1,
                    "date": tx.date,
                    "description": tx.description,
                    "debit": tx.debit,
                    "credit": tx.credit,
                    "terms": hits,
                }
            )
            for ln in tx.lines:
                amounts = assign_amounts(ln, bands)
                bal = amounts.get("balance")
                for w in ln.words:
                    if bal is not None and w.bbox == bal.bbox:
                        continue
                    protect.append((tx.page, w.bbox))
        else:
            keep_l = {t.lower() for t in keep_terms}
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", tx.description):
                low = token.lower()
                if low in VERIFY_STOPWORDS or low in keep_l:
                    continue
                if token.isupper() and len(token) <= 4:
                    continue
                must_die.append(token)
                break
        regions.extend(regions_for_transaction(tx, keep, keep_col_set, bands))

    if payload.get("header_bbox"):
        header_page = 0
        boxes = payload.get("header_bboxes") or []
        if boxes and isinstance(boxes[0], dict):
            header_page = int(boxes[0].get("page") or 0)
        protect.append((header_page, payload["header_bbox"]))
    for item in payload.get("header_bboxes") or []:
        if isinstance(item, dict):
            protect.append((int(item["page"]), item["bbox"]))
        else:
            protect.append((0, item))

    if kept_count == 0:
        raise SystemExit(
            f"No transactions matched {keep_terms or keep_regexes}. "
            "Do not redact. Ask the user for another search term."
        )

    pii_fields = set(redact_pii)
    # running_balance on kept rows is handled in regions_for_transaction
    for hit in payload.get("pii") or []:
        field = hit["field"]
        mapped = field
        if field == "address_block":
            if "bank_address" in pii_fields or "account_holder_address" in pii_fields:
                mapped = "bank_address" if "bank_address" in pii_fields else "account_holder_address"
            else:
                continue
        if mapped not in pii_fields and field not in pii_fields:
            continue
        regions.append(Region(hit["page"], hit["bbox"], f"pii_{field}", field))

    extra_regions = regions_for_also_redact(payload, extra_terms, extra_re)
    if extra_regions:
        protect = [
            (page, bbox)
            for page, bbox in protect
            if not any(page == r.page and _contains_point(bbox, _center(r.bbox)) for r in extra_regions)
        ]
        regions.extend(extra_regions)
        must_die.extend(extra_terms)

    merged = drop_regions_hitting_protect(merge_regions(regions), protect)
    return {
        "source": payload.get("source"),
        "keep_terms": keep_terms,
        "keep_regexes": keep_regexes,
        "keep_columns": keep_columns,
        "redact_pii": redact_pii,
        "unkeep_indexes": sorted(skip_idx),
        "also_redact": extra_terms,
        "also_redact_re": extra_re,
        "match_count": kept_count,
        "transaction_count": len(txs),
        "matches": matches,
        "must_survive": must_survive,
        "must_die": sorted({t for t in must_die if t}),
        "regions": [
            {"page": r.page, "bbox": r.bbox, "reason": r.reason, "field": r.field} for r in merged
        ],
    }


def apply_pymupdf(pdf_path: Path, plan: dict[str, Any], output_path: Path, fill: tuple[float, float, float]) -> None:
    doc = pymupdf.open(pdf_path)
    try:
        by_page: dict[int, list[list[float]]] = {}
        for region in plan["regions"]:
            by_page.setdefault(region["page"], []).append(region["bbox"])
        for i, page in enumerate(doc):
            for bbox in by_page.get(i, []):
                rect = pymupdf.Rect(bbox)
                page.add_redact_annot(rect, fill=fill, cross_out=False)
            if i in by_page:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
        meta = {k: "" for k in (doc.metadata or {})}
        doc.set_metadata(meta)
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output_path, garbage=4, deflate=True)
    finally:
        doc.close()


def apply_jev(pdf_path: Path, plan: dict[str, Any], output_path: Path) -> None:
    """Optional external apply backend if a `jev` CLI is on PATH."""
    jev = shutil.which("jev")
    if not jev:
        raise SystemExit("jev is not on PATH. Install it or omit --engine jev to use PyMuPDF.")
    boxes_path = output_path.with_suffix(".jev-boxes.json")
    boxes_path.write_text(json.dumps(plan["regions"], indent=2), encoding="utf-8")
    raise SystemExit(
        f"jev found at {jev}, but its CLI contract is unknown in this workspace. "
        f"Wrote region boxes to {boxes_path}. Use --engine pymupdf, or wire jev here once the flags are known."
    )


def verify_output(output_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    doc = pymupdf.open(output_path)
    try:
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    missing = [t for t in plan.get("must_survive") or [] if t.lower() not in text.lower()]
    leaked = [t for t in plan.get("must_die") or [] if t and t.lower() in text.lower()]
    ok = not missing and not leaked
    return {"ok": ok, "missing_kept_terms": missing, "leaked_tokens": leaked}


def render_preview(pdf_path: Path, plan: dict[str, Any], out_dir: Path) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    src = pymupdf.open(pdf_path)
    paths: list[str] = []
    try:
        for i, page in enumerate(src):
            tmp = pymupdf.open()
            overlay = tmp.new_page(width=page.rect.width, height=page.rect.height)
            overlay.show_pdf_page(page.rect, src, i)
            shape = overlay.new_shape()
            drew = False
            for region in plan["regions"]:
                if region["page"] != i:
                    continue
                shape.draw_rect(pymupdf.Rect(region["bbox"]))
                drew = True
            if drew:
                shape.finish(color=(0.85, 0.05, 0.05), fill=(0.85, 0.05, 0.05), fill_opacity=0.32, width=0.5)
                shape.commit()
            pix = overlay.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
            dest = out_dir / f"page-{i + 1:02d}.png"
            pix.save(dest)
            paths.append(str(dest))
            tmp.close()
    finally:
        src.close()
    return paths


def make_sample(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    font = "courier"
    def put(x, y, text, size=10):
        page.insert_text((x, y), text, fontname=font, fontsize=size)

    put(50, 50, "FIRST NATIONAL BANK", 14)
    put(50, 66, "100 Main Street")
    put(50, 80, "Springfield, IL 62701")
    put(50, 94, "Phone: 800-555-0100")
    put(330, 50, "Account Holder: Jane Q Public")
    put(330, 66, "55 Elm Street")
    put(330, 80, "Springfield, IL 62702")
    put(50, 118, "Account Number: 123456789012")
    put(330, 118, "Routing Number: 021000021")
    put(50, 136, "Statement Period: 08/01/2026 - 08/31/2026")
    put(50, 154, "Beginning Balance: 5,000.00")
    put(50, 180, "Date       Description                    Debit       Credit      Balance")
    rows = [
        "08/02/2026 STARBUCKS #1234                6.45                    4,993.55",
        "08/05/2026 HOSTINGER.COM                  12.99                   4,980.56",
        "08/05/2026 PAYROLL ACME CORP                          2,400.00    7,380.56",
        "08/12/2026 HOSTINGER *DOMAINS             10.99                   7,369.57",
        "08/18/2026 WHOLE FOODS                    84.12                   7,285.45",
        "08/22/2026 ATM WITHDRAWAL                 60.00                   7,225.45",
    ]
    y = 196
    for row in rows:
        put(50, y, row)
        y += 16
    put(50, y + 16, "Ending Balance: 7,225.45")
    put(50, y + 32, "Total Debits: 174.55")
    put(50, y + 48, "Total Credits: 2,400.00")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


def dump_json(data: Any, path: Path | None) -> None:
    text = json.dumps(data, indent=2)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def print_find_human(plan_or_payload: dict[str, Any], matches: list[dict[str, Any]], pii: list[dict[str, Any]]) -> None:
    print(f"Matched {len(matches)} transaction(s):")
    for m in matches:
        amt = m.get("debit") or m.get("credit") or "?"
        desc = (m.get("description") or "")[:72]
        print(f"  [{m['index']}] p{m['page']}  {m.get('date')}  {desc}  {amt}")
    print("Detected fields (masked):")
    by_field: dict[str, list[str]] = {}
    for hit in pii:
        by_field.setdefault(hit["field"], []).append(hit.get("preview") or "")
    for field, previews in sorted(by_field.items()):
        print(f"  {field}: {', '.join(previews[:3])}")
    print("Revise with --keep / --unkeep-index / --also-redact / --redact-pii")


def parse_csv_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_int_list(values: list[int] | list[str] | None) -> set[int]:
    out: set[int] = set()
    if not values:
        return out
    for raw in values:
        if isinstance(raw, int):
            out.add(raw)
            continue
        for part in str(raw).split(","):
            part = part.strip()
            if part:
                out.add(int(part))
    return out


def regions_for_also_redact(payload: dict[str, Any], terms: list[str], regexes: list[str]) -> list[Region]:
    regions: list[Region] = []
    needles = [t for t in terms if t]
    patterns = [re.compile(p, re.I) for p in regexes if p]
    if not needles and not patterns:
        return regions

    def hits_text(text: str) -> bool:
        low = text.lower()
        if any(n.lower() in low for n in needles):
            return True
        return any(p.search(text) for p in patterns)

    for item in payload.get("transactions") or []:
        for raw in item.get("words") or []:
            text = str(raw.get("text") or "")
            if hits_text(text):
                regions.append(
                    Region(
                        int(raw["page"]),
                        [raw["x0"], raw["y0"], raw["x1"], raw["y1"]],
                        "also_redact",
                        "also_redact",
                    )
                )
    for line in payload.get("header_lines") or []:
        if hits_text(str(line.get("text") or "")):
            regions.append(Region(int(line["page"]), line["bbox"], "also_redact", "also_redact"))
    return regions


def cmd_sample(args: argparse.Namespace) -> None:
    make_sample(Path(args.output))
    print(f"Wrote {args.output}")


def cmd_scan(args: argparse.Namespace) -> None:
    pdf = Path(args.pdf)
    work = Path(args.workdir)
    extract_path = Path(args.extract) if args.extract else work / "extract.json"
    map_path = Path(args.output) if args.output else work / "map.json"
    payload = extract_payload(pdf)
    dump_json(payload, extract_path)
    mp = build_statement_map(payload, str(extract_path))
    keep = args.keep or []
    if keep:
        matches = []
        for item in payload.get("transactions") or []:
            tx = tx_from_payload(item)
            hits = match_terms(tx, keep, args.keep_re or [])
            if hits:
                matches.append(
                    {
                        "index": item["index"],
                        "page": tx.page + 1,
                        "date": tx.date,
                        "description": _scrub_preview(tx.description),
                        "debit": tx.debit,
                        "credit": tx.credit,
                        "terms": hits,
                    }
                )
        mp["keep_terms"] = keep
        mp["keep_matches"] = matches
        mp["keep_match_count"] = len(matches)
    dump_json(mp, map_path)
    if args.json:
        dump_json(mp, None)
    else:
        print_map_human(mp)
        if keep:
            print(f"  keep {keep!r}: {mp.get('keep_match_count', 0)} match(es)")
            for m in mp.get("keep_matches") or []:
                amt = m.get("debit") or m.get("credit") or "?"
                print(f"    [{m['index']}] p{m['page']}  {m.get('date')}  {m.get('description')}  {amt}")
        print(f"Wrote {extract_path}")
        print(f"Wrote {map_path}")
    if payload.get("scanned_pages"):
        raise SystemExit(4)
    if not mp["ready"]:
        raise SystemExit("Map is not ready. Do not one-shot. Inspect header_row and sample rows.")
    if keep and not mp.get("keep_match_count"):
        raise SystemExit(2)


def cmd_extract(args: argparse.Namespace) -> None:
    payload = extract_payload(Path(args.pdf))
    dump_json(payload, Path(args.output) if args.output else None)
    if payload["scanned_pages"]:
        print(
            f"warning: pages {[p + 1 for p in payload['scanned_pages']]} look image-only; OCR before redacting",
            file=sys.stderr,
        )
    print(f"transactions={len(payload['transactions'])} pii_hits={len(payload['pii'])}", file=sys.stderr)


def cmd_find(args: argparse.Namespace) -> None:
    payload = load_json(Path(args.extract))
    keep = args.keep or []
    regexes = args.keep_re or []
    if not keep and not regexes:
        raise SystemExit("Pass --keep TERM (repeatable) and/or --keep-re REGEX")
    matches = []
    for item in payload.get("transactions") or []:
        tx = tx_from_payload(item)
        hits = match_terms(tx, keep, regexes)
        if hits:
            matches.append(
                {
                    "index": item["index"],
                    "page": tx.page + 1,
                    "date": tx.date,
                    "description": tx.description,
                    "debit": tx.debit,
                    "credit": tx.credit,
                    "terms": hits,
                }
            )
    result = {
        "match_count": len(matches),
        "matches": matches,
        "pii": payload.get("pii") or [],
        "ask_pii": list(ASK_PII),
        "recommended_pii": list(RECOMMENDED_PII),
        "default_keep_columns": list(DEFAULT_KEEP_COLUMNS),
        "scanned_pages": payload.get("scanned_pages") or [],
    }
    if args.json:
        dump_json(result, Path(args.output) if args.output else None)
    else:
        print_find_human(payload, matches, payload.get("pii") or [])
        if args.output:
            dump_json(result, Path(args.output))
    if not matches:
        raise SystemExit(2)


def redact_pii_from_args(args: argparse.Namespace, keep_columns: list[str]) -> list[str]:
    redact_pii = parse_csv_arg(args.redact_pii)
    if getattr(args, "keep_running_balance", False):
        if "balance" not in keep_columns:
            keep_columns.append("balance")
        return [f for f in redact_pii if f != "running_balance"]
    if "running_balance" not in redact_pii and "balance" not in keep_columns:
        redact_pii.append("running_balance")
    return redact_pii


def build_plan_from_args(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    keep_columns = parse_csv_arg(args.keep_columns) or list(DEFAULT_KEEP_COLUMNS)
    redact_pii = redact_pii_from_args(args, keep_columns)
    return build_plan(
        payload,
        args.keep or [],
        args.keep_re or [],
        redact_pii,
        keep_columns,
        unkeep_indexes=parse_int_list(getattr(args, "unkeep_index", None)),
        also_redact=list(getattr(args, "also_redact", None) or []),
        also_redact_re=list(getattr(args, "also_redact_re", None) or []),
    )


def cmd_plan(args: argparse.Namespace) -> None:
    payload = load_json(Path(args.extract))
    plan = build_plan_from_args(payload, args)
    dump_json(plan, Path(args.output) if args.output else None)
    print(
        f"matches={plan['match_count']} unkeep={plan.get('unkeep_indexes')} "
        f"also={plan.get('also_redact')} regions={len(plan['regions'])}",
        file=sys.stderr,
    )


def cmd_apply(args: argparse.Namespace) -> None:
    plan = load_json(Path(args.plan))
    fill_name = args.fill
    fill = {"black": (0, 0, 0), "white": (1, 1, 1)}[fill_name]
    engine = args.engine
    if engine == "auto":
        engine = "pymupdf"
    out = Path(args.output)
    if engine == "jev":
        apply_jev(Path(args.pdf), plan, out)
    else:
        apply_pymupdf(Path(args.pdf), plan, out, fill)
    print(f"Wrote {out}")


def cmd_preview(args: argparse.Namespace) -> None:
    plan = load_json(Path(args.plan))
    paths = render_preview(Path(args.pdf), plan, Path(args.output))
    for p in paths:
        print(p)


def cmd_verify(args: argparse.Namespace) -> None:
    plan = load_json(Path(args.plan))
    result = verify_output(Path(args.pdf), plan)
    dump_json(result, Path(args.output) if args.output else None)
    if not result["ok"]:
        raise SystemExit(3)


def cmd_redact(args: argparse.Namespace) -> None:
    work = Path(args.workdir)
    extract_path = work / "extract.json"
    plan_path = work / "plan.json"
    payload = None
    mp = None
    if getattr(args, "map", None):
        mp = load_json(Path(args.map))
        cached = mp.get("extract_path")
        if cached and Path(cached).exists():
            payload = load_json(Path(cached))
            extract_path = Path(cached)
    if payload is None and getattr(args, "extract", None):
        payload = load_json(Path(args.extract))
        extract_path = Path(args.extract)
    if payload is None:
        payload = extract_payload(Path(args.pdf))
        dump_json(payload, extract_path)
    if mp is None:
        mp = build_statement_map(payload, str(extract_path))
        dump_json(mp, work / "map.json")
    if mp.get("scanned_pages"):
        raise SystemExit("Map has image-only pages. Do not one-shot.")
    if not mp.get("ready"):
        raise SystemExit("Statement map is not ready. Run scan first and inspect the header.")
    if not parse_csv_arg(args.redact_pii) and mp.get("recommended_redact_pii"):
        args.redact_pii = ",".join(mp["recommended_redact_pii"])
    dump_json(payload, extract_path)
    plan = build_plan_from_args(payload, args)
    dump_json(plan, plan_path)
    fill = {"black": (0, 0, 0), "white": (1, 1, 1)}[args.fill]
    apply_pymupdf(Path(args.pdf), plan, Path(args.output), fill)
    result = verify_output(Path(args.output), plan)
    dump_json(result, work / "verify.json")
    print(f"Wrote {args.output}")
    print(f"profile={mp.get('profile')} matches={plan['match_count']} regions={len(plan['regions'])} verify_ok={result['ok']}")
    if not result["ok"]:
        raise SystemExit(3)


def add_selection_flags(parser: argparse.ArgumentParser, *, keep_required: bool = False) -> None:
    parser.add_argument("--keep", action="append", default=[], required=keep_required)
    parser.add_argument("--keep-re", action="append", default=[])
    parser.add_argument(
        "--unkeep-index",
        action="append",
        default=[],
        help="Transaction index to blank even if it matched --keep",
    )
    parser.add_argument(
        "--also-redact",
        action="append",
        default=[],
        help="Extra text to blank, including on kept rows",
    )
    parser.add_argument("--also-redact-re", action="append", default=[])
    parser.add_argument("--keep-columns", default="date,description,debit,credit")
    parser.add_argument("--redact-pii", default="")
    parser.add_argument("--keep-running-balance", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redactus",
        description="Keep-search redaction for bank-statement PDFs",
        epilog="Scan a statement first, then one-shot with redact --map. The original PDF is never overwritten.",
    )
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="Write a synthetic statement PDF")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("scan", help="Map this PDF's layout before a one-shot redact")
    s.add_argument("pdf")
    s.add_argument("-o", "--output", help="map.json path")
    s.add_argument("--extract", help="extract.json path (default: workdir/extract.json)")
    s.add_argument("--workdir", default=".redactus")
    s.add_argument("--keep", action="append", default=[], help="Optional keep term to count during the scan")
    s.add_argument("--keep-re", action="append", default=[])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("extract", help="Extract transactions and masked PII")
    s.add_argument("pdf")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("find", help="Show keep-term matches from an extract")
    s.add_argument("extract")
    s.add_argument("--keep", action="append", default=[])
    s.add_argument("--keep-re", action="append", default=[])
    s.add_argument("-o", "--output")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("plan", help="Build redaction regions")
    s.add_argument("extract")
    add_selection_flags(s)
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("apply", help="Burn redaction regions into a PDF")
    s.add_argument("pdf")
    s.add_argument("plan")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--fill", choices=("black", "white"), default="black")
    s.add_argument("--engine", choices=("auto", "pymupdf", "jev"), default="pymupdf")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("preview", help="Rasterize pages with proposed boxes")
    s.add_argument("pdf")
    s.add_argument("plan")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(func=cmd_preview)

    s = sub.add_parser("verify", help="Fail if kept terms vanished or leaked tokens remain")
    s.add_argument("pdf")
    s.add_argument("plan")
    s.add_argument("-o", "--output")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("redact", help="extract + plan + apply + verify in one shot (after scan + user confirmed)")
    s.add_argument("pdf")
    add_selection_flags(s, keep_required=True)
    s.add_argument("-o", "--output", required=True)
    s.add_argument("--workdir", default=".redactus")
    s.add_argument("--map", help="map.json from scan; reuse extract and recommended fields")
    s.add_argument("--extract", help="Reuse an existing extract.json")
    s.add_argument("--fill", choices=("black", "white"), default="black")
    s.set_defaults(func=cmd_redact)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
