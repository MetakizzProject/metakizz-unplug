"""
External webhook endpoints. Currently used by Go High Level to notify the app
of new PLF signups so the leaderboard can be updated and welcome emails sent.
"""

import logging
from flask import Blueprint, request, jsonify, current_app
from app.services.signup import create_signup

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)


@webhook_bp.route("/api/webhook/signup", methods=["POST"])
def ghl_signup():
    """
    Receives a signup notification from GHL after a user fills the PLF form on Lovable.

    Expected JSON body: {"name": str, "email": str, "ref": str (optional)}
    Auth: shared secret in X-Webhook-Secret header.
    """
    expected_secret = current_app.config.get("GHL_WEBHOOK_SECRET", "")
    if not expected_secret or request.headers.get("X-Webhook-Secret") != expected_secret:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "")
    email = payload.get("email", "")
    ref_code = payload.get("ref", "")

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
