"""Buddy Finder — committed Urbankiz dancers map (Phase 1 / MVP).

Public blueprint with:
  GET  /buddies                          → public map page
  GET  /buddies/<dashboard_code>/edit    → publish/edit form (per-Ambassador)
  POST /api/buddies/<dashboard_code>/save → upsert BuddyPost (JSON)
  POST /api/buddies/<dashboard_code>/delete → delete BuddyPost
  POST /api/buddies/<post_id>/contact     → relay contact form (anyone)

Auth model: each ambassador has a unique `dashboard_code` (random secret
URL slug). Possessing the URL == authority to edit/delete their own
BuddyPost. Same pattern as the existing /dashboard/<code> page.

Anti-spam:
  - Per-IP rate limit on save (3 publish attempts/hour)
  - Per-email quota on contacts (max 3 sent/day, max 3 received/day)
  - Honeypot field on both forms ('website')
  - Email regex + disposable blocklist
"""

import os
import re
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from threading import Lock

from flask import Blueprint, render_template, request, jsonify, abort, redirect, url_for

from app.models import db, Ambassador, BuddyPost, BuddyContact
from app.services.geocoding import geocode_city, country_center
from app.services.email_validation import is_valid_email_syntax, is_disposable_email
from app.mailer import send_buddy_contact_relay

logger = logging.getLogger(__name__)

buddies_bp = Blueprint("buddies", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
POST_TTL_DAYS = 60
DAILY_CONTACT_QUOTA = 3
PUBLISH_RATE_LIMIT_PER_HOUR = 3
MAX_MESSAGE_LEN = 300
MAX_CONTACT_MSG_LEN = 1000

# Process-local IP rate limit (good enough for one Render instance).
_publish_attempts = defaultdict(list)  # ip -> [datetime, ...]
_publish_lock = Lock()


def _utcnow():
    """Naive UTC datetime — matches how SQLite/Postgres stores our DateTime
    columns (no tzinfo). Comparing aware vs naive datetimes raises
    TypeError in Python, so we standardize on naive across the buddies module.
    """
    return datetime.utcnow()


def _check_publish_rate_limit(ip):
    """Return True if the IP can publish, False if it's rate-limited."""
    if not ip:
        return True
    now = _utcnow()
    cutoff = now - timedelta(hours=1)
    with _publish_lock:
        recent = [t for t in _publish_attempts[ip] if t > cutoff]
        recent.append(now)
        _publish_attempts[ip] = recent
        return len(recent) <= PUBLISH_RATE_LIMIT_PER_HOUR


def _client_ip():
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr or ""
    )


def _post_to_dict(post, include_email=False):
    """Serialize a BuddyPost to JSON-safe dict for the map. Email is
    NEVER exposed unless include_email=True (only used internally)."""
    avail = (post.availability or "").split(",") if post.availability else []
    avail = [a.strip() for a in avail if a.strip()]
    amb = post.ambassador
    payload = {
        "id": post.id,
        "name": (amb.name if amb else "(unknown)"),
        "city": post.city,
        "country_code": (post.country_code or "").upper(),
        "lat": post.latitude,
        "lng": post.longitude,
        "role": post.role,
        "looking_for_partner": post.looking_for_partner,
        "looking_to_train": post.looking_to_train,
        "looking_to_socialize": post.looking_to_socialize,
        "looking_for_mkot_buddy": post.looking_for_mkot_buddy,
        "festivals_per_year": post.festivals_per_year,
        "dance_level": post.dance_level,
        "years_dancing": post.years_dancing,
        "commitment": post.commitment,
        "goal": post.goal,
        "availability": avail,
        "message": post.message or "",
        "instagram_handle": (amb.instagram_handle if amb else None),
        "profile_picture_url": (amb.profile_picture_url if amb else None),
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "expires_at": post.expires_at.isoformat() if post.expires_at else None,
    }
    if include_email:
        payload["contact_email"] = (
            post.contact_email_override or (amb.email if amb else None)
        )
    return payload


def _public_posts():
    """All published, non-hidden, non-expired posts."""
    now = _utcnow()
    return (
        BuddyPost.query
        .filter(BuddyPost.hidden.is_(False))
        .filter(BuddyPost.expires_at > now)
        .order_by(BuddyPost.published_at.desc())
        .all()
    )


