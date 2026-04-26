import secrets
from datetime import datetime, timezone
from io import BytesIO
import qrcode
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, send_file
from app.models import db, Ambassador, Referral
from app.mailer import send_welcome_email

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
            total_count = Ambassador.query.count()
            return render_template("community.html", total_count=total_count)

        ambassador = Ambassador.query.filter_by(email=email).first()
        if ambassador:
            return redirect(url_for("dashboard.show", code=ambassador.dashboard_code))
        else:
            flash("No dashboard found for that email. Join the challenge instead!", "info")
            return redirect(url_for("home.join"))

    total_count = Ambassador.query.count()
    return render_template("community.html", total_count=total_count)


@home_bp.route("/join", methods=["GET", "POST"])
def join():
    """Public signup for anyone (Instagram, social media, etc.)."""
    if request.method == "POST":
        from app.services.email_validation import is_disposable_email, client_ip, client_user_agent

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        instagram = request.form.get("instagram", "").strip().lstrip("@")
        ref_code = (request.args.get("ref") or request.form.get("ref") or "").strip()

        if not name or not email:
            flash("Name and email are required.", "error")
            return render_template("join.html")

        # Disposable / temp-mail blocklist — first line of defence against the
        # obvious referral-fraud loop (Bad Actor signs up with mailinator emails).
        if is_disposable_email(email):
            flash("Please use a real email address. Throwaway/temp-mail providers aren't accepted.", "error")
            return render_template("join.html")

        existing = Ambassador.query.filter_by(email=email).first()
        if existing:
            flash("You're already in the challenge!", "info")
            return redirect(url_for("dashboard.show", code=existing.dashboard_code))

        ip = client_ip()
        ua = client_user_agent()

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
            signup_ip=ip,
            signup_user_agent=ua,
        )
        db.session.add(ambassador)

        # If they came in via someone else's referral link, credit that referrer
        # with a Referral row carrying the same IP/UA fingerprint. (This was
        # missing before — /join wasn't crediting referrers!)
        if ref_code:
            referrer = Ambassador.query.filter_by(referral_code=ref_code).first()
            if referrer is not None:
                already = Referral.query.filter_by(email=email).first()
                if already is None:
                    db.session.add(Referral(
                        ambassador_id=referrer.id,
                        name=name,
                        email=email,
                        signup_ip=ip,
                        signup_user_agent=ua,
                    ))

        db.session.commit()

        try:
            if send_welcome_email(ambassador, current_app.config["APP_URL"]):
                ambassador.welcome_sent_at = datetime.now(timezone.utc)
                db.session.commit()
        except Exception:
            current_app.logger.exception("welcome email failed for %s via /join", email)

        return redirect(url_for("dashboard.show", code=ambassador.dashboard_code))

    return render_template("join.html")


@home_bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """One-click email opt-out. GET shows confirmation, POST records the opt-out."""
    ambassador = Ambassador.query.filter_by(unsubscribe_token=token).first()
    if ambassador is None:
        return render_template("unsubscribe.html", state="invalid"), 404

    if request.method == "POST":
        if ambassador.unsubscribed_at is None:
            ambassador.unsubscribed_at = datetime.now(timezone.utc)
            db.session.commit()
        return render_template("unsubscribe.html", state="done", ambassador=ambassador)

    if ambassador.unsubscribed_at is not None:
        return render_template("unsubscribe.html", state="already", ambassador=ambassador)

    return render_template("unsubscribe.html", state="confirm", ambassador=ambassador)


@home_bp.route("/qr/<referral_code>.png")
def qr_image(referral_code):
    """Generate and serve a QR code on the fly (no file storage needed)."""
    ambassador = Ambassador.query.filter_by(referral_code=referral_code).first_or_404()
    landing_url = current_app.config["LANDING_URL"].rstrip("/")
    referral_url = f"{landing_url}?ref={ambassador.referral_code}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(referral_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=f"metakizz-qr-{referral_code}.png")


@home_bp.route("/story/<referral_code>.jpg")
def story_image(referral_code):
    """Generate a 1080x1920 Instagram-story image with the ambassador's QR.

    Uses app/static/story_bg.{png,jpg} as background if present (user-provided
    branded design). Falls back to a default Matrix-style template otherwise.
    """
    from app.services.story_image import generate as generate_story
    ambassador = Ambassador.query.filter_by(referral_code=referral_code).first_or_404()
    landing_url = current_app.config["LANDING_URL"].rstrip("/")
    referral_url = f"{landing_url}?ref={ambassador.referral_code}"
    buf = generate_story(referral_url)
    response = send_file(
        buf,
        mimetype="image/jpeg",
        download_name=f"metakizz-poster-{referral_code}.jpg",
    )
    # Poster content is deterministic per referral_code; cache aggressively.
    response.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return response
