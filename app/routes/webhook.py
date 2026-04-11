"""
External webhook endpoints. Currently used by Go High Level to notify the app
of new PLF signups so the leaderboard can be updated and welcome emails sent.
"""

import json
import logging
from flask import Blueprint, request, jsonify, current_app
from app.services.signup import create_signup

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
