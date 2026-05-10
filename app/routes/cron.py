"""
Cron trigger endpoints — called by an external scheduler (e.g. cron-job.org).

Auth: X-Cron-Secret header MUST match CRON_SECRET config.
All endpoints are idempotent — safe to re-call.

Kill switch: set DISABLE_CRON_EMAILS=1 in env to make every cron-driven
email dispatch return early without sending. The external scheduler will
keep hitting the endpoints (we still want the auth check to log) but no
email goes out. Synchronous emails (welcome, first_unplug, guaranteed
prize) are NOT affected — those fire on user actions, not on time.
"""

import os
import logging
from flask import Blueprint, request, jsonify, current_app
from app.services import cron_logic

cron_bp = Blueprint("cron", __name__, url_prefix="/cron")
logger = logging.getLogger(__name__)


def _kill_switch_active():
    """Return True if the cron-email kill switch is on."""
    return os.getenv("DISABLE_CRON_EMAILS", "").strip().lower() in ("1", "true", "yes", "on")


def _kill_switch_response(endpoint):
    """Standard 'skipped due to kill switch' response."""
    logger.warning("cron/%s skipped: DISABLE_CRON_EMAILS is set", endpoint)
    return jsonify({"ok": True, "skipped": "kill_switch_active", "endpoint": endpoint}), 200


def _auth_ok():
    expected = current_app.config.get("CRON_SECRET", "")
    if not expected:
        logger.warning("CRON_SECRET not configured; refusing all cron calls")
        return False
    got = request.headers.get("X-Cron-Secret", "")
    return got == expected


@cron_bp.route("/daily", methods=["POST", "GET"])
def daily():
    """Daily check: activation nudges (48h+) + midway reminders (7d+).
    Wire this to run every day around 10:00 Madrid.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _kill_switch_active():
        return _kill_switch_response("daily")
    stats = cron_logic.dispatch_daily()
    logger.info("cron/daily result: %s", stats)
    return jsonify({"ok": True, "stats": stats}), 200


@cron_bp.route("/final-48h", methods=["POST", "GET"])
def final_48h():
    """One-shot: Send Final 48h email to everyone active. Schedule 2026-05-05 19:00 Madrid."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _kill_switch_active():
        return _kill_switch_response("final-48h")
    stats = cron_logic.dispatch_final_48h()
    logger.info("cron/final-48h result: %s", stats)
    return jsonify({"ok": True, "stats": stats}), 200


@cron_bp.route("/last-6h", methods=["POST", "GET"])
def last_6h():
    """One-shot: Send Last 6 Hours to count IN (3, 4). Schedule 2026-05-07 13:00 Madrid."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _kill_switch_active():
        return _kill_switch_response("last-6h")
    stats = cron_logic.dispatch_last_6h()
    logger.info("cron/last-6h result: %s", stats)
    return jsonify({"ok": True, "stats": stats}), 200


@cron_bp.route("/results", methods=["POST", "GET"])
def results():
    """One-shot: Send Results to all active ambassadors. Schedule 2026-05-08 10:00 Madrid."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _kill_switch_active():
        return _kill_switch_response("results")
    stats = cron_logic.dispatch_results()
    logger.info("cron/results result: %s", stats)
    return jsonify({"ok": True, "stats": stats}), 200


@cron_bp.route("/you-won", methods=["POST", "GET"])
def you_won():
    """One-shot: Send You Won to all prize winners. Schedule 2026-05-08 10:30 Madrid."""
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401
    if _kill_switch_active():
        return _kill_switch_response("you-won")
    stats = cron_logic.dispatch_you_won()
    logger.info("cron/you-won result: %s", stats)
    return jsonify({"ok": True, "stats": stats}), 200


@cron_bp.route("/buddy-cleanup", methods=["POST", "GET"])
def buddy_cleanup():
    """Daily: hide expired BuddyPosts + send 7-day-before-expiration
    renewal reminders. Idempotent.
    """
    if not _auth_ok():
        return jsonify({"error": "unauthorized"}), 401

    from datetime import datetime, timedelta
    from app.models import db, BuddyPost
    from app.mailer import send_buddy_renewal_reminder

    # Naive UTC: matches what BuddyPost stores in SQLite/Postgres without tz.
    now = datetime.utcnow()

    # 1. Hide expired posts.
    hidden_count = 0
    expired = (
        BuddyPost.query
        .filter(BuddyPost.hidden.is_(False))
        .filter(BuddyPost.expires_at < now)
        .all()
    )
    for p in expired:
        p.hidden = True
        hidden_count += 1
    if expired:
        db.session.commit()

    # 2. Send renewal reminders (7d before expiration, only once per post).
    if _kill_switch_active():
        logger.info("cron/buddy-cleanup: skipping reminder emails (kill switch on)")
        return jsonify({"ok": True, "hidden": hidden_count, "reminded": 0,
                        "skipped_emails": True}), 200

    cutoff_lo = now
    cutoff_hi = now + timedelta(days=7)
    candidates = (
        BuddyPost.query
        .filter(BuddyPost.hidden.is_(False))
        .filter(BuddyPost.renewal_reminder_sent_at.is_(None))
        .filter(BuddyPost.expires_at >= cutoff_lo)
        .filter(BuddyPost.expires_at <= cutoff_hi)
        .all()
    )
    reminded = 0
    for p in candidates:
        try:
            if send_buddy_renewal_reminder(p):
                p.renewal_reminder_sent_at = now
                reminded += 1
        except Exception:
            logger.exception("buddy renewal reminder failed for post %s", p.id)
    if candidates:
        db.session.commit()

    logger.info("cron/buddy-cleanup: hidden=%d reminded=%d", hidden_count, reminded)
    return jsonify({"ok": True, "hidden": hidden_count, "reminded": reminded}), 200
