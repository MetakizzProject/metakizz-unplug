from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification
from app.mailer import (
    send_first_referral_email,
    send_referral_notification_email,
    send_milestone_email,
    send_almost_there_email,
)

register_bp = Blueprint("register", __name__)


@register_bp.route("/r/<code>", methods=["GET", "POST"])
def landing(code):
    ambassador = Ambassador.query.filter_by(referral_code=code).first_or_404()

    total_registered = Referral.query.count()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not name or not email:
            flash("Please fill in your name and email.", "error")
            return render_template("landing.html", ambassador=ambassador, total_registered=total_registered)

        # Check if this email already registered
        existing = Referral.query.filter_by(email=email).first()
        if existing:
            flash("This email is already registered for the masterclass!", "info")
            return render_template("landing.html", ambassador=ambassador, registered=True, total_registered=total_registered)

        # Also check if this person is already an ambassador
        existing_ambassador = Ambassador.query.filter_by(email=email).first()
        if existing_ambassador:
            flash("You're already part of the challenge!", "info")
            return render_template("landing.html", ambassador=ambassador, registered=True, total_registered=total_registered)

        referral = Referral(
            ambassador_id=ambassador.id,
            name=name,
            email=email,
        )
        db.session.add(referral)
        db.session.commit()

        app_url = current_app.config["APP_URL"]
        count = ambassador.referral_count

        # Get tiers for email context
        tiers = (
            RewardTier.query
            .filter_by(channel=ambassador.source)
            .order_by(RewardTier.sort_order)
            .all()
        )
        next_tier = ambassador.next_tier(tiers)

        # Send appropriate referral email
        if count == 1:
            all_ambassadors = Ambassador.query.filter_by(source=ambassador.source).all()
            sorted_ambs = sorted(all_ambassadors, key=lambda a: a.referral_count, reverse=True)
            rank = next((i + 1 for i, a in enumerate(sorted_ambs) if a.id == ambassador.id), len(sorted_ambs))
            send_first_referral_email(ambassador, name, rank, next_tier, app_url)
        else:
            send_referral_notification_email(ambassador, name, next_tier, app_url)

        # Send "almost there" nudge if 1 away from next tier
        if next_tier and next_tier.threshold - count == 1:
            send_almost_there_email(ambassador, next_tier, app_url)

        # Check if ambassador hit a new milestone
        _check_new_milestones(ambassador)

        return render_template(
            "landing.html",
            ambassador=ambassador,
            registered=True,
            total_registered=total_registered + 1,
            registrant_name=name,
            registrant_email=email,
        )

    return render_template("landing.html", ambassador=ambassador, total_registered=total_registered)


def _check_new_milestones(ambassador):
    """Check if this ambassador just crossed a reward tier threshold."""
    tiers = (
        RewardTier.query
        .filter_by(channel=ambassador.source)
        .order_by(RewardTier.sort_order)
        .all()
    )
    count = ambassador.referral_count

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

                # Send milestone email
                app_url = current_app.config["APP_URL"]
                next_tier = ambassador.next_tier(tiers)
                send_milestone_email(ambassador, tier, next_tier, app_url)
