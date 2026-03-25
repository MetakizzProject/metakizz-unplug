import secrets
import os
import qrcode
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from app.models import db, Ambassador

home_bp = Blueprint("home", __name__)


@home_bp.route("/")
def index():
    """Redirect to community entry point."""
    return redirect(url_for("home.community"))


@home_bp.route("/community", methods=["GET", "POST"])
def community():
    """Email lookup for existing Circle community members."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not email:
            flash("Please enter your email.", "error")
            return render_template("community.html")

        ambassador = Ambassador.query.filter_by(email=email).first()
        if ambassador:
            return redirect(url_for("dashboard.show", code=ambassador.dashboard_code))
        else:
            flash("No dashboard found for that email. Join the challenge instead!", "info")
            return redirect(url_for("home.join"))

    return render_template("community.html")


@home_bp.route("/join", methods=["GET", "POST"])
def join():
    """Public signup for anyone (Instagram, social media, etc.)."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        instagram = request.form.get("instagram", "").strip().lstrip("@")

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("join.html")

        existing = Ambassador.query.filter_by(email=email).first()
        if existing:
            flash("You're already in the challenge!", "info")
            return redirect(url_for("dashboard.show", code=existing.dashboard_code))

        referral_code = secrets.token_urlsafe(6)[:8]
        dashboard_code = secrets.token_urlsafe(6)[:8]

        while Ambassador.query.filter_by(referral_code=referral_code).first():
            referral_code = secrets.token_urlsafe(6)[:8]
        while Ambassador.query.filter_by(dashboard_code=dashboard_code).first():
            dashboard_code = secrets.token_urlsafe(6)[:8]

        ambassador = Ambassador(
            name=name,
            email=email,
            referral_code=referral_code,
            dashboard_code=dashboard_code,
            source="public",
            instagram_handle=instagram if instagram else None,
        )
        db.session.add(ambassador)
        db.session.commit()

        _generate_qr(ambassador, current_app.config["APP_URL"])

        return redirect(url_for("dashboard.show", code=ambassador.dashboard_code))

    return render_template("join.html")


def _generate_qr(ambassador, app_url):
    """Generate a QR code PNG for an ambassador's referral link."""
    referral_url = f"{app_url}/r/{ambassador.referral_code}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(referral_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    qr_dir = os.path.join(current_app.root_path, "static", "qrcodes")
    os.makedirs(qr_dir, exist_ok=True)
    img.save(os.path.join(qr_dir, f"{ambassador.referral_code}.png"))
