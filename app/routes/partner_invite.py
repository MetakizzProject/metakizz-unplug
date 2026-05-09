"""Public Partner Invite flow for the MKOT 3.0 Couple plan.

Flow:
  1. Buyer (already paid Couple plan) visits /invite-partner.
  2. Submits a JSON POST to /api/invite-partner with their info + their
     partner's info.
  3. We look up the buyer in Circle to mirror their access group
     (Dancers or Instructors), then add the partner to that same group
     and send Resend emails to both parties.

The DB row is created BEFORE the Circle call so we always have an audit
trail — even if Circle / Resend fail.
"""

import re
import logging
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, jsonify

from app.models import db, PartnerInvite
from app.services.circle_invite import invite_partner_to_circle, serialize_response
from app.mailer import (
    send_partner_welcome,
    send_partner_buyer_confirmation,
    send_partner_invite_failure_alert,
)

logger = logging.getLogger(__name__)

partner_invite_bp = Blueprint("partner_invite", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NOTE_MAX_LEN = 200

FRIENDLY_FAILURE_MSG = (
    "Something went wrong. Álvaro will reach out personally to set this up — "
    "check your inbox in the next hour."
)
BUYER_MISSING_MSG = (
    "We couldn't find your account in our community with that email. "
    "Double-check it's the same email you used to pay, or contact us directly."
)
SUCCESS_MSG = "Done. Your partner will receive their access in the next 5 minutes. 🫶🏼"

# Maps the technical Circle status to a category for the response/alert logic.
HARD_FAILURE_STATUSES = {"failed", "buyer_no_group"}
SUCCESS_STATUSES = {"created", "added_to_group"}


def _utcnow():
    return datetime.now(timezone.utc)


@partner_invite_bp.route("/invite-partner", methods=["GET"])
def invite_form():
    return render_template("partner_invite_form.html")


@partner_invite_bp.route("/api/invite-partner", methods=["POST"])
def invite_submit():
    payload = request.get_json(silent=True) or {}

    buyer_name = (payload.get("buyer_name") or "").strip()
    buyer_email = (payload.get("buyer_email") or "").strip().lower()
    partner_name = (payload.get("partner_name") or "").strip()
    partner_email = (payload.get("partner_email") or "").strip().lower()
    location = (payload.get("location") or "").strip() or None
    personal_note = (payload.get("personal_note") or "").strip() or None

    errors = []
    if not buyer_name:
        errors.append("Please enter your name.")
    if not buyer_email or not EMAIL_RE.match(buyer_email):
        errors.append("Please enter a valid email for yourself.")
    if not partner_name:
        errors.append("Please enter your partner's name.")
    if not partner_email or not EMAIL_RE.match(partner_email):
        errors.append("Please enter a valid email for your partner.")
    if buyer_email and partner_email and buyer_email == partner_email:
        errors.append("Your partner's email must be different from yours.")
    if personal_note and len(personal_note) > NOTE_MAX_LEN:
        personal_note = personal_note[:NOTE_MAX_LEN]

    if errors:
        return jsonify({"ok": False, "message": " ".join(errors)}), 400

    invite = PartnerInvite(
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        partner_name=partner_name,
        partner_email=partner_email,
        location=location,
        personal_note=personal_note,
        needs_followup=False,
    )
    db.session.add(invite)
    db.session.commit()

    status, raw, target_group = invite_partner_to_circle(
        buyer_email=buyer_email,
        partner_email=partner_email,
        partner_name=partner_name,
    )
    invite.circle_status = status
    invite.circle_response = serialize_response(raw)
    invite.target_group = target_group
    db.session.commit()

    if status == "buyer_missing":
        # Don't waste an admin alert on a typoed email — the user gets a
        # specific message and is told to retry.
        logger.warning(
            "partner invite: buyer email not in community: invite=%s buyer=%s",
            invite.id, buyer_email,
        )
        return jsonify({"ok": False, "message": BUYER_MISSING_MSG}), 200

    if status in HARD_FAILURE_STATUSES:
        try:
            sent = send_partner_invite_failure_alert(invite, invite.circle_response)
            if sent:
                invite.admin_alert_sent_at = _utcnow()
                db.session.commit()
        except Exception:
            logger.exception("failed to send admin failure alert for invite %s", invite.id)

        logger.warning(
            "partner invite circle add failed: id=%s buyer=%s partner=%s status=%s",
            invite.id, buyer_email, partner_email, status,
        )
        return jsonify({"ok": False, "message": FRIENDLY_FAILURE_MSG}), 200

    if status not in SUCCESS_STATUSES:
        # Defensive: unknown status — treat as a hard failure.
        logger.error("partner invite unknown circle status %r for invite %s", status, invite.id)
        return jsonify({"ok": False, "message": FRIENDLY_FAILURE_MSG}), 200

    # Circle add succeeded → send both emails. Failures here are non-fatal;
    # we flag the row for manual follow-up but still tell the buyer success.
    try:
        partner_sent = send_partner_welcome(invite)
        if partner_sent:
            invite.partner_email_sent_at = _utcnow()
        else:
            invite.needs_followup = True
    except Exception:
        logger.exception("failed to send partner welcome email for invite %s", invite.id)
        invite.needs_followup = True

    try:
        buyer_sent = send_partner_buyer_confirmation(invite)
        if buyer_sent:
            invite.buyer_email_sent_at = _utcnow()
    except Exception:
        logger.exception("failed to send buyer confirmation email for invite %s", invite.id)

    db.session.commit()

    logger.info(
        "partner invite ok: id=%s buyer=%s partner=%s circle=%s target=%s "
        "partner_email_sent=%s buyer_email_sent=%s needs_followup=%s",
        invite.id, buyer_email, partner_email, status, target_group,
        invite.partner_email_sent_at is not None,
        invite.buyer_email_sent_at is not None,
        invite.needs_followup,
    )

    return jsonify({"ok": True, "message": SUCCESS_MSG}), 200
