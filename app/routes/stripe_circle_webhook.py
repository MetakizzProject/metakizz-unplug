"""Stripe Circle account webhook → auto-refund the €100 deposit.

Endpoint:
  POST /api/webhook/stripe-circle
    Receives `checkout.session.completed` and `charge.succeeded` events
    from the Circle-connected Stripe account (the one where buyers pay
    the full MKOT 3.0 plan).
    Verifies signature with STRIPE_CIRCLE_WEBHOOK_SECRET. When the buyer
    matches an existing Reservation (by email) with paid_at set and no
    refund yet, we issue a refund of €100 against the deposit account
    using STRIPE_DEPOSIT_API_KEY.

Safety:
  STRIPE_REFUND_ENABLED=1 → executes the real refund.
  Anything else (default OFF) → "dry-run": logs everything, marks
    refund_status="dry_run" on the Reservation, but does NOT call Stripe.

  This lets us deploy the code, see real webhooks land, validate the
  match logic against real customers, and only flip the switch to live
  refunds once we're confident.
"""

import os
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from app.models import db, Reservation, CirclePayment
from app.mailer import send_refund_admin_alert, send_refund_confirmation_email

logger = logging.getLogger(__name__)

stripe_circle_bp = Blueprint("stripe_circle_webhook", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _refund_enabled():
    return os.getenv("STRIPE_REFUND_ENABLED", "").strip() in ("1", "true", "True", "yes")


@stripe_circle_bp.route("/api/webhook/stripe-circle", methods=["POST"])
def stripe_circle_webhook():
    """Handle pay-event from the Circle Stripe account → auto-refund deposit."""
    secret = os.getenv("STRIPE_CIRCLE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error("STRIPE_CIRCLE_WEBHOOK_SECRET not configured — rejecting")
        return ("webhook secret not configured", 503)

    try:
        import stripe
    except ImportError:
        logger.error("stripe package not installed")
        return ("stripe package missing", 500)

    payload = request.get_data(as_text=False)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except ValueError as e:
        logger.warning("circle webhook: invalid payload: %s", e)
        return ("invalid payload", 400)
    except Exception as e:
        logger.warning("circle webhook: invalid signature: %s", e)
        return ("invalid signature", 400)

    event_type = event.get("type", "")
    event_id = event.get("id", "")

    # We only care about events that mean "buyer just paid for the full plan".
    # checkout.session.completed → fires on Payment Links / Checkout flows.
    # charge.succeeded → fires for one-off charges and subscription cycles.
    # We dedupe by Stripe charge id so multiple events for the same payment
    # only result in ONE refund.
    customer_name = None
    description = None
    payment_intent_id = None

    if event_type == "checkout.session.completed":
        session = event["data"]["object"]
        email = _extract_email(session)
        amount = session.get("amount_total")
        currency = (session.get("currency") or "eur").lower()
        session_id = session.get("id")
        circle_charge_id = (
            session.get("payment_intent")
            or session_id
            or event_id
        )
        payment_intent_id = session.get("payment_intent")
        customer_name = ((session.get("customer_details") or {}).get("name") or None)
        # Try the inline payload first (rarely populated).
        inline_items = session.get("line_items") or {}
        if isinstance(inline_items, dict):
            data = inline_items.get("data") or []
            if data and isinstance(data[0], dict):
                description = (data[0].get("description") or None)
        # Pull line items from the API to get the real product name. The
        # webhook payload doesn't include line_items by default; we have
        # to expand them ourselves.
        api_key = os.getenv("STRIPE_CIRCLE_API_KEY", "").strip()
        if not description and api_key and session_id:
            try:
                full_session = stripe.checkout.Session.retrieve(
                    session_id,
                    api_key=api_key,
                    expand=["line_items", "line_items.data.price.product"],
                )
                items = (full_session.get("line_items") or {}).get("data") or []
                if items:
                    first = items[0]
                    description = first.get("description") or None
                    if not description:
                        price = first.get("price") or {}
                        product = price.get("product") or {}
                        if isinstance(product, dict):
                            description = product.get("name") or None
            except Exception:
                logger.exception("circle webhook: failed to expand line_items for session %s", session_id)
    elif event_type == "charge.succeeded":
        charge = event["data"]["object"]
        billing = charge.get("billing_details") or {}
        email = (
            (billing.get("email") or "")
            or (charge.get("receipt_email") or "")
        ).strip().lower()
        amount = charge.get("amount")
        currency = (charge.get("currency") or "eur").lower()
        circle_charge_id = charge.get("id") or event_id
        payment_intent_id = charge.get("payment_intent")
        customer_name = (billing.get("name") or None)
        description = (charge.get("description") or None)
    else:
        # Ignore — but 200 so Stripe doesn't retry forever.
        return jsonify(ok=True, ignored=event_type), 200

    if isinstance(circle_charge_id, dict):
        circle_charge_id = circle_charge_id.get("id")
    if isinstance(payment_intent_id, dict):
        payment_intent_id = payment_intent_id.get("id")

    logger.info(
        "circle webhook: type=%s email=%s amount=%s currency=%s charge=%s",
        event_type, email, amount, currency, circle_charge_id,
    )

    if not email:
        logger.warning("circle webhook: no email on event %s", event_id)
        return jsonify(ok=True, no_email=True), 200

    # Upsert a CirclePayment row so the admin sees ALL paid customers in
    # /admin/reservations (even ones without a deposit). Idempotent on
    # stripe_charge_id.
    new_circle_payment = None
    if circle_charge_id:
        existing_payment = CirclePayment.query.filter_by(stripe_charge_id=circle_charge_id).first()
        if existing_payment is None:
            new_circle_payment = CirclePayment(
                stripe_charge_id=circle_charge_id,
                stripe_payment_intent_id=payment_intent_id,
                email=email,
                customer_name=customer_name,
                amount_cents=amount,
                currency=currency,
                paid_at=_utcnow(),
                description=description,
                raw_event_type=event_type,
            )
            db.session.add(new_circle_payment)
            db.session.commit()
            logger.info(
                "circle webhook: persisted CirclePayment charge=%s email=%s amount=%s",
                circle_charge_id, email, amount,
            )

    # Auto-send invoice if enabled and we have a fresh CirclePayment.
    # Safety flag: INVOICE_AUTO_SEND=1 to enable, anything else = off.
    if new_circle_payment is not None and os.getenv("INVOICE_AUTO_SEND", "").strip() in ("1", "true", "True", "yes"):
        try:
            from app.routes.admin import _generate_and_send_invoice
            _generate_and_send_invoice(new_circle_payment)
        except Exception:
            logger.exception("circle webhook: auto-invoice failed for CirclePayment %s", new_circle_payment.id)
    elif new_circle_payment is not None:
        logger.info(
            "circle webhook: invoice NOT sent (INVOICE_AUTO_SEND off). "
            "CirclePayment id=%s — set INVOICE_AUTO_SEND=1 to enable.",
            new_circle_payment.id,
        )

    # Idempotency: have we already processed THIS Circle charge?
    already = (
        Reservation.query
        .filter_by(circle_payment_id=circle_charge_id)
        .first()
        if circle_charge_id else None
    )
    if already is not None:
        logger.info(
            "circle webhook: charge %s already processed for reservation %s — no-op",
            circle_charge_id, already.id,
        )
        return jsonify(ok=True, duplicate=True), 200

    # Find the buyer's deposit reservation. Match by email (case-insensitive),
    # paid (paid_at IS NOT NULL), and not yet refunded.
    matches = (
        Reservation.query
        .filter(Reservation.email.ilike(email))
        .filter(Reservation.paid_at.isnot(None))
        .filter(Reservation.refunded_at.is_(None))
        .order_by(Reservation.paid_at.desc())
        .all()
    )

    if not matches:
        # Buyer paid the full plan without a deposit on file. That's normal
        # for direct-purchase customers — nothing to refund.
        logger.info("circle webhook: no deposit reservation for email=%s — skipping refund", email)
        return jsonify(ok=True, no_deposit=True), 200

    if len(matches) > 1:
        # Should be rare. Don't auto-pick — let the admin decide.
        logger.warning(
            "circle webhook: %d deposits found for email=%s — admin will decide",
            len(matches), email,
        )
        try:
            send_refund_admin_alert(
                email=email,
                reason=f"Multiple deposits ({len(matches)}) match this buyer",
                reservations=matches,
                circle_charge_id=circle_charge_id,
                circle_amount_cents=amount,
            )
        except Exception:
            logger.exception("circle webhook: failed to send multi-match admin alert")
        return jsonify(ok=True, multi_match=True, count=len(matches)), 200

    reservation = matches[0]
    refund_amount = reservation.amount_cents or 10000
    reservation.refund_attempted_at = _utcnow()
    reservation.circle_payment_id = circle_charge_id

    # Safety flag: only execute real refund when explicitly enabled.
    if not _refund_enabled():
        reservation.refund_status = "dry_run"
        db.session.commit()
        logger.info(
            "circle webhook: DRY-RUN — would refund %d cents on reservation %s "
            "(deposit pi=%s) for email=%s. Set STRIPE_REFUND_ENABLED=1 to go live.",
            refund_amount, reservation.id, reservation.stripe_payment_intent_id, email,
        )
        return jsonify(ok=True, dry_run=True, reservation_id=reservation.id), 200

    # Live mode: call Stripe in the deposit account.
    deposit_key = os.getenv("STRIPE_DEPOSIT_API_KEY", "").strip()
    if not deposit_key:
        msg = "STRIPE_DEPOSIT_API_KEY not configured — cannot issue refund"
        logger.error("circle webhook: %s", msg)
        reservation.refund_status = "failed"
        reservation.refund_error = msg
        db.session.commit()
        try:
            send_refund_admin_alert(
                email=email,
                reason=msg,
                reservations=[reservation],
                circle_charge_id=circle_charge_id,
                circle_amount_cents=amount,
            )
        except Exception:
            logger.exception("circle webhook: failed to send no-key admin alert")
        return jsonify(ok=True, refund_failed=True, reason="missing_key"), 200

    if not reservation.stripe_payment_intent_id:
        msg = "Reservation missing stripe_payment_intent_id — cannot refund"
        logger.error("circle webhook: %s id=%s", msg, reservation.id)
        reservation.refund_status = "failed"
        reservation.refund_error = msg
        db.session.commit()
        try:
            send_refund_admin_alert(
                email=email,
                reason=msg,
                reservations=[reservation],
                circle_charge_id=circle_charge_id,
                circle_amount_cents=amount,
            )
        except Exception:
            logger.exception("circle webhook: failed to send missing-pi admin alert")
        return jsonify(ok=True, refund_failed=True, reason="missing_payment_intent"), 200

    try:
        refund = stripe.Refund.create(
            api_key=deposit_key,
            payment_intent=reservation.stripe_payment_intent_id,
            reason="requested_by_customer",
            metadata={
                "reservation_id": str(reservation.id),
                "trigger": "circle_full_payment",
                "circle_charge_id": circle_charge_id or "",
                "buyer_email": email,
            },
        )
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception("circle webhook: refund failed for reservation %s", reservation.id)
        reservation.refund_status = "failed"
        reservation.refund_error = msg[:2000]
        db.session.commit()
        try:
            send_refund_admin_alert(
                email=email,
                reason=f"Stripe refund call failed: {msg}",
                reservations=[reservation],
                circle_charge_id=circle_charge_id,
                circle_amount_cents=amount,
            )
        except Exception:
            logger.exception("circle webhook: failed to send refund-failed admin alert")
        return jsonify(ok=True, refund_failed=True, error=msg), 200

    reservation.refund_id = refund.get("id")
    reservation.refund_amount_cents = refund.get("amount") or refund_amount
    reservation.refund_status = "success"
    reservation.refunded_at = _utcnow()
    db.session.commit()

    # Notify the buyer that their deposit is on the way back. Best-effort:
    # a failed email here does NOT undo the refund. Stamp the timestamp so
    # we don't double-send if the admin batch endpoint fires later.
    try:
        if send_refund_confirmation_email(reservation):
            reservation.refund_email_sent_at = _utcnow()
            db.session.commit()
    except Exception:
        logger.exception(
            "circle webhook: refund issued OK but confirmation email failed for reservation %s",
            reservation.id,
        )

    logger.info(
        "circle webhook: REFUND OK reservation=%s amount=%s refund_id=%s email=%s",
        reservation.id, reservation.refund_amount_cents, reservation.refund_id, email,
    )
    return jsonify(
        ok=True,
        refunded=True,
        reservation_id=reservation.id,
        refund_id=reservation.refund_id,
        amount_cents=reservation.refund_amount_cents,
    ), 200


def _extract_email(session):
    """Pull the buyer email from a checkout.session.completed payload."""
    customer_details = session.get("customer_details") or {}
    return (
        customer_details.get("email")
        or session.get("customer_email")
        or ""
    ).strip().lower()
