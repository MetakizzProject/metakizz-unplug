"""Infer modality / payment plan / program from a CirclePayment.

The admin reservations dashboard shows both what the buyer ELECTED
(Reservation.modality_choice / payment_plan, filled via the public
form before paying) AND what they actually PAID (inferred from the
CirclePayment amount and description). When the two disagree, the
admin needs to see it.

Inference rules (in order):
  1. Exact-or-close price-point match on `amount_cents` against the
     known MKOT 3.0 product matrix. This is the most reliable signal.
     One-payment prices and six-installment fragments both live in
     the table so we can tell the plan apart from the cycle amount.
  2. Description keyword scan (case-insensitive) for fallback.
  3. raw_event_type as a tie-breaker for the payment plan:
       'checkout.session.completed' → almost always one_payment
       'charge.succeeded'           → usually six_installments cycle

Returns a dict shaped like::

    {
      "program": "dancers"|"instructors"|None,
      "modality": "solo"|"duo"|None,
      "payment_plan": "one_payment"|"six_installments"|None,
      "source": "amount"|"description"|"event_type"|"unknown",
    }

Any field can be None when we can't tell with reasonable confidence.
The MKOT_AVG_ESTIMATE bucket (€1300) is intentionally NOT included —
that's a placeholder for "form pending", not a real product price.
"""

# Full single-payment product prices, in cents (EUR).
# Matches the buckets in admin.py:_revenue_breakdown so the inference
# stays in sync with the revenue dashboard.
_ONE_PAYMENT_PRICES = {
    99700:  ("dancers",     "solo"),
    124700: ("dancers",     "duo"),
    134700: ("instructors", "solo"),
    179700: ("instructors", "duo"),
}

# Per-cycle amount for the 6-installments plan (full price / 6, rounded
# to the nearest cent). Stripe's recurring charge fires charge.succeeded
# with one of these amounts each cycle.
_SIX_INSTALLMENT_PRICES = {
    16617:  ("dancers",     "solo"),       # 99700/6
    20783:  ("dancers",     "duo"),        # 124700/6
    22450:  ("instructors", "solo"),       # 134700/6
    29950:  ("instructors", "duo"),        # 179700/6
}

# Tolerance (in cents) when matching prices, to absorb tiny rounding
# differences from Stripe's currency conversions or fee handling.
_PRICE_TOLERANCE_CENTS = 200

# Description keyword sets — last-resort fallback when the amount
# doesn't match any known price.
_DESCRIPTION_KEYWORDS_MODALITY = {
    "solo": ("solo", "single", "individual"),
    "duo": ("duo", "couple", "pareja", "dúo"),
}
_DESCRIPTION_KEYWORDS_PROGRAM = {
    "dancers": ("dancer", "dancers", "bailarin", "bailarín", "bailarines"),
    "instructors": ("instructor", "instructors", "instructores", "teacher", "profesor"),
}
_DESCRIPTION_KEYWORDS_PLAN = {
    "one_payment": ("one payment", "single payment", "1 payment", "pago único", "un pago"),
    "six_installments": ("six", "6 payment", "6 installments", "installment", "cuotas", "plazos", "monthly"),
}


def _close(amount_cents, target_cents):
    """True if amount is within the global tolerance of target."""
    if amount_cents is None or target_cents is None:
        return False
    return abs(amount_cents - target_cents) <= _PRICE_TOLERANCE_CENTS


def _match_by_price(amount_cents):
    """Try to match the amount against known one-payment or 6-installment
    price points. Returns (program, modality, payment_plan, source) or
    (None, None, None, None) if no confident match.
    """
    if amount_cents is None or amount_cents <= 0:
        return None, None, None, None
    for price, (program, modality) in _ONE_PAYMENT_PRICES.items():
        if _close(amount_cents, price):
            return program, modality, "one_payment", "amount"
    for price, (program, modality) in _SIX_INSTALLMENT_PRICES.items():
        if _close(amount_cents, price):
            return program, modality, "six_installments", "amount"
    return None, None, None, None


def _match_keyword(description, keyword_map):
    """Find the first key whose any-of-its-keywords appears in description.
    Returns the key (e.g. 'solo'), or None.
    """
    if not description:
        return None
    desc = description.lower()
    for value, keywords in keyword_map.items():
        for kw in keywords:
            if kw in desc:
                return value
    return None


def infer_from_payment(cp):
    """Infer program/modality/payment_plan from a CirclePayment row.

    `cp` only needs `amount_cents`, `description`, `raw_event_type`. We
    don't query the DB or touch any other model.
    """
    if cp is None:
        return {"program": None, "modality": None, "payment_plan": None, "source": "unknown"}

    program, modality, plan, source = _match_by_price(cp.amount_cents)
    if program or modality or plan:
        return {"program": program, "modality": modality, "payment_plan": plan, "source": source}

    # Fall back to scanning the description for keywords. Less reliable
    # because product names on Stripe vary, but better than nothing for
    # unusual amounts (refunds, partial payments, etc.).
    desc = cp.description or ""
    modality = _match_keyword(desc, _DESCRIPTION_KEYWORDS_MODALITY)
    program = _match_keyword(desc, _DESCRIPTION_KEYWORDS_PROGRAM)
    plan = _match_keyword(desc, _DESCRIPTION_KEYWORDS_PLAN)

    if modality or program or plan:
        # raw_event_type can disambiguate the plan when the description
        # is silent on it: charge.succeeded on a subscription cycle
        # implies six-installments.
        if plan is None and (cp.raw_event_type or "") == "charge.succeeded":
            plan = "six_installments"
        elif plan is None and (cp.raw_event_type or "") == "checkout.session.completed":
            plan = "one_payment"
        return {"program": program, "modality": modality, "payment_plan": plan, "source": "description"}

    return {"program": None, "modality": None, "payment_plan": None, "source": "unknown"}
