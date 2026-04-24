from flask import Blueprint, render_template, request, flash, current_app
from app.models import db, Ambassador, Referral

dashboard_bp = Blueprint("dashboard", __name__)


# Top 3 prize catalogue. Each entry has:
#   - place:    badge label (1ST / 2ND / 3RD)
#   - name:     the main reward description
#   - subtitle: a short qualifier (can be None)
#   - amount:   the monetary-value badge text (e.g. "€1,000+"); None for prizes without a value badge
_TOP3_PRIZES = {
    "community": [
        {"place": "1ST", "name": "1 year of MetaDancers, free", "subtitle": None,                 "amount": "€1,000+"},
        {"place": "2ND", "name": "Video feedback on your dancing", "subtitle": "direct from us",  "amount": "€150+"},
        {"place": "3RD", "name": "Personalized MetaKizz hoodie", "subtitle": None,                "amount": "€60+"},
    ],
    "public": [
        {"place": "1ST", "name": "Video feedback on your dancing", "subtitle": "direct from us",  "amount": "€150+"},
        {"place": "2ND", "name": "Personalized MetaKizz hoodie", "subtitle": None,                "amount": "€60+"},
        {"place": "3RD", "name": "Personalized MetaKizz t-shirt", "subtitle": None,               "amount": "€30+"},
    ],
}


def _guaranteed_reward_label(source):
    """Plain-text name of the guaranteed reward unlocked at 5 unplugs."""
    if source == "community":
        return "1 month free of MetaDancers"
    return "Live musicality masterclass with Jesus & Anni (€97)"


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

    # Get this ambassador's referrals
    referrals = (
        Referral.query
        .filter_by(ambassador_id=ambassador.id)
        .order_by(Referral.registered_at.desc())
        .all()
    )

    # Calculate leaderboard position within source bucket
    all_ambassadors = Ambassador.query.filter_by(source=ambassador.source).all()
    sorted_ambassadors = sorted(all_ambassadors, key=lambda a: a.referral_count, reverse=True)
    rank = next(
        (i + 1 for i, a in enumerate(sorted_ambassadors) if a.id == ambassador.id),
        len(sorted_ambassadors),
    )

    count = ambassador.referral_count
    community = (ambassador.source == "community")
    guaranteed_reward = _guaranteed_reward_label(ambassador.source)
    top3_prizes = _TOP3_PRIZES.get(ambassador.source, _TOP3_PRIZES["public"])

    # "Next milestone" message — replaces the old RewardTier system
    if count < 5:
        progress_label = "Guaranteed reward"
        progress_target = 5
        progress_pct = int((count / 5) * 100)
        progress_remaining = 5 - count
        progress_message = f"{progress_remaining} more to lock {guaranteed_reward}"
    else:
        # 5+ unplugs — guaranteed locked. Now climbing toward top 3.
        progress_label = "Top 3 ranking"
        progress_target = None
        progress_pct = 100
        progress_remaining = 0
        if rank in (1, 2, 3):
            progress_message = f"You're currently #{rank}. Keep sharing to hold the spot."
        else:
            # Compute gap to top 3
            third_count = sorted_ambassadors[2].referral_count if len(sorted_ambassadors) >= 3 else 0
            gap = max(0, third_count - count + 1)
            progress_message = f"Reward locked. {gap} more unplug{'s' if gap != 1 else ''} to enter the top 3."

    landing_url = current_app.config["LANDING_URL"].rstrip("/")
    referral_url = f"{landing_url}?ref={ambassador.referral_code}"

    # Global momentum counter: total people registered in the system (ambassadors +
    # referrals of ambassadors who aren't ambassadors themselves yet).
    total_joined = Ambassador.query.count() + Referral.query.count()

    return render_template(
        "dashboard.html",
        ambassador=ambassador,
        referrals=referrals,
        rank=rank,
        total_ambassadors=len(sorted_ambassadors),
        total_joined=total_joined,
        referral_url=referral_url,
        community=community,
        guaranteed_reward=guaranteed_reward,
        top3_prizes=top3_prizes,
        progress_label=progress_label,
        progress_target=progress_target,
        progress_pct=progress_pct,
        progress_remaining=progress_remaining,
        progress_message=progress_message,
    )
