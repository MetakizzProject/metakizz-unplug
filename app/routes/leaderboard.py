from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, current_app
from app.models import Ambassador, Referral

leaderboard_bp = Blueprint("leaderboard", __name__)


@leaderboard_bp.route("/leaderboard")
def index():
    """Bare /leaderboard → redirect to gateway."""
    return redirect(url_for("home.index"))


@leaderboard_bp.route("/leaderboard/<channel>")
def show(channel):
    """Leaderboard for a source bucket. Top 10 only, first name display.

    Optional ?from=<dashboard_code> query param identifies the viewing
    ambassador, so we can surface their personal position even if they
    are outside the top 10.
    """
    if channel not in ("community", "public"):
        return redirect(url_for("home.index"))

    embed = request.args.get("embed", "false").lower() == "true"
    from_code = (request.args.get("from") or "").strip()

    ambassadors = Ambassador.query.filter_by(source=channel).all()

    # Sort by referral count desc, breaking ties by who joined first.
    sorted_all = sorted(
        ambassadors,
        key=lambda a: (-a.referral_count, a.created_at),
    )
    total_in_bucket = len(sorted_all)

    # Cap the public board to top 10.
    top10_ambassadors = sorted_all[:10]

    leaderboard = []
    for i, amb in enumerate(top10_ambassadors):
        first_name = amb.name.strip().split()[0] if amb.name and amb.name.strip() else "?"
        leaderboard.append({
            "rank": i + 1,
            "name": first_name,
            "profile_picture_url": amb.profile_picture_url,
            "referral_count": amb.referral_count,
        })

    # If the viewer is known (came from their dashboard), compute their personal rank
    viewer_rank = None
    viewer_count = None
    viewer_dashboard_code = None
    if from_code:
        viewer = Ambassador.query.filter_by(dashboard_code=from_code).first()
        if viewer and viewer.source == channel:
            viewer_dashboard_code = viewer.dashboard_code
            viewer_count = viewer.referral_count
            for i, amb in enumerate(sorted_all):
                if amb.id == viewer.id:
                    viewer_rank = i + 1
                    break

    # Context strip data
    total_joined = Ambassador.query.count()
    close_str = current_app.config.get("CAMPAIGN_CLOSE_ISO", "2026-05-07T19:00:00+02:00")
    try:
        close_dt = datetime.fromisoformat(close_str)
        now = datetime.now(close_dt.tzinfo)
        delta = close_dt - now
        days_left = max(0, delta.days)
    except Exception:
        days_left = None

    return render_template(
        "leaderboard.html",
        leaderboard=leaderboard,
        channel=channel,
        embed=embed,
        total_in_bucket=total_in_bucket,
        total_joined=total_joined,
        days_left=days_left,
        viewer_rank=viewer_rank,
        viewer_count=viewer_count,
        viewer_dashboard_code=viewer_dashboard_code,
    )
