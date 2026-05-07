"""Stripe webhook + lookup helpers for the MKOT 3.0 reservation flow.

Endpoint:
  POST /api/webhook/stripe
    Receives `checkout.session.completed` events from Stripe Payment Links.
    Verifies the signature, then upserts a Reservation row keyed by
    stripe_session_id (idempotent on retries).

The companion form lives in app/routes/reservation.py and looks up the
Reservation by ?sid=<stripe_session_id> to pre-fill the email and persist
the rest of the buyer-supplied fields.
"""

import os
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from app.models import db, Reservation, Ambassador

logger = logging.getLogger(__name__)

stripe_bp = Blueprint("stripe_webhook", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


@stripe_bp.route("/api/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Stripe posts here on payment events. We only care about
    checkout.session.completed for now (Payment Links produce these).

    Signature is verified with STRIPE_WEBHOOK_SECRET. If the secret is not
    configured we reject — better to fail loud than silently accept fake
    events in production.
    """
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error("STRIPE_WEBHOOK_SECRET not configured — rejecting webhook")
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
        logger.warning("Invalid Stripe payload: %s", e)
        return ("invalid payload", 400)
    except Exception as e:
        # SignatureVerificationError lives under stripe.error in older versions.
        logger.warning("Invalid Stripe signature: %s", e)
        return ("invalid signature", 400)

    event_type = event.get("type", "")
    if event_type != "checkout.session.completed":
        # Ignore — but 200 so Stripe doesn't retry.
        return jsonify(ok=True, ignored=event_type), 200

    session = event["data"]["object"]
    session_id = session.get("id")
    if not session_id:
        logger.warning("checkout.session.completed without id")
        return ("missing session id", 400)

    # Idempotency: if we already have a row with this session_id, no-op.
    existing = Reservation.query.filter_by(stripe_session_id=session_id).first()
    if existing is not None:
        # Update payment_intent if missing (some events arrive in pieces).
        pi = session.get("payment_intent")
        if pi and not existing.stripe_payment_intent_id:
            existing.stripe_payment_intent_id = pi
            db.session.commit()
        logger.info("stripe webhook: duplicate session %s — no-op", session_id)
        return jsonify(ok=True, duplicate=True), 200

    # Stripe Payment Links capture customer_email under customer_details.
    customer_details = session.get("customer_details") or {}
    email = (
        customer_details.get("email")
        or session.get("customer_email")
        or ""
    ).strip().lower()

    if not email:
        # We need an email to identify the buyer in the form.
        logger.warning("checkout.session.completed without buyer email: %s", session_id)
        # Still record the row so it shows up in admin — form lookup will fail loudly.
        email = "unknown@unknown.invalid"

    amount_total = session.get("amount_total")  # already in cents
    currency = (session.get("currency") or "eur").lower()
    payment_intent = session.get("payment_intent")
    if isinstance(payment_intent, dict):
        payment_intent = payment_intent.get("id")

    # Match by email to an existing Ambassador (case-insensitive).
    ambassador = (
        Ambassador.query.filter(Ambassador.email.ilike(email)).first()
        if email and "@" in email else None
    )

    reservation = Reservation(
        stripe_session_id=session_id,
        stripe_payment_intent_id=payment_intent,
        amount_cents=amount_total,
        currency=currency,
        paid_at=_utcnow(),
        email=email,
        ambassador_id=(ambassador.id if ambassador else None),
    )
    db.session.add(reservation)
    db.session.commit()

    logger.info(
        "stripe webhook: created reservation id=%s sid=%s email=%s amt=%s",
        reservation.id, session_id, email, amount_total,
    )
    return jsonify(ok=True, reservation_id=reservation.id), 200
