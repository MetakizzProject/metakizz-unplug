from flask import Blueprint, render_template, request, redirect, url_for
from app.models import Ambassador

leaderboard_bp = Blueprint("leaderboard", __name__)


@leaderboard_bp.route("/leaderboard")
def index():
    """Bare /leaderboard → redirect to gateway."""
    return redirect(url_for("home.index"))


@leaderboard_bp.route("/leaderboard/<channel>")
def show(channel):
    """Isolated leaderboard — only shows one channel, no toggle."""
    if channel not in ("community", "public"):
        return redirect(url_for("home.index"))

    embed = request.args.get("embed", "false").lower() == "true"

    ambassadors = Ambassador.query.filter_by(source=channel).all()

    sorted_ambassadors = sorted(
        ambassadors,
        key=lambda a: (-a.referral_count, a.created_at),
    )

    leaderboard = []
    for i, amb in enumerate(sorted_ambassadors):
        leaderboard.append({
            "rank": i + 1,
            "name": amb.name,
            "profile_picture_url": amb.profile_picture_url,
            "referral_count": amb.referral_count,
        })

    return render_template(
        "leaderboard.html",
        leaderboard=leaderboard,
        channel=channel,
        embed=embed,
    )