@buddies_bp.route("/buddies", methods=["GET"])
def buddy_map():
    """Public map page — no auth, anyone can view."""
    posts = _public_posts()
    posts_json = [_post_to_dict(p) for p in posts]

    # Aggregate "Ambassadors per country" so the map never looks empty
    # even before published BuddyPosts catch up. We only show counts —
    # never names or emails — so this is privacy-safe.
    from sqlalchemy import func
    country_rows = (
        db.session.query(Ambassador.country_code, func.count(Ambassador.id))
        .filter(Ambassador.country_code.isnot(None))
        .filter(Ambassador.country_code != "")
        .group_by(Ambassador.country_code)
        .all()
    )
    country_aggregates = []
    for cc, count in country_rows:
        center = country_center(cc)
        if center is None:
            continue
        country_aggregates.append({
            "country_code": cc.upper(),
            "count": int(count),
            "lat": center[0],
            "lng": center[1],
        })

    # `?ref=<dashboard_code>` tracks viral attribution for the next publisher.
    ref = (request.args.get("ref") or "").strip()
    return render_template(
        "buddies_map.html",
        posts=posts,
        posts_json=posts_json,
        post_count=len(posts),
        country_aggregates=country_aggregates,
        ref=ref,
    )


@buddies_bp.route("/buddies/<code>/edit", methods=["GET"])
def buddy_edit(code):
    """Form for a specific ambassador to publish/edit their post.

    Auth: anyone with the dashboard URL can edit. Same pattern as the
    /dashboard/<code> route. No login required for Phase 1.
    """
    amb = Ambassador.query.filter_by(dashboard_code=code).first_or_404()
    post = BuddyPost.query.filter_by(ambassador_id=amb.id).first()
    ref = (request.args.get("ref") or "").strip()
    return render_template(
        "buddies_edit.html",
        ambassador=amb,
        post=post,
        ref=ref,
        ttl_days=POST_TTL_DAYS,
    )


@buddies_bp.route("/api/buddies/<code>/save", methods=["POST"])
def buddy_save(code):
    """Create or update a BuddyPost. JSON body."""
    amb = Ambassador.query.filter_by(dashboard_code=code).first()
    if amb is None:
        return jsonify(ok=False, message="Ambassador not found."), 404

    if not _check_publish_rate_limit(_client_ip()):
        return jsonify(ok=False, message="Too many publish attempts. Try again in an hour."), 429

    payload = request.get_json(silent=True) or {}

    # Honeypot
    if (payload.get("website") or "").strip():
        logger.info("buddies: honeypot triggered for ambassador %s", amb.id)
        return jsonify(ok=True, silent=True), 200

    city = (payload.get("city") or "").strip()
    role = (payload.get("role") or "").strip().lower()
    if not city or len(city) > 120:
        return jsonify(ok=False, message="Please enter your city."), 400
    if role not in ("lead", "follower", "ambi"):
        return jsonify(ok=False, message="Pick lead, follower, or ambi."), 400

    looking_for_partner = bool(payload.get("looking_for_partner"))
    looking_to_train = bool(payload.get("looking_to_train"))
    looking_to_socialize = bool(payload.get("looking_to_socialize"))
    looking_for_mkot_buddy = bool(payload.get("looking_for_mkot_buddy"))
    if not (looking_for_partner or looking_to_train or looking_to_socialize or looking_for_mkot_buddy):
        return jsonify(ok=False, message="Pick at least one of the 'looking for' options."), 400

    festivals = (payload.get("festivals_per_year") or "").strip().lower() or None
    dance_level = (payload.get("dance_level") or "").strip().lower() or None
    years_dancing = (payload.get("years_dancing") or "").strip().lower() or None
    commitment = (payload.get("commitment") or "").strip().lower() or None
    goal = (payload.get("goal") or "").strip().lower() or None

    availability_raw = payload.get("availability") or []
    if isinstance(availability_raw, str):
        availability_raw = [a for a in availability_raw.split(",")]
    availability = ",".join(
        sorted({
            a.strip().lower()
            for a in availability_raw
            if a and a.strip().lower() in ("mornings", "afternoons", "evenings", "weekends")
        })
    ) or None

    message = (payload.get("message") or "").strip()
    if message and len(message) > MAX_MESSAGE_LEN:
        message = message[:MAX_MESSAGE_LEN]

    contact_email_override = (payload.get("contact_email_override") or "").strip().lower() or None
    if contact_email_override and not EMAIL_RE.match(contact_email_override):
        return jsonify(ok=False, message="Contact email looks invalid."), 400

    invited_by = (payload.get("invited_by_dashboard_code") or "").strip() or None
    country_code = (amb.country_code or payload.get("country_code") or "").strip().upper() or None

    # Geocode (best-effort).
    lat, lng = geocode_city(city, country_code=country_code)

    now = _utcnow()
    post = BuddyPost.query.filter_by(ambassador_id=amb.id).first()
    is_new = post is None
    if post is None:
        post = BuddyPost(ambassador_id=amb.id, role=role, city=city)
        db.session.add(post)

    post.city = city
    post.country_code = country_code
    post.latitude = lat
    post.longitude = lng
    post.role = role
    post.looking_for_partner = looking_for_partner
    post.looking_to_train = looking_to_train
    post.looking_to_socialize = looking_to_socialize
    post.looking_for_mkot_buddy = looking_for_mkot_buddy
    post.festivals_per_year = festivals
    post.dance_level = dance_level
    post.years_dancing = years_dancing
    post.commitment = commitment
    post.goal = goal
    post.availability = availability
    post.message = message or None
    post.contact_email_override = contact_email_override
    if is_new and invited_by:
        post.invited_by_dashboard_code = invited_by
    post.published_at = now
    post.expires_at = now + timedelta(days=POST_TTL_DAYS)
    post.hidden = False
    post.renewal_reminder_sent_at = None
    db.session.commit()

    logger.info(
        "buddies: %s post id=%s ambassador=%s city=%s lat=%s lng=%s",
        "created" if is_new else "updated", post.id, amb.id, city, lat, lng,
    )
    return jsonify(
        ok=True,
        post_id=post.id,
        is_new=is_new,
        has_geo=lat is not None,
        expires_at=post.expires_at.isoformat(),
    )


