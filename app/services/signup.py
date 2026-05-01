"""
Centralized signup logic shared by /join, the GHL webhook, and any other entry point.

create_signup() encapsulates:
- Idempotent dedup by email (returns existing Ambassador if found)
- Auto-generation of unique referral_code and dashboard_code
- Crediting the referring Ambassador via a new Referral row
- Sending welcome + referral notification + milestone emails
"""

import secrets
import logging
from datetime import datetime, timezone, timedelta
from flask import current_app
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification, PendingReferral


# Velocity throttle — when a referrer accumulates this many referrals in
# the rolling window, further new signups are queued in PendingReferral
# instead of being credited immediately. Admin reviews the queue manually.
VELOCITY_THRESHOLD_COUNT = 5
VELOCITY_WINDOW_MINUTES = 30


def _check_velocity_exceeded(referrer):
    """Return (exceeded, recent_count) for a referrer over the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=VELOCITY_WINDOW_MINUTES)
    recent = (
        Referral.query
        .filter(Referral.ambassador_id == referrer.id)
        .filter(Referral.registered_at >= cutoff)
        .count()
    )
    return (recent >= VELOCITY_THRESHOLD_COUNT, recent)
from app.mailer import (
    send_welcome_email,
    send_first_unplug_email,
    send_guaranteed_prize_email,
    send_first_referral_email,
    send_referral_notification_email,
    send_almost_there_email,
    send_milestone_email,
)


def _rank_in_bucket(ambassador):
    """Compute this ambassador's 1-based rank within their source bucket."""
    bucket = Ambassador.query.filter_by(source=ambassador.source).all()
    ordered = sorted(bucket, key=lambda a: (-a.referral_count, a.created_at))
    return next(
        (i + 1 for i, a in enumerate(ordered) if a.id == ambassador.id),
        len(ordered),
    )

logger = logging.getLogger(__name__)


def _generate_unique_code():
    """Generate an 8-char URL-safe code that doesn't collide with any existing Ambassador."""
    code = secrets.token_urlsafe(6)[:8]
    while (
        Ambassador.query.filter_by(referral_code=code).first()
        or Ambassador.query.filter_by(dashboard_code=code).first()
    ):
        code = secrets.token_urlsafe(6)[:8]
    return code


