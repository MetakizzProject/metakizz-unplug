import csv
import io
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, session, current_app, Response,
)
from app.models import db, Ambassador, Referral, RewardTier
from app.email import (
    send_welcome_email,
    send_first_referral_email,
    send_referral_notification_email,
    send_milestone_email,
    send_almost_there_email,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


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

    return render_template(
        "admin.html",
        ambassadors=sorted_ambassadors,
        total_referrals=total_referrals,
        community_count=community_count,
        public_count=public_count,
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

        # Create a lightweight copy so we don't touch the DB
        class FakeAmbassador:
            pass

        fake = FakeAmbassador()
        fake.name = ambassador.name
        fake.email = to_email
        fake.referral_code = ambassador.referral_code
        fake.dashboard_code = ambassador.dashboard_code
        fake.referral_count = max(ambassador.referral_count, 3)  # Ensure some count for display

        tiers = RewardTier.query.filter_by(channel=ambassador.source).order_by(RewardTier.sort_order).all()

        # For testing, use first and second tier
        first_tier = tiers[0] if tiers else None
        second_tier = tiers[1] if len(tiers) > 1 else None

        try:
            success = False
            if email_type == "welcome":
                success = send_welcome_email(fake, app_url)
            elif email_type == "first_referral":
                success = send_first_referral_email(fake, "Test Dancer", 1, first_tier, app_url)
            elif email_type == "referral":
                success = send_referral_notification_email(fake, "Test Dancer", first_tier, app_url)
            elif email_type == "milestone" and first_tier:
                success = send_milestone_email(fake, first_tier, second_tier, app_url)
            elif email_type == "almost_there" and first_tier:
                success = send_almost_there_email(fake, first_tier, app_url)
            else:
                flash("No tier data available for this email type. Add tiers in admin first.", "error")
                return redirect(url_for("admin.test_email"))

            if success:
                flash(f"Test '{email_type}' email sent to {to_email}!", "success")
            else:
                flash(f"Failed to send email. Check RESEND_API_KEY env var and Resend dashboard.", "error")
        except Exception as e:
            flash(f"Error: {str(e)}", "error")

        return redirect(url_for("admin.test_email"))

    return render_template("admin_test_email.html")


@admin_bp.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("home.index"))
