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
from flask import current_app
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification
from app.mailer import (
    send_welcome_email,
    send_first_unplug_email,
    send_first_referral_email,
    send_referral_notification_email,
    send_almost_there_email,
    send_milestone_email,
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


def create_signup(name, email, ref_code=None):
    """
    Create (or return existing) Ambassador for a PLF signup, and credit the referrer.

    Returns: tuple(ambassador, was_new) where was_new is True if a new Ambassador was created.
    """
    name = (name or "").strip()
    email = (email or "").strip().lower()
    ref_code = (ref_code or "").strip() or None

    if not name or not email:
        raise ValueError("name and email are required")

    # 1. Dedup by email — if already an Ambassador, return it (no double credit).
    existing = Ambassador.query.filter_by(email=email).first()
    if existing:
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
    )
    db.session.add(new_ambassador)

    # 4. If we have a valid referrer, credit them with a new Referral row.
    #    Guard against orphan Referral rows from pre-launch data with the same email.
    if referrer is not None:
        existing_referral = Referral.query.filter_by(email=email).first()
        if existing_referral is None:
            referral = Referral(
                ambassador_id=referrer.id,
                name=name,
                email=email,
            )
            db.session.add(referral)

    db.session.commit()

    # 5. Send welcome email to the new ambassador (with their personal share link).
    app_url = current_app.config["APP_URL"]
    try:
        send_welcome_email(new_ambassador, app_url)
    except Exception:
        logger.exception("welcome email failed for %s", email)

    # 6. Referrer notifications. We currently fire only Email #3 (First Unplug)
    #    when this is their first referral (count goes 0 -> 1). The other emails
    #    (#4 Guaranteed Prize, follow-up notifications, milestones) are queued up
    #    for the next iteration and disabled here to avoid sending obsolete copy.
    if referrer is not None:
        try:
            new_count = Referral.query.filter_by(ambassador_id=referrer.id).count()
            if new_count == 1:
                send_first_unplug_email(referrer, name, app_url)
            # Future: count == 5 -> send_guaranteed_prize_email(referrer, app_url)
        except Exception:
            logger.exception("first_unplug email failed for %s", referrer.email)

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
