"""Correlative invoice numbering — INV-{YYYY}-{NNNN}.

Format chosen for US-LLC invoicing convention. Yearly counter resets the
sequence; we look up the highest existing number for the current year
and add 1. Stored in CirclePayment.invoice_id.

Concurrency note: in single-process Flask + low write volume (one invoice
per Stripe payment), max(invoice_id)+1 is safe enough. If we ever start
generating in parallel, we'd switch to a Postgres SEQUENCE.
"""

import re
from datetime import datetime, timezone

from app.models import db, CirclePayment


_INVOICE_RE = re.compile(r"^INV-(\d{4})-(\d+)$")


def next_invoice_number(now=None):
    """Return the next correlative invoice number for the current year.

    Example: if the latest INV for 2026 is INV-2026-0042, returns
    "INV-2026-0043". If none yet for this year, returns "INV-2026-0001".
    """
    now = now or datetime.now(timezone.utc)
    year = now.year
    prefix = f"INV-{year}-"

    # Find the max numeric suffix among invoice_ids that match the prefix.
    # We pull and parse in Python (small dataset) for portability across SQLite/Postgres.
    rows = (
        CirclePayment.query
        .filter(CirclePayment.invoice_id.like(f"{prefix}%"))
        .with_entities(CirclePayment.invoice_id)
        .all()
    )
    max_seq = 0
    for (inv_id,) in rows:
        if not inv_id:
            continue
        m = _INVOICE_RE.match(inv_id)
        if not m:
            continue
        try:
            seq = int(m.group(2))
            if seq > max_seq:
                max_seq = seq
        except ValueError:
            continue

    next_seq = max_seq + 1
    return f"{prefix}{next_seq:04d}"


def assign_invoice_number(circle_payment, commit=True):
    """Assign the next correlative invoice number to a CirclePayment.

    Idempotent — if the row already has invoice_id set, returns it unchanged.
    """
    if circle_payment.invoice_id:
        return circle_payment.invoice_id
    number = next_invoice_number()
    circle_payment.invoice_id = number
    if commit:
        db.session.commit()
    return number