def create_signup(
    name, email, ref_code=None,
    signup_ip=None, signup_user_agent=None,
    turnstile_status=None, turnstile_codes=None,
    phone_number=None, country_code=None,
):
    """
    Create (or return existing) Ambassador for a PLF signup, and credit the referrer.

    Returns: tuple(ambassador, was_new) where was_new is True if a new Ambassador was created.

    signup_ip / signup_user_agent are stored on both the new Ambassador AND the new
    Referral row (when a referrer is credited). They power the admin's fraud-detection
    badge that flags ambassadors with many referrals from the same IP / user agent.

    turnstile_status / turnstile_codes record the Cloudflare Turnstile verification
    result (see app/services/turnstile.py). They are stored for monitoring; rejection
    based on them happens in the route layer, not here.

    Existing community members imported from Circle still get the welcome email the
    FIRST time they register through the landing (they need their dashboard link),
    gated by the welcome_sent_at idempotency flag.
    """
    name = (name or "").strip()
    email = (email or "").strip().lower()
    ref_code = (ref_code or "").strip() or None

    if not name or not email:
        raise ValueError("name and email are required")

    # 1. Dedup by email — if already an Ambassador, send welcome (if owed) and return.
    existing = Ambassador.query.filter_by(email=email).first()
    if existing:
        app_url = current_app.config["APP_URL"]
        # Send welcome to existing ambassadors who haven't received it yet.
        # Most often: community members imported from Circle who land here via
        # the public landing. They need their dashboard link too.
        if existing.welcome_sent_at is None and existing.unsubscribed_at is None:
            try:
                if send_welcome_email(existing, app_url):
                    existing.welcome_sent_at = datetime.now(timezone.utc)
                    db.session.commit()
            except Exception:
                logger.exception("welcome (to existing) failed for %s", email)
        return existing, False

    # 2. Look up the referring ambassador (if ref_code provided).
    referrer = None
    if ref_code:
        referrer = Ambassador.query.filter_by(referral_code=ref_code).first()
        if referrer is None:
            logger.warning("signup with unknown ref_code=%s for email=%s", ref_code, email)

    # 3. Create the new Ambassador (the signup themselves).
    new_ambassador = Ambassador(
        name=name,
        email=email,
        referral_code=_generate_unique_code(),
        dashboard_code=_generate_unique_code(),
        source="public",
        signup_ip=signup_ip,
        signup_user_agent=signup_user_agent,
        turnstile_status=turnstile_status,
        turnstile_codes=turnstile_codes,
        phone_number=phone_number,
        country_code=country_code,
    )
    db.session.add(new_ambassador)

    # 4. If we have a valid referrer, decide: credit immediately OR queue for review.
    #    Guard against orphan Referral rows from pre-launch data with the same email.
    if referrer is not None:
        existing_referral = Referral.query.filter_by(email=email).first()
        if existing_referral is None:
            # Two ways a signup ends up in the pending queue:
            #   (a) referrer is already under review → ALL their incoming
            #       referrals go to pending until admin clears them
            #   (b) velocity threshold exceeded for this referrer right now
            already_under_review = referrer.under_review_at is not None
            exceeded, recent_count = _check_velocity_exceeded(referrer)
            queue_to_pending = already_under_review or exceeded

            if queue_to_pending:
                if already_under_review:
                    reason = "referrer_under_review"
                else:
                    reason = (
                        f"velocity:{recent_count + 1}_in_{VELOCITY_WINDOW_MINUTES}min "
                        f"(threshold {VELOCITY_THRESHOLD_COUNT})"
                    )
                pending = PendingReferral(
                    referrer_ambassador_id=referrer.id,
                    new_ambassador_id=None,  # set after Ambassador commit below
                    referrer_code=ref_code,
                    name=name,
                    email=email,
                    flagged_reason=reason,
                    signup_ip=signup_ip,
                    signup_user_agent=signup_user_agent,
                    status="pending",
                )
                db.session.add(pending)
                # Flag the referrer for review (idempotent — only set the
                # first time, so we preserve the original timestamp).
                if referrer.under_review_at is None:
                    referrer.under_review_at = datetime.now(timezone.utc)
                    logger.warning(
                        "AMBASSADOR FLAGGED FOR REVIEW: %s (id=%d)",
                        referrer.email, referrer.id,
                    )
                logger.warning(
                    "VELOCITY THROTTLE: referrer=%s (id=%d) recent=%d window=%dmin "
                    "reason=%s queued for new=%s",
                    referrer.email, referrer.id, recent_count,
                    VELOCITY_WINDOW_MINUTES, reason, email,
                )
            else:
                referral = Referral(
                    ambassador_id=referrer.id,
                    name=name,
                    email=email,
                    signup_ip=signup_ip,
                    signup_user_agent=signup_user_agent,
                )
                db.session.add(referral)

    db.session.commit()

    # If we created a PendingReferral, link it back to the new ambassador now
    # that we have its id (post-commit).
    if referrer is not None and Referral.query.filter_by(email=email).first() is None:
        pending = (
            PendingReferral.query
            .filter_by(email=email, status="pending")
            .order_by(PendingReferral.received_at.desc())
            .first()
        )
        if pending and pending.new_ambassador_id is None:
            pending.new_ambassador_id = new_ambassador.id
            db.session.commit()

    # 5. Send welcome email to the new ambassador (with their personal share link).
    app_url = current_app.config["APP_URL"]
    try:
        if send_welcome_email(new_ambassador, app_url):
            new_ambassador.welcome_sent_at = datetime.now(timezone.utc)
            db.session.commit()
    except Exception:
        logger.exception("welcome email failed for %s", email)

    # 6. Referrer notifications (real-time email triggers):
    #    - count 0 -> 1: Email #3 First Unplug
    #    - count 4 -> 5: Email #4 Guaranteed Prize (idempotent via guaranteed_prize_sent_at)
    #    Nudges / reminders / results run on crons, not here.
    if referrer is not None:
        try:
            new_count = Referral.query.filter_by(ambassador_id=referrer.id).count()

            if new_count == 1 and referrer.first_unplug_sent_at is None:
                if send_first_unplug_email(referrer, name, app_url):
                    referrer.first_unplug_sent_at = datetime.now(timezone.utc)
                    db.session.commit()

            elif new_count >= 5 and referrer.guaranteed_prize_sent_at is None:
                rank = _rank_in_bucket(referrer)
                if send_guaranteed_prize_email(referrer, rank, app_url):
                    referrer.guaranteed_prize_sent_at = datetime.now(timezone.utc)
                    db.session.commit()
        except Exception:
            logger.exception("referrer email failed for %s", referrer.email)

    return new_ambassador, True


def _notify_referrer(referrer, registrant_name, app_url):
    """Send the referrer the appropriate notification email based on their referral count."""
    tiers = (
        RewardTier.query
        .filter_by(channel=referrer.source)
        .order_by(RewardTier.sort_order)
        .all()
    )
    next_tier = referrer.next_tier(tiers)
    count = referrer.referral_count

    try:
        if count == 1:
            all_ambassadors = Ambassador.query.filter_by(source=referrer.source).all()
            sorted_ambs = sorted(all_ambassadors, key=lambda a: a.referral_count, reverse=True)
            rank = next(
                (i + 1 for i, a in enumerate(sorted_ambs) if a.id == referrer.id),
                len(sorted_ambs),
            )
            send_first_referral_email(referrer, registrant_name, rank, next_tier, app_url)
        else:
            send_referral_notification_email(referrer, registrant_name, next_tier, app_url)

        # "Almost there" nudge if exactly 1 away from next tier.
        if next_tier and next_tier.threshold - count == 1:
            send_almost_there_email(referrer, next_tier, app_url)
    except Exception:
        logger.exception("referrer notification failed for %s", referrer.email)


def _check_new_milestones(ambassador):
    """Check if this ambassador just crossed a reward tier threshold, send milestone emails."""
    tiers = (
        RewardTier.query
        .filter_by(channel=ambassador.source)
        .order_by(RewardTier.sort_order)
        .all()
    )
    count = ambassador.referral_count
    app_url = current_app.config["APP_URL"]

    for tier in tiers:
        if count >= tier.threshold:
            already_notified = MilestoneNotification.query.filter_by(
                ambassador_id=ambassador.id,
                reward_tier_id=tier.id,
            ).first()

            if not already_notified:
                notification = MilestoneNotification(
                    ambassador_id=ambassador.id,
                    reward_tier_id=tier.id,
                )
                db.session.add(notification)
                db.session.commit()

                next_tier = ambassador.next_tier(tiers)
                try:
                    send_milestone_email(ambassador, tier, next_tier, app_url)
                except Exception:
                    logger.exception("milestone email failed for %s", ambassador.email)