@buddies_bp.route("/api/buddies/<code>/delete", methods=["POST"])
def buddy_delete(code):
    amb = Ambassador.query.filter_by(dashboard_code=code).first()
    if amb is None:
        return jsonify(ok=False, message="Ambassador not found."), 404
    post = BuddyPost.query.filter_by(ambassador_id=amb.id).first()
    if post is None:
        return jsonify(ok=True, deleted=False)
    db.session.delete(post)
    db.session.commit()
    logger.info("buddies: deleted post for ambassador %s", amb.id)
    return jsonify(ok=True, deleted=True)


@buddies_bp.route("/api/buddies/<int:post_id>/contact", methods=["POST"])
def buddy_contact(post_id):
    """Relay a contact message to the publisher. Quota: max 3/day per
    sender_email AND max 3/day per publisher (hidden behind email-relay)."""
    post = BuddyPost.query.get(post_id)
    if post is None or post.hidden or post.expires_at < _utcnow():
        return jsonify(ok=False, message="This profile is no longer available."), 404

    payload = request.get_json(silent=True) or {}

    # Honeypot
    if (payload.get("website") or "").strip():
        return jsonify(ok=True, silent=True), 200

    sender_name = (payload.get("name") or "").strip()
    sender_email = (payload.get("email") or "").strip().lower()
    msg = (payload.get("message") or "").strip()

    if not sender_name or len(sender_name) > 120:
        return jsonify(ok=False, message="Please enter your name."), 400
    if not sender_email or not EMAIL_RE.match(sender_email):
        return jsonify(ok=False, message="Please enter a valid email."), 400
    if not msg:
        return jsonify(ok=False, message="Please write a short message."), 400
    if len(msg) > MAX_CONTACT_MSG_LEN:
        msg = msg[:MAX_CONTACT_MSG_LEN]

    # Lightweight email validation (regex + disposable blocklist).
    if not is_valid_email_syntax(sender_email):
        return jsonify(ok=False, message="Email looks invalid."), 400
    if is_disposable_email(sender_email):
        return jsonify(ok=False, message="Please use a real email address."), 400

    # Quota: max 3/day sent by this email.
    cutoff = _utcnow() - timedelta(hours=24)
    sent_today = (
        BuddyContact.query
        .filter(BuddyContact.sender_email.ilike(sender_email))
        .filter(BuddyContact.created_at >= cutoff)
        .count()
    )
    if sent_today >= DAILY_CONTACT_QUOTA:
        return jsonify(
            ok=False,
            message=f"You've reached the {DAILY_CONTACT_QUOTA}/day contact limit. Try again tomorrow.",
        ), 429

    # Quota: max 3/day received by this publisher.
    received_today = (
        BuddyContact.query
        .filter(BuddyContact.target_post_id == post.id)
        .filter(BuddyContact.created_at >= cutoff)
        .count()
    )
    if received_today >= DAILY_CONTACT_QUOTA:
        return jsonify(
            ok=False,
            message="This dancer is already getting a lot of messages today. Try again tomorrow.",
        ), 429

    # Persist before sending so the quota counts even if email fails.
    contact = BuddyContact(
        target_post_id=post.id,
        sender_email=sender_email,
        sender_name=sender_name,
        message=msg,
        sender_ip=_client_ip()[:64],
    )
    db.session.add(contact)
    db.session.commit()

    # Relay email (server-side; sender_email is the reply-to so the
    # publisher can reply directly without exposing their email until they want).
    try:
        sent = bool(send_buddy_contact_relay(post, sender_name, sender_email, msg))
        if sent:
            contact.relay_email_sent_at = _utcnow()
            post.contact_count = (post.contact_count or 0) + 1
            db.session.commit()
    except Exception:
        logger.exception("buddies: relay email failed for post %s", post.id)

    return jsonify(ok=True, sent=True)
