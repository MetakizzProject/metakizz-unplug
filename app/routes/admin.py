import csv
import io
import logging
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, session, current_app, Response,
)
from datetime import datetime, timezone
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification
from app.mailer import (
    send_welcome_email,
    send_activation_nudge_email,
    send_first_unplug_email,
    send_guaranteed_prize_email,
    send_midway_reminder_email,
    send_final_48h_email,
    send_last_6h_email,
    send_results_announcement_email,
    send_you_won_email,
    # legacy:
    send_first_referral_email,
    send_referral_notification_email,
    send_milestone_email,
    send_almost_there_email,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger(__name__)


@admin_bp.before_request
def require_admin():
    if request.endpoint == "admin.login":
        return
    if not session.get("is_admin"):
        return redirect(url_for("admin.login"))


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == current_app.config["ADMIN_PASSWORD"]:
            session["is_admin"] = True
            return redirect(url_for("admin.index"))
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@admin_bp.route("/")
def index():
    channel = request.args.get("channel", "all")

    if channel == "all":
        ambassadors = Ambassador.query.all()
    else:
        ambassadors = Ambassador.query.filter_by(source=channel).all()

    sorted_ambassadors = sorted(ambassadors, key=lambda a: a.referral_count, reverse=True)

    total_referrals = Referral.query.count()
    community_count = Ambassador.query.filter_by(source="community").count()
    public_count = Ambassador.query.filter_by(source="public").count()
    prizes_earned = MilestoneNotification.query.count()
    prizes_pending = MilestoneNotification.query.filter_by(delivered=False).count()

    return render_template(
        "admin.html",
        ambassadors=sorted_ambassadors,
        total_referrals=total_referrals,
        community_count=community_count,
        public_count=public_count,
        prizes_earned=prizes_earned,
        prizes_pending=prizes_pending,
        channel=channel,
    )


@admin_bp.route("/tiers", methods=["GET", "POST"])
def tiers():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            tier = RewardTier(
                name=request.form["name"],
                channel=request.form["channel"],
                threshold=int(request.form["threshold"]),
                reward=request.form["reward"],
                sort_order=int(request.form.get("sort_order", 0)),
            )
            db.session.add(tier)
            db.session.commit()
            flash(f"Tier '{tier.name}' added.", "success")

        elif action == "delete":
            tier_id = int(request.form["tier_id"])
            tier = RewardTier.query.get_or_404(tier_id)
            db.session.delete(tier)
            db.session.commit()
            flash("Tier deleted.", "success")

        return redirect(url_for("admin.tiers"))

    community_tiers = RewardTier.query.filter_by(channel="community").order_by(RewardTier.sort_order).all()
    public_tiers = RewardTier.query.filter_by(channel="public").order_by(RewardTier.sort_order).all()

    return render_template("admin_tiers.html", community_tiers=community_tiers, public_tiers=public_tiers)


@admin_bp.route("/rewards")
def rewards():
    """View all earned rewards with delivery tracking."""
    channel = request.args.get("channel", "all")
    status = request.args.get("status", "all")

    query = (
        db.session.query(MilestoneNotification, Ambassador, RewardTier)
        .join(Ambassador, MilestoneNotification.ambassador_id == Ambassador.id)
        .join(RewardTier, MilestoneNotification.reward_tier_id == RewardTier.id)
    )

    if channel != "all":
        query = query.filter(Ambassador.source == channel)
    if status == "pending":
        query = query.filter(MilestoneNotification.delivered == False)
    elif status == "delivered":
        query = query.filter(MilestoneNotification.delivered == True)

    results = query.order_by(MilestoneNotification.sent_at.desc()).all()

    # Stats
    total_earned = MilestoneNotification.query.count()
    total_delivered = MilestoneNotification.query.filter_by(delivered=True).count()
    total_pending = total_earned - total_delivered

    return render_template(
        "admin_rewards.html",
        results=results,
        total_earned=total_earned,
        total_delivered=total_delivered,
        total_pending=total_pending,
        channel=channel,
        status=status,
    )


@admin_bp.route("/rewards/deliver", methods=["POST"])
def deliver_reward():
    """Mark a reward as delivered."""
    notification_id = int(request.form["notification_id"])
    notification = MilestoneNotification.query.get_or_404(notification_id)
    notification.delivered = True
    notification.delivered_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Reward marked as delivered!", "success")
    return redirect(url_for("admin.rewards", channel=request.args.get("channel", "all"), status=request.args.get("status", "all")))


@admin_bp.route("/rewards/undeliver", methods=["POST"])
def undeliver_reward():
    """Undo delivery marking."""
    notification_id = int(request.form["notification_id"])
    notification = MilestoneNotification.query.get_or_404(notification_id)
    notification.delivered = False
    notification.delivered_at = None
    db.session.commit()
    flash("Delivery status reverted.", "success")
    return redirect(url_for("admin.rewards", channel=request.args.get("channel", "all"), status=request.args.get("status", "all")))


@admin_bp.route("/export")
def export_csv():
    channel = request.args.get("channel", "all")

    if channel == "all":
        ambassadors = Ambassador.query.all()
    else:
        ambassadors = Ambassador.query.filter_by(source=channel).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Email", "Source", "Referral Code", "Referrals", "Instagram", "Shared on IG", "Joined"])

    for amb in sorted(ambassadors, key=lambda a: a.referral_count, reverse=True):
        writer.writerow([
            amb.name,
            amb.email,
            amb.source,
            amb.referral_code,
            amb.referral_count,
            amb.instagram_handle or "",
            "Yes" if amb.shared_on_instagram else "No",
            amb.created_at.strftime("%Y-%m-%d"),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=metakizz_ambassadors_{channel}.csv"},
    )


@admin_bp.route("/export-referrals")
def export_referrals():
    referrals = (
        db.session.query(Referral, Ambassador)
        .join(Ambassador, Referral.ambassador_id == Ambassador.id)
        .order_by(Referral.registered_at.desc())
        .all()
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Referral Name", "Referral Email", "Referred By", "Ambassador Email", "Channel", "Date"])

    for ref, amb in referrals:
        writer.writerow([
            ref.name,
            ref.email,
            amb.name,
            amb.email,
            amb.source,
            ref.registered_at.strftime("%Y-%m-%d %H:%M"),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=metakizz_referrals.csv"},
    )


@admin_bp.route("/test-email", methods=["GET", "POST"])
def test_email():
    """Send test emails to verify all templates work."""
    if request.method == "POST":
        email_type = request.form.get("type")
        to_email = request.form.get("email", "").strip()
        app_url = current_app.config["APP_URL"]

        if not to_email:
            flash("Enter an email address.", "error")
            return redirect(url_for("admin.test_email"))

        # Use first ambassador as test data but create a fake copy to avoid DB changes
        ambassador = Ambassador.query.first()
        if not ambassador:
            flash("No ambassadors in database to use as test data.", "error")
            return redirect(url_for("admin.test_email"))

        # Create a lightweight copy so we don't touch the DB.
        # The fake mirrors the Ambassador interface used by the new mailer functions.
        class FakeAmbassador:
            pass

        fake = FakeAmbassador()
        fake.name = ambassador.name or "Tester"
        fake.email = to_email
        fake.referral_code = ambassador.referral_code
        fake.dashboard_code = ambassador.dashboard_code
        fake.source = ambassador.source or "public"
        fake.referral_count = 1  # for first_unplug test
        fake.unsubscribe_token = ambassador.unsubscribe_token
        fake.unsubscribed_at = None

        # Variant override: query param ?source=community/public lets you preview both
        variant = request.form.get("source") or request.args.get("source")
        if variant in ("community", "public"):
            fake.source = variant

        # Dummy stats used by the results email
        top3_demo = [
            {"name": "Maria", "count": 23},
            {"name": "Pedro", "count": 19},
            {"name": "Laura", "count": 14},
        ]

        try:
            success = False

            if email_type == "welcome":
                fake.referral_count = 0
                success = send_welcome_email(fake, app_url)

            elif email_type == "activation_nudge":
                fake.referral_count = 0
                success = send_activation_nudge_email(fake, app_url)

            elif email_type == "first_unplug":
                fake.referral_count = 1
                success = send_first_unplug_email(fake, "Maria Lopez", app_url)

            elif email_type == "guaranteed_prize":
                fake.referral_count = 5
                success = send_guaranteed_prize_email(fake, position=4, app_url=app_url)

            elif email_type == "midway_reminder":
                fake.referral_count = 3
                success = send_midway_reminder_email(fake, position=12, days_left=7, app_url=app_url)

            elif email_type == "final_48h":
                fake.referral_count = 4
                success = send_final_48h_email(fake, position=8, gap_to_top3=2, app_url=app_url)

            elif email_type == "last_6h":
                fake.referral_count = 4
                success = send_last_6h_email(fake, app_url)

            elif email_type == "results":
                fake.referral_count = 7
                success = send_results_announcement_email(
                    fake, total_ambassadors=196, total_unplugs=380, total_countries=27,
                    top3=top3_demo, app_url=app_url,
                )

            elif email_type == "you_won_guaranteed":
                fake.referral_count = 8
                success = send_you_won_email(fake, position=None, app_url=app_url)  # rama 1

            elif email_type == "you_won_top3_guaranteed":
                fake.referral_count = 14
                success = send_you_won_email(fake, position=2, app_url=app_url)  # rama 2

            elif email_type == "you_won_top3_only":
                fake.referral_count = 4
                success = send_you_won_email(fake, position=3, app_url=app_url)  # rama 3 edge case

            else:
                flash(f"Unknown email type: {email_type}", "error")
                return redirect(url_for("admin.test_email"))

            if success:
                flash(f"Test '{email_type}' email sent to {to_email} (source={fake.source})!", "success")
            else:
                flash("Failed to send email. Check RESEND_API_KEY env var and Resend dashboard.", "error")
        except Exception as e:
            logger.exception("test email failed")
            flash(f"Error: {str(e)}", "error")

        return redirect(url_for("admin.test_email"))

    return render_template("admin_test_email.html")


@admin_bp.route("/cron-status", methods=["GET"])
def cron_status():
    """Dashboard of cron-driven email sends. Shows counters per email + manual
    force-send buttons (fallback if the external scheduler fails)."""
    totals = {
        "activation_nudge_sent": Ambassador.query.filter(Ambassador.activation_nudge_sent_at.isnot(None)).count(),
        "midway_sent": Ambassador.query.filter(Ambassador.midway_sent_at.isnot(None)).count(),
        "final_48h_sent": Ambassador.query.filter(Ambassador.final_48h_sent_at.isnot(None)).count(),
        "last_6h_sent": Ambassador.query.filter(Ambassador.last_6h_sent_at.isnot(None)).count(),
        "results_sent": Ambassador.query.filter(Ambassador.results_sent_at.isnot(None)).count(),
        "you_won_sent": Ambassador.query.filter(Ambassador.you_won_sent_at.isnot(None)).count(),
    }
    total_ambassadors = Ambassador.query.count()
    return render_template(
        "admin_cron_status.html",
        totals=totals,
        total_ambassadors=total_ambassadors,
    )


@admin_bp.route("/cron-force/<job>", methods=["POST"])
def cron_force(job):
    """Manually trigger a cron job from the admin UI (fallback if external cron fails).
    Bypasses the CRON_SECRET because we're already admin-authed.
    """
    from app.services import cron_logic
    job_map = {
        "daily": cron_logic.dispatch_daily,
        "final-48h": cron_logic.dispatch_final_48h,
        "last-6h": cron_logic.dispatch_last_6h,
        "results": cron_logic.dispatch_results,
        "you-won": cron_logic.dispatch_you_won,
    }
    fn = job_map.get(job)
    if fn is None:
        flash(f"Unknown cron job: {job}", "error")
        return redirect(url_for("admin.cron_status"))
    try:
        stats = fn()
        flash(f"cron/{job} ran. Stats: {stats}", "success")
        logger.warning("ADMIN force-ran cron/%s: %s", job, stats)
    except Exception as e:
        flash(f"cron/{job} failed: {e}", "error")
        logger.exception("admin force cron/%s failed", job)
    return redirect(url_for("admin.cron_status"))


@admin_bp.route("/backfill-guaranteed", methods=["POST"])
def backfill_guaranteed():
    """Send Email #4 (Guaranteed Prize) to any ambassador who already hit 5+ unplugs
    but didn't receive it yet (because the trigger was wired after they reached 5).

    Idempotent via guaranteed_prize_sent_at — safe to re-run.
    """
    from app.mailer import send_guaranteed_prize_email
    from datetime import datetime, timezone
    from app.services.signup import _rank_in_bucket
    app_url = current_app.config["APP_URL"]

    # Find all ambassadors with count >= 5 and no guaranteed_prize yet
    candidates = [
        a for a in Ambassador.query.all()
        if a.referral_count >= 5 and a.guaranteed_prize_sent_at is None and a.unsubscribed_at is None
    ]

    sent = 0
    failed = 0
    for amb in candidates:
        try:
            rank = _rank_in_bucket(amb)
            if send_guaranteed_prize_email(amb, rank, app_url):
                amb.guaranteed_prize_sent_at = datetime.now(timezone.utc)
                db.session.commit()
                sent += 1
            else:
                failed += 1
        except Exception:
            logger.exception("backfill #4 failed for %s", amb.email)
            failed += 1

    if sent or failed:
        flash(f"Backfill complete. Sent: {sent}. Failed: {failed}. Candidates found: {len(candidates)}.", "success")
    else:
        flash("No candidates found — nobody at 5+ without the guaranteed prize email.", "info")
    logger.warning("ADMIN BACKFILL #4: sent=%d failed=%d candidates=%d", sent, failed, len(candidates))
    return redirect(url_for("admin.index"))


@admin_bp.route("/ambassadors/<int:ambassador_id>/reset", methods=["POST"])
def reset_ambassador(ambassador_id):
    """Per-ambassador reset: delete only this ambassador's referrals + milestone notifs.
    Keeps the ambassador row itself. Their counter goes back to 0.
    """
    amb = Ambassador.query.get_or_404(ambassador_id)
    n_refs = Referral.query.filter_by(ambassador_id=amb.id).count()
    n_notifs = MilestoneNotification.query.filter_by(ambassador_id=amb.id).count()
    MilestoneNotification.query.filter_by(ambassador_id=amb.id).delete()
    Referral.query.filter_by(ambassador_id=amb.id).delete()
    db.session.commit()
    flash(f"Reset {amb.name}: deleted {n_refs} referrals, {n_notifs} milestone notifs.", "success")
    logger.warning("ADMIN per-user RESET: ambassador_id=%d (%s)", amb.id, amb.email)
    return redirect(url_for("admin.index", channel=request.args.get("channel", "all")))


@admin_bp.route("/ambassadors/<int:ambassador_id>/delete", methods=["POST"])
def delete_ambassador(ambassador_id):
    """Per-ambassador delete: removes the ambassador entirely (and their referrals + notifs).
    Use with care — irreversible.
    """
    amb = Ambassador.query.get_or_404(ambassador_id)
    name = amb.name
    email = amb.email
    n_refs = Referral.query.filter_by(ambassador_id=amb.id).count()
    MilestoneNotification.query.filter_by(ambassador_id=amb.id).delete()
    Referral.query.filter_by(ambassador_id=amb.id).delete()
    db.session.delete(amb)
    db.session.commit()
    flash(f"Deleted {name} <{email}> ({n_refs} referrals removed too).", "success")
    logger.warning("ADMIN per-user DELETE: ambassador_id=%d (%s)", ambassador_id, email)
    return redirect(url_for("admin.index", channel=request.args.get("channel", "all")))


@admin_bp.route("/reset-test-data", methods=["GET", "POST"])
def reset_test_data():
    """Wipe test data: all referrals, all milestone notifications, all public ambassadors.
    Keeps community ambassadors (the Circle import) and any unsubscribe opt-outs.

    Use this AFTER deploy and BEFORE launch to clean any test pollution from prod.
    Requires the confirmation phrase to be typed exactly to prevent accidents.
    """
    CONFIRM_PHRASE = "YES_DELETE_ALL_TESTS"

    if request.method == "POST":
        if request.form.get("confirm", "").strip() != CONFIRM_PHRASE:
            flash(f'Confirmation phrase incorrect. Type exactly: {CONFIRM_PHRASE}', "error")
            return redirect(url_for("admin.reset_test_data"))

        before_referrals = Referral.query.count()
        before_milestones = MilestoneNotification.query.count()
        before_public = Ambassador.query.filter_by(source="public").count()

        # Order matters: clear FK-referencing tables first.
        MilestoneNotification.query.delete()
        Referral.query.delete()
        Ambassador.query.filter_by(source="public").delete()
        db.session.commit()

        flash(
            f"Reset complete. Deleted: {before_referrals} referrals, "
            f"{before_milestones} milestone notifications, "
            f"{before_public} public ambassadors. "
            f"Community ambassadors preserved.",
            "success",
        )
        logger.warning(
            "ADMIN RESET: deleted %d referrals, %d milestones, %d public ambassadors",
            before_referrals, before_milestones, before_public,
        )
        return redirect(url_for("admin.reset_test_data"))

    counts = {
        "total_amb": Ambassador.query.count(),
        "community": Ambassador.query.filter_by(source="community").count(),
        "public": Ambassador.query.filter_by(source="public").count(),
        "referrals": Referral.query.count(),
        "milestones": MilestoneNotification.query.count(),
        "unsubscribed": Ambassador.query.filter(Ambassador.unsubscribed_at.isnot(None)).count(),
    }
    public_ambs = (
        Ambassador.query
        .filter_by(source="public")
        .order_by(Ambassador.created_at.desc())
        .all()
    )
    return render_template(
        "admin_reset.html",
        counts=counts,
        public_ambs=public_ambs,
        confirm_phrase=CONFIRM_PHRASE,
    )


@admin_bp.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("home.index"))
