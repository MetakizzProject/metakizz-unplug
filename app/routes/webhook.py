"""
External webhook endpoints:

- /api/webhook/signup → Go High Level posts new PLF signups here.
- /api/webhook/resend → Resend posts email lifecycle events (sent, opened,
                       clicked, bounced, complained, delivered) so we can
                       compute open/click rates per template in the admin.
"""

import json
import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from app.services.signup import create_signup
from app.models import db, EmailEvent

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)


def _pluck(payload, *keys):
    """Return the first non-empty value found by walking the payload at the given keys.

    GHL webhooks can deliver custom data either at the top level or nested under
    keys like 'custom_data', 'customData', 'contact', etc. This walks all the
    common shapes and returns whatever it finds first.
    """
    if not isinstance(payload, dict):
        return ""

    # 1. Direct top-level lookup.
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 2. Nested under common GHL containers.
    for container in ("custom_data", "customData", "data", "contact", "Contact"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            for key in keys:
                val = nested.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    return ""


def _extract_signup_fields(payload):
    """Pull name, email, and ref code from a GHL webhook payload in any reasonable shape."""
    # Email — most stable identifier across GHL payload variants.
    email = _pluck(payload, "email", "Email", "contact_email", "contactEmail")

    # Name — try full name first, then fall back to first/last concatenation.
    name = _pluck(
        payload,
        "name", "full_name", "fullName", "Full Name", "contact_name", "contactName",
    )
    if not name:
        first = _pluck(payload, "first_name", "firstName", "First Name")
        last = _pluck(payload, "last_name", "lastName", "Last Name")
        name = (first + " " + last).strip()

    # Ref code (optional) — referrer's referral_code, sent through the GHL form's hidden field.
    ref_code = _pluck(payload, "ref", "referred_by", "referredBy", "referral_code")

    return name, email, ref_code


@webhook_bp.route("/api/webhook/signup", methods=["POST"])
def ghl_signup():
    """
    Receives a signup notification from GHL after a user fills the PLF form on Lovable.

    Tolerates several payload shapes (top-level keys, custom_data wrapper, contact wrapper).
    Auth: shared secret in X-Webhook-Secret header.
    """
    expected_secret = current_app.config.get("GHL_WEBHOOK_SECRET", "")
    if not expected_secret or request.headers.get("X-Webhook-Secret") != expected_secret:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    # Log the raw payload so we can see exactly what GHL sends. Truncated to avoid
    # log spam if a future caller sends a giant body.
    try:
        raw_preview = json.dumps(payload)[:2000]
    except Exception:
        raw_preview = repr(payload)[:2000]
    logger.info("GHL webhook payload: %s", raw_preview)

    name, email, ref_code = _extract_signup_fields(payload)

    if not name or not email:
        logger.warning(
            "GHL webhook missing fields after extraction: name=%r email=%r ref=%r",
            name, email, ref_code,
        )
        return jsonify({
            "error": "name and email are required",
            "extracted": {"name": name, "email": email, "ref": ref_code},
        }), 400

    # Email validation — same checks as /join, applied here so GHL-driven
    # signups can't bypass the disposable/MX defences. We log + reject;
    # GHL will retry on 4xx, so use 400 for permanent errors.
    from app.services.email_validation import (
        is_disposable_email, is_valid_email_syntax, has_mx_record,
    )
    email_lower = email.lower().strip()
    if not is_valid_email_syntax(email_lower):
        logger.warning("GHL webhook rejected: bad syntax email=%r", email)
        return jsonify({"error": "invalid_email_syntax", "email": email}), 400
    if is_disposable_email(email_lower):
        logger.warning("GHL webhook rejected: disposable email=%r", email)
        return jsonify({"error": "disposable_email", "email": email}), 400
    if not has_mx_record(email_lower):
        logger.warning("GHL webhook rejected: no MX record email=%r", email)
        return jsonify({"error": "domain_no_mx", "email": email}), 400

    try:
        ambassador, was_new = create_signup(name, email, ref_code)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("signup webhook failed")
        return jsonify({"error": "internal"}), 500

    return jsonify({
        "ok": True,
        "was_new": was_new,
        "referral_code": ambassador.referral_code,
        "dashboard_url": f"{current_app.config['APP_URL']}/dashboard/{ambassador.dashboard_code}",
    }), 200


# ════════════════════════════════════════════════════════════════════
# RESEND WEBHOOK — email lifecycle events (open, click, bounce, etc.)
# ════════════════════════════════════════════════════════════════════
#
# Configure in Resend dashboard:
#   1. Webhooks → Add → URL: https://<your-app>/api/webhook/resend
#   2. Events to select: email.sent, email.delivered, email.opened,
#                        email.clicked, email.bounced, email.complained
#   3. Copy the signing secret → set as RESEND_WEBHOOK_SECRET env var.
#
# We match incoming events to our 'sent' rows via Resend's email id (which
# we stored at send time). The original 'sent' row carries the template_key
# and ambassador_id, so per-template open/click rates can be computed by
# joining sent rows ↔ event rows on resend_email_id.
#
# Supported event names emitted by Resend (subject to API version):
#   email.sent / email.delivered / email.opened / email.clicked /
#   email.bounced / email.complained / email.delivery_delayed

_RESEND_TYPE_MAP = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.opened": "opened",
    "email.clicked": "clicked",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.delivery_delayed": "delayed",
}


@webhook_bp.route("/api/webhook/resend", methods=["POST"])
def resend_webhook():
    """Persist Resend lifecycle events as EmailEvent rows.

    Lookup logic:
      - Webhook payload contains email_id → match the original 'sent' row
        (via resend_email_id) to copy its template_key + ambassador_id.
      - If we can't match (e.g. an email sent before tracking was wired),
        we still record the event with template_key='unknown'.
    """
    payload = request.get_json(silent=True) or {}
    event_type_raw = payload.get("type") or ""
    event_type = _RESEND_TYPE_MAP.get(event_type_raw, event_type_raw)

    data = payload.get("data") or {}
    resend_email_id = data.get("email_id") or data.get("id")
    to_field = data.get("to") or []
    if isinstance(to_field, list) and to_field:
        to_email = to_field[0]
    elif isinstance(to_field, str):
        to_email = to_field
    else:
        to_email = ""

    # Look up the original 'sent' row to copy template_key + ambassador_id.
    template_key = "unknown"
    ambassador_id = None
    if resend_email_id:
        sent = (
            EmailEvent.query
            .filter_by(resend_email_id=resend_email_id, event_type="sent")
            .first()
        )
        if sent is not None:
            template_key = sent.template_key
            ambassador_id = sent.ambassador_id

    # 'sent' itself we already wrote at send time; ignore the duplicate from
    # Resend (their event would race the synchronous insert anyway).
    if event_type == "sent":
        return jsonify({"ok": True, "ignored": "duplicate_sent"}), 200

    try:
        evt = EmailEvent(
            ambassador_id=ambassador_id,
            template_key=template_key,
            event_type=event_type,
            resend_email_id=resend_email_id,
            to_email=to_email,
            extra=json.dumps(payload)[:5000] if payload else None,
        )
        db.session.add(evt)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to persist resend webhook event")
        return jsonify({"error": "persist_failed"}), 500

    return jsonify({"ok": True, "stored": event_type}), 200

