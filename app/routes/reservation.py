"""Public reservation routes for MKOT 3.0.

Flow:
  1. Buyer pays via Stripe Payment Link → Stripe redirects to
     /reservation/form?sid=<checkout_session_id>.
  2. We look up the Reservation row inserted by the Stripe webhook and
     render a short form (name, surname, program, modality, clarity).
  3. POST persists the form, sends the confirmation email, and lands the
     user on /reservation/thanks.

If the redirect arrives before the webhook (race), the form template
auto-refreshes for ~20s while we wait for the row to appear.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, request, render_template, redirect, url_for, abort

from app.models import db, Reservation
from app.mailer import send_reservation_confirmed

logger = logging.getLogger(__name__)

reservation_bp = Blueprint("reservation", __name__)


def _utcnow():
    return datetime.now(timezone.utc)


@reservation_bp.route("/reservation/form", methods=["GET"])
def reservation_form():
    sid = (request.args.get("sid") or "").strip()
    if not sid:
        return render_template(
            "reservation_form.html",
            reservation=None,
            sid="",
            error_message="We couldn't find your payment. If you just paid, hit refresh; otherwise, please get in touch.",
            waiting=False,
        ), 404

    reservation = Reservation.query.filter_by(stripe_session_id=sid).first()
    if reservation is None:
        # Webhook hasn't landed yet. Show a waiting page that auto-refreshes.
        attempt = int(request.args.get("attempt", "0") or "0")
        if attempt >= 6:
            return render_template(
                "reservation_form.html",
                reservation=None,
                sid=sid,
                error_message="We couldn't confirm your payment automatically. If you paid, please reach out and we'll sort it.",
                waiting=False,
            ), 200
        return render_template(
            "reservation_form.html",
            reservation=None,
            sid=sid,
            waiting=True,
            attempt=attempt + 1,
            error_message=None,
        ), 200

    if reservation.form_completed_at is not None:
        # Already submitted — go straight to thanks.
        return redirect(url_for("reservation.reservation_thanks"))

    return render_template(
        "reservation_form.html",
        reservation=reservation,
        sid=sid,
        waiting=False,
        error_message=None,
    )


@reservation_bp.route("/reservation/form", methods=["POST"])
def reservation_form_submit():
    sid = (request.form.get("sid") or "").strip()
    if not sid:
        abort(400, "missing sid")

    reservation = Reservation.query.filter_by(stripe_session_id=sid).first()
    if reservation is None:
        abort(404, "reservation not found")

    if reservation.form_completed_at is not None:
        return redirect(url_for("reservation.reservation_thanks"))

    name = (request.form.get("name") or "").strip()
    surname = (request.form.get("surname") or "").strip()
    program = (request.form.get("program_choice") or "").strip().lower()
    modality = (request.form.get("modality_choice") or "").strip().lower()
    payment_plan = (request.form.get("payment_plan") or "").strip().lower()
    clarity = (request.form.get("clarity") or "").strip().lower()
    notes = (request.form.get("notes") or "").strip() or None

    errors = []
    if not name:
        errors.append("Please enter your first name.")
    if not surname:
        errors.append("Please enter your last name.")
    if program not in ("dancers", "instructors", "not_sure"):
        errors.append("Pick Meta Dancers, Meta Instructors, or Not sure yet.")
    if modality not in ("solo", "duo", "not_sure"):
        errors.append("Pick solo, in a duo, or Not sure yet.")
    if payment_plan not in ("one_payment", "six_installments", "not_sure"):
        errors.append("Pick a payment preference.")
    if clarity not in ("clear", "doubts"):
        errors.append("Let us know how you feel about the next step.")

    if errors:
        return render_template(
            "reservation_form.html",
            reservation=reservation,
            sid=sid,
            waiting=False,
            error_message=" ".join(errors),
            # Echo back what they typed so they don't lose progress.
            form_values={
                "name": name, "surname": surname,
                "program_choice": program, "modality_choice": modality,
                "payment_plan": payment_plan,
                "clarity": clarity, "notes": notes or "",
            },
        ), 400

    reservation.name = name
    reservation.surname = surname
    reservation.program_choice = program
    reservation.modality_choice = modality
    reservation.payment_plan = payment_plan
    reservation.clarity = clarity
    reservation.notes = notes
    reservation.form_completed_at = _utcnow()
    db.session.commit()

    # Send confirmation email — best effort, do not block the redirect.
    try:
        sent = send_reservation_confirmed(reservation)
        if sent:
            reservation.confirmation_email_sent_at = _utcnow()
            db.session.commit()
    except Exception:
        logger.exception("failed to send reservation confirmation email for %s", reservation.email)

    return redirect(url_for("reservation.reservation_thanks"))


@reservation_bp.route("/reservation/thanks", methods=["GET"])
def reservation_thanks():
    return render_template("reservation_thanks.html")


@reservation_bp.route("/reservation/preview", methods=["GET"])
def reservation_preview():
    """Admin-only preview helper: redirects to /reservation/form?sid=<latest>
    where <latest> is the most recent paid-but-not-completed reservation. If
    none exist, redirects with a placeholder so the waiting page renders."""
    from flask import session
    if not session.get("is_admin"):
        # Lightweight protection — admin panel only links to this from /admin/raffle.
        return redirect(url_for("admin.login"))
    pending = (
        Reservation.query
        .filter(Reservation.paid_at.isnot(None))
        .filter(Reservation.form_completed_at.is_(None))
        .order_by(Reservation.paid_at.desc())
        .first()
    )
    sid = pending.stripe_session_id if pending else "cs_preview_none"
    return redirect(url_for("reservation.reservation_form", sid=sid))
