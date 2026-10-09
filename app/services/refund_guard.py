"""Guard rails for the automatic deposit refund (stripe-circle webhook).

The Circle Stripe account no longer sells only the full programme. From
12-oct-2026 it also sells the monthly MetaKizz Community subscription
(15 → 19 → 24 → 29 €). A deposit holder who buys the Community must NOT
get the €100 deposit back: the deposit is only refunded when the buyer
pays for a real programme.

Two independent checks, applied before any refund:

  1. Amount: a charge smaller than the deposit never triggers the refund
     (refunding €100 against a €15 charge makes no sense). Programme
     charges are ≥ €150, even the 6-instalment and recovery plans.
  2. Product: STRIPE_REFUND_EXCLUDE_PRODUCT_IDS (comma-separated Stripe
     product ids, empty by default). When the product of the payment can
     be resolved (charge → invoice → lines → price.product, or the
     Checkout Session line items) and any of them is in the list, no
     refund.

`refund_skip_reason()` is a pure function (see tests/). The resolver
calls the Stripe API and is best-effort: when it cannot tell, it returns
an empty set and only the amount check applies.

Amounts are compared in minor units without currency conversion; every
deposit and programme price lives in EUR.
"""

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_DEPOSIT_CENTS = 10000

REASON_AMOUNT_UNKNOWN = "amount_unknown"
REASON_AMOUNT_BELOW_DEPOSIT = "amount_below_deposit"
REASON_EXCLUDED_PRODUCT = "excluded_product"


def excluded_product_ids(raw=None):
    """Parse STRIPE_REFUND_EXCLUDE_PRODUCT_IDS into a set of product ids."""
    if raw is None:
        raw = os.getenv("STRIPE_REFUND_EXCLUDE_PRODUCT_IDS", "")
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


def refund_skip_reason(amount_cents, deposit_cents=DEFAULT_DEPOSIT_CENTS,
                       product_ids=(), excluded_ids=()):
    """Return None when the deposit refund may go ahead, or the reason to skip it.

    - amount_unknown: the payment carries no usable amount.
    - amount_below_deposit: the payment is smaller than the deposit.
    - excluded_product: one of the payment's products is excluded.
    """
    if amount_cents is None:
        return REASON_AMOUNT_UNKNOWN
    try:
        amount = int(amount_cents)
    except (TypeError, ValueError):
        return REASON_AMOUNT_UNKNOWN
    deposit = int(deposit_cents or DEFAULT_DEPOSIT_CENTS)
    if amount < deposit:
        return REASON_AMOUNT_BELOW_DEPOSIT
    if set(product_ids or ()) & set(excluded_ids or ()):
        return REASON_EXCLUDED_PRODUCT
    return None


def _obj_id(value):
    """Stripe fields can be an id string or an expanded object."""
    if isinstance(value, dict):
        return value.get("id")
    return value or None


def _products_from_lines(lines):
    """Collect product ids from invoice lines or Checkout line items.

    Handles the classic shape (line.price.product) and the newer API
    shape (line.pricing.price_details.product).
    """
    found = set()
    for line in lines or []:
        product_id = None
        price = line.get("price")
        if isinstance(price, dict):
            product_id = _obj_id(price.get("product"))
        if not product_id:
            pricing = line.get("pricing") or {}
            product_id = _obj_id((pricing.get("price_details") or {}).get("product"))
        if product_id:
            found.add(product_id)
    return found


def resolve_product_ids(stripe, event_type, obj, api_key):
    """Best-effort set of Stripe product ids paid by this webhook object.

    `stripe` is the stripe module, `obj` the event's data.object and
    `api_key` a key of the Circle Stripe account (STRIPE_CIRCLE_API_KEY).
    Never raises; returns an empty set when the product can't be resolved.
    """
    if not api_key or not obj:
        return set()
    try:
        if event_type == "checkout.session.completed":
            session_id = obj.get("id")
            if not session_id:
                return set()
            session = stripe.checkout.Session.retrieve(
                session_id, api_key=api_key, expand=["line_items"],
            )
            return _products_from_lines(((session.get("line_items") or {}).get("data")))

        if event_type == "charge.succeeded":
            # Circle paywalls (one-off and subscriptions) bill through an
            # invoice. The webhook payload may omit `invoice` depending on
            # the endpoint's API version, so re-read the charge if needed.
            invoice_id = _obj_id(obj.get("invoice"))
            if not invoice_id and obj.get("id"):
                charge = stripe.Charge.retrieve(obj["id"], api_key=api_key)
                invoice_id = _obj_id(charge.get("invoice"))
            if invoice_id:
                invoice = stripe.Invoice.retrieve(invoice_id, api_key=api_key)
                return _products_from_lines(((invoice.get("lines") or {}).get("data")))

            # No invoice: Payment Link / Checkout one-off payment.
            payment_intent_id = _obj_id(obj.get("payment_intent"))
            if payment_intent_id:
                sessions = stripe.checkout.Session.list(
                    payment_intent=payment_intent_id,
                    limit=1,
                    api_key=api_key,
                    expand=["data.line_items"],
                )
                found = set()
                for session in sessions.get("data") or []:
                    found |= _products_from_lines(((session.get("line_items") or {}).get("data")))
                return found
    except Exception:
        logger.exception(
            "refund guard: failed to resolve product for %s %s",
            event_type, obj.get("id"),
        )
    return set()
