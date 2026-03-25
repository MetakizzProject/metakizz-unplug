from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification

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
                # Email notification will be handled by tools/check_milestones.py
                # or can be triggered here in Phase 2
