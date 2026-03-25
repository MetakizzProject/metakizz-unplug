from flask import Blueprint, render_template, request, flash, current_app
from app.models import db, Ambassador, Referral, RewardTier

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard/<code>", methods=["GET", "POST"])
def show(code):
    ambassador = Ambassador.query.filter_by(dashboard_code=code).first_or_404()

    # Handle Instagram share self-report
    if request.method == "POST" and "instagram_share" in request.form:
        proof_url = request.form.get("instagram_url", "").strip()
        ambassador.shared_on_instagram = True
        if proof_url:
            ambassador.instagram_proof_url = proof_url
        db.session.commit()
        flash("Instagram share recorded!", "success")

    # Get reward tiers for this ambassador's channel
    tiers = (
        RewardTier.query
        .filter_by(channel=ambassador.source)
        .order_by(RewardTier.sort_order)
        .all()
    )

    # Get this ambassador's referrals
    referrals = (
        Referral.query
        .filter_by(ambassador_id=ambassador.id)
        .order_by(Referral.registered_at.desc())
        .all()
    )

    # Calculate leaderboard position
    all_ambassadors = (
        Ambassador.query
        .filter_by(source=ambassador.source)
        .all()
    )
    sorted_ambassadors = sorted(all_ambassadors, key=lambda a: a.referral_count, reverse=True)
    rank = next(
        (i + 1 for i, a in enumerate(sorted_ambassadors) if a.id == ambassador.id),
        len(sorted_ambassadors),
    )

    # Current and next tier
    current_tier = ambassador.current_tier(tiers)
    next_tier = ambassador.next_tier(tiers)

    # Progress toward next tier
    progress_pct = 0
    if next_tier:
        prev_threshold = current_tier.threshold if current_tier else 0
        range_size = next_tier.threshold - prev_threshold
        if range_size > 0:
            progress_pct = min(
                100,
                int(((ambassador.referral_count - prev_threshold) / range_size) * 100),
            )

    app_url = current_app.config["APP_URL"]
    referral_url = f"{app_url}/r/{ambassador.referral_code}"

    return render_template(
        "dashboard.html",
        ambassador=ambassador,
        referrals=referrals,
        tiers=tiers,
        current_tier=current_tier,
        next_tier=next_tier,
        progress_pct=progress_pct,
        rank=rank,
        total_ambassadors=len(sorted_ambassadors),
        referral_url=referral_url,
    )
