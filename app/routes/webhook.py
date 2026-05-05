"""
External webhook endpoints:

- /api/webhook/signup → Go High Level posts new PLF signups here.
- /api/webhook/resend → Resend posts email lifecycle events (sent, opened,
                       clicked, bounced, complained, delivered) so we can
                       compute open/click rates per template in the admin.
"""

import json
import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app, make_response
from sqlalchemy import func
from app.services.signup import create_signup
from app.services.turnstile import (
    verify_token as verify_turnstile,
    extract_token_from_payload as extract_turnstile_token,
    is_enforce_mode as turnstile_enforce_mode,
    record_rejection as record_turnstile_rejection,
    STATUS_VALID, STATUS_INVALID, STATUS_MISSING,
)
from app.models import db, EmailEvent, Ambassador, LeadEvent

logger = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__)


def _pluck(payload, *keys):
    """Return the first non-empty value found by walking the payload at the given keys.

    GHL webhooks can deliver custom data either at the top level or nested under
    keys like 'custom_data', 'customData', 'contact', etc. This walks all the
    common shapes and returns whatever it finds first.
    """
    if not isinstance(payload, dict):
        return ""

    # 1. Direct top-level lookup.
    for key in keys:
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 2. Nested under common GHL containers.
    for container in ("custom_data", "customData", "data", "contact", "Contact"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            for key in keys:
                val = nested.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    return ""


def _extract_signup_fields(payload):
    """Pull name, email, and ref code from a GHL webhook payload in any reasonable shape."""
    # Email — most stable identifier across GHL payload variants.
    email = _pluck(payload, "email", "Email", "contact_email", "contactEmail")

    # Name — try full name first, then fall back to first/last concatenation.
    name = _pluck(
        payload,
        "name", "full_name", "fullName", "Full Name", "contact_name", "contactName",
    )
    if not name:
        first = _pluck(payload, "first_name", "firstName", "First Name")
        last = _pluck(payload, "last_name", "lastName", "Last Name")
        name = (first + " " + last).strip()

    # Ref code (optional) — referrer's referral_code, sent through the GHL form's hidden field.
    ref_code = _pluck(payload, "ref", "referred_by", "referredBy", "referral_code")

    return name, email, ref_code


def _extract_client_ip(payload):
    """Pull the original client IP from a GHL payload.

    GHL itself doesn't forward the user's IP in headers (we'd see GHL's IP),
    so the Lovable form has to capture it client-side (e.g. via api.ipify.org)
    and pass it through as a JSON field. We tolerate a few naming variants.
    Returns "" if not present, or a truncated string (max 64 chars).
    """
    ip = _pluck(
        payload,
        "client_ip", "clientIp", "user_ip", "userIp", "ip",
    )
    return ip[:64] if ip else ""


def _extract_attribution(payload):
    """Pull UTM/click-id fields from a GHL signup payload.

    Empty/missing fields are returned as None. GHL must be configured to
    forward these as custom data fields in the outbound webhook (Lovable
    captures them client-side from the URL into a hidden form field, then
    GHL passes them through).
    """
    keys = (
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "fbclid", "gclid", "ttclid",
    )
    out = {}
    for k in keys:
        v = _pluck(payload, k, k.replace("_", ""))
        out[k] = v or None
    return out


def _extract_phone(payload):
    """Pull the user's phone number from a GHL webhook payload.

    GHL forwards `{{contact.phone}}` (or similar). We tolerate variants;
    return the raw string for the parser to normalize.
    """
    return _pluck(
        payload,
        "phone", "Phone", "phone_number", "phoneNumber",
        "contact_phone", "contactPhone",
    )


@webhook_bp.route("/api/webhook/signup", methods=["POST"])
def ghl_signup():
    """
    Receives a signup notification from GHL after a user fills the PLF form on Lovable.

    Tolerates several payload shapes (top-level keys, custom_data wrapper, contact wrapper).
    Auth: shared secret in X-Webhook-Secret header.
    """
    expected_secret = current_app.config.get("GHL_WEBHOOK_SECRET", "")
    if not expected_secret or request.headers.get("X-Webhook-Secret") != expected_secret:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}

    # Log the raw payload so we can see exactly what GHL sends. Truncated to avoid
    # log spam if a future caller sends a giant body.
    try:
        raw_preview = json.dumps(payload)[:2000]
    except Exception:
        raw_preview = repr(payload)[:2000]
    logger.info("GHL webhook payload: %s", raw_preview)

    name, email, ref_code = _extract_signup_fields(payload)

    # Extract the user's real IP — Lovable pre-fetches it (api.ipify.org)
    # and forwards via GHL custom field. Fall back to the request header
    # (which on the GHL path is GHL's own IP, useless for fraud, but kept
    # as a safety net for future direct callers).
    client_ip = _extract_client_ip(payload)
    if not client_ip:
        fwd = request.headers.get("X-Forwarded-For", "")
        client_ip = fwd.split(",")[0].strip()[:64] if fwd else ""

    # Honeypot check — if either trap field arrives non-empty, it's a bot.
    # We accept (200) so the bot thinks it worked, but skip processing.
    honeypot_website = _pluck(payload, "website")
    honeypot_phone_number = _pluck(payload, "phone_number")
    if honeypot_website or honeypot_phone_number:
        logger.warning(
            "honeypot triggered on /api/webhook/signup: website=%r phone_number=%r email=%r",
            honeypot_website, honeypot_phone_number, email,
        )
        return jsonify({"ok": True, "ignored": "honeypot"}), 200

    if not name or not email:
        logger.warning(
            "GHL webhook missing fields after extraction: name=%r email=%r ref=%r",
            name, email, ref_code,
        )
        return jsonify({
            "error": "name and email are required",
            "extracted": {"name": name, "email": email, "ref": ref_code},
        }), 400

    # Email validation — same checks as /join, applied here so GHL-driven
    # signups can't bypass the disposable/MX defences. We log + reject;
    # GHL will retry on 4xx, so use 400 for permanent errors.
    from app.services.email_validation import (
        is_disposable_email, is_valid_email_syntax, has_mx_record,
        looks_like_bot_email,
    )
    email_lower = email.lower().strip()
    if not is_valid_email_syntax(email_lower):
        logger.warning("GHL webhook rejected: bad syntax email=%r", email)
        return jsonify({"error": "invalid_email_syntax", "email": email}), 400
    if is_disposable_email(email_lower):
        logger.warning("GHL webhook rejected: disposable email=%r", email)
        return jsonify({"error": "disposable_email", "email": email}), 400
    if looks_like_bot_email(email_lower):
        logger.warning("GHL webhook rejected: bot-pattern email=%r", email)
        return jsonify({"error": "bot_pattern_email", "email": email}), 400
    if not has_mx_record(email_lower):
        logger.warning("GHL webhook rejected: no MX record email=%r", email)
        return jsonify({"error": "domain_no_mx", "email": email}), 400

    # Cloudflare Turnstile verification. Log-only by default — only blocks
    # when TURNSTILE_ENFORCE=1, and even then only on missing/invalid tokens.
    # error/not_configured fail open so a CF outage doesn't kill signups.
    turnstile_token = extract_turnstile_token(payload)
    ts_result = verify_turnstile(turnstile_token, remote_ip=client_ip or None)
    logger.info(
        "turnstile webhook verify: status=%s codes=%s email=%s",
        ts_result["status"], ts_result["codes"], email,
    )
    if turnstile_enforce_mode() and ts_result["status"] in (STATUS_INVALID, STATUS_MISSING):
        logger.warning(
            "turnstile rejected webhook signup: status=%s codes=%s email=%s",
            ts_result["status"], ts_result["codes"], email,
        )
        record_turnstile_rejection(
            status=ts_result["status"],
            codes=ts_result["codes"],
            email_attempted=email,
            name_attempted=name,
            ip=client_ip or None,
            user_agent=request.headers.get("User-Agent", "") or None,
            source="webhook",
        )
        return jsonify({
            "error": "turnstile_failed",
            "status": ts_result["status"],
        }), 400

    # Phone + country detection. The Lovable form already collects phone;
    # GHL needs a custom-data row mapping {{contact.phone}} into the
    # outbound webhook body. Failures here never block the signup.
    raw_phone = _extract_phone(payload)
    phone_e164 = None
    country_iso = None
    if raw_phone:
        try:
            from app.services.phone import parse as parse_phone
            parsed = parse_phone(raw_phone)
            if parsed:
                phone_e164 = parsed["e164"]
                country_iso = parsed["country_code"]
        except Exception:
            logger.exception("phone parsing failed for %r", raw_phone)

    # Attribution (UTMs + click ids) — GHL forwards them as custom data when
    # the outbound webhook workflow is configured. Failures here never block
    # signup; the field stays NULL until backfilled by /api/lead-event.
    attribution = _extract_attribution(payload)

    try:
        ambassador, was_new = create_signup(
            name, email, ref_code,
            signup_ip=client_ip or None,
            turnstile_status=ts_result["status"],
            turnstile_codes=ts_result["codes"],
            phone_number=phone_e164,
            country_code=country_iso,
            attribution=attribution,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("signup webhook failed")
        return jsonify({"error": "internal"}), 500

    return jsonify({
        "ok": True,
        "was_new": was_new,
        "referral_code": ambassador.referral_code,
        "dashboard_url": f"{current_app.config['APP_URL']}/dashboard/{ambassador.dashboard_code}",
    }), 200


# ════════════════════════════════════════════════════════════════════
# RESEND WEBHOOK — email lifecycle events (open, click, bounce, etc.)
# ════════════════════════════════════════════════════════════════════
#
# Configure in Resend dashboard:
#   1. Webhooks → Add → URL: https://<your-app>/api/webhook/resend
#   2. Events to select: email.sent, email.delivered, email.opened,
#                        email.clicked, email.bounced, email.complained
#   3. Copy the signing secret → set as RESEND_WEBHOOK_SECRET env var.
#
# We match incoming events to our 'sent' rows via Resend's email id (which
# we stored at send time). The original 'sent' row carries the template_key
# and ambassador_id, so per-template open/click rates can be computed by
# joining sent rows ↔ event rows on resend_email_id.
#
# Supported event names emitted by Resend (subject to API version):
#   email.sent / email.delivered / email.opened / email.clicked /
#   email.bounced / email.complained / email.delivery_delayed

_RESEND_TYPE_MAP = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.opened": "opened",
    "email.clicked": "clicked",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.delivery_delayed": "delayed",
}


@webhook_bp.route("/api/webhook/resend", methods=["POST"])
def resend_webhook():
    """Persist Resend lifecycle events as EmailEvent rows.

    Lookup logic:
      - Webhook payload contains email_id → match the original 'sent' row
        (via resend_email_id) to copy its template_key + ambassador_id.
      - If we can't match (e.g. an email sent before tracking was wired),
        we still record the event with template_key='unknown'.
    """
    payload = request.get_json(silent=True) or {}
    event_type_raw = payload.get("type") or ""
    event_type = _RESEND_TYPE_MAP.get(event_type_raw, event_type_raw)

    data = payload.get("data") or {}
    resend_email_id = data.get("email_id") or data.get("id")
    to_field = data.get("to") or []
    if isinstance(to_field, list) and to_field:
        to_email = to_field[0]
    elif isinstance(to_field, str):
        to_email = to_field
    else:
        to_email = ""

    # Look up the original 'sent' row to copy template_key + ambassador_id.
    template_key = "unknown"
    ambassador_id = None
    if resend_email_id:
        sent = (
            EmailEvent.query
            .filter_by(resend_email_id=resend_email_id, event_type="sent")
            .first()
        )
        if sent is not None:
            template_key = sent.template_key
            ambassador_id = sent.ambassador_id

    # 'sent' itself we already wrote at send time; ignore the duplicate from
    # Resend (their event would race the synchronous insert anyway).
    if event_type == "sent":
        return jsonify({"ok": True, "ignored": "duplicate_sent"}), 200

    try:
        evt = EmailEvent(
            ambassador_id=ambassador_id,
            template_key=template_key,
            event_type=event_type,
            resend_email_id=resend_email_id,
            to_email=to_email,
            extra=json.dumps(payload)[:5000] if payload else None,
        )
        db.session.add(evt)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to persist resend webhook event")
        return jsonify({"error": "persist_failed"}), 500

    return jsonify({"ok": True, "stored": event_type}), 200


# ════════════════════════════════════════════════════════════════════
# LOVABLE LEAD-EVENT WEBHOOK — class views, video progress, downloads
# ════════════════════════════════════════════════════════════════════
#
# Receives behavioural events posted by the Lovable class pages
# (src/lib/webhooks.ts → fireClassEvent). Cross-origin: Lovable browsers
# call this from a different domain, so we add permissive CORS headers
# and answer OPTIONS preflight.
#
# Payload shape (Lovable):
#   {
#     "email": "user@example.com",
#     "event": "class1_viewed" | "class1_progress_25" | "class1_completed" | ...
#     "timestamp": "2026-05-04T...Z",
#     "page_url": "https://.../class1",
#     "class_number": 1,
#     "percent": 25,                  # progress events
#     "watched_seconds": 180,         # progress events
#     "duration_seconds": 720,        # progress events
#     "utm_source": "...", ...        # attribution
#     "ref": "ABC123"                 # referrer code
#   }
#
# We accept anonymous POSTs (no auth) because:
#   - Lovable runs in the user's browser, no shared secret possible
#   - Worst-case spam is mitigated by event-type validation + per-row
#     storage limits; we never act on these events except to store them
#
# Future hardening: tighten Access-Control-Allow-Origin to the Lovable
# domain(s) once known and stable.

# Whitelisted event_types so a malicious caller can't fill the table with
# arbitrary garbage. New event names must be added here.
_ALLOWED_LEAD_EVENTS = {
    # Class video events
    "class1_viewed", "class2_viewed", "class3_viewed",
    "class1_progress_25", "class1_progress_50", "class1_progress_75", "class1_progress_95",
    "class2_progress_25", "class2_progress_50", "class2_progress_75", "class2_progress_95",
    "class3_progress_25", "class3_progress_50", "class3_progress_75", "class3_progress_95",
    "class1_completed", "class2_completed", "class3_completed",
    "class1_resource_unlocked", "class2_resource_unlocked", "class3_resource_unlocked",
    "class1_resource_downloaded", "class2_resource_downloaded", "class3_resource_downloaded",
    # Add-to-calendar events
    "class_calendar_open", "class_calendar_added",
    # Future: webinar / purchase events
    "webinar_link_clicked", "webinar_joined", "webinar_left",
    "purchase_started", "purchase_completed",
}


def _add_cors_headers(response):
    """Permissive CORS for Lovable browser callers. Tighten to specific
    origins once domains are stable."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


@webhook_bp.route("/api/lead-event", methods=["POST", "OPTIONS"])
def lead_event():
    """Persist a Lovable class-page event as a LeadEvent row.

    Looks up the Ambassador by email (case-insensitive). If found, links
    the event to that ambassador AND backfills any UTM fields that are
    still NULL on the ambassador (first-touch attribution).
    """
    if request.method == "OPTIONS":
        return _add_cors_headers(make_response("", 204))

    payload = request.get_json(silent=True) or {}

    email = (payload.get("email") or "").strip().lower()
    event_type = (payload.get("event") or "").strip()

    if not email or not event_type:
        return _add_cors_headers(jsonify({"error": "email and event are required"})), 400

    if event_type not in _ALLOWED_LEAD_EVENTS:
        logger.warning("lead-event rejected unknown event=%r email=%r", event_type, email)
        return _add_cors_headers(jsonify({"error": "unknown_event_type", "event": event_type})), 400

    amb = Ambassador.query.filter(func.lower(Ambassador.email) == email).first()

    # Coerce numeric fields defensively (Lovable sends them as numbers, but
    # in case any future caller sends strings we don't want to 500).
    def _to_int(val):
        try:
            return int(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    pct = _to_int(payload.get("percent"))
    current_time_sec = _to_int(payload.get("watched_seconds"))
    duration_sec = _to_int(payload.get("duration_seconds"))
    class_number = _to_int(payload.get("class_number"))

    # Truncate URLs / strings to fit column widths.
    def _trim(val, n):
        if val is None:
            return None
        s = str(val)
        return s[:n] if s else None

    evt = LeadEvent(
        ambassador_id=amb.id if amb else None,
        email=email[:200],
        event_type=event_type[:60],
        pct=pct,
        current_time_sec=current_time_sec,
        duration_sec=duration_sec,
        class_number=class_number,
        page_url=_trim(payload.get("page_url"), 500),
        utm_source=_trim(payload.get("utm_source"), 100),
        utm_medium=_trim(payload.get("utm_medium"), 100),
        utm_campaign=_trim(payload.get("utm_campaign"), 100),
        utm_content=_trim(payload.get("utm_content"), 200),
        utm_term=_trim(payload.get("utm_term"), 100),
        ref=_trim(payload.get("ref") or payload.get("referral_code"), 50),
        fbclid=_trim(payload.get("fbclid"), 200),
        gclid=_trim(payload.get("gclid"), 200),
        ttclid=_trim(payload.get("ttclid"), 200),
        extra=json.dumps(payload)[:5000] if payload else None,
    )

    try:
        db.session.add(evt)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to persist lead event email=%s event=%s", email, event_type)
        return _add_cors_headers(jsonify({"error": "persist_failed"})), 500

    # First-touch attribution backfill: if the ambassador exists but has no
    # UTMs stored yet, copy the ones from this event. Only fills NULL fields,
    # never overwrites existing values.
    if amb is not None:
        changed = False
        for fld in ("utm_source", "utm_medium", "utm_campaign", "utm_content",
                    "utm_term", "fbclid", "gclid", "ttclid"):
            current = getattr(amb, fld, None)
            incoming = getattr(evt, fld, None)
            if not current and incoming:
                setattr(amb, fld, incoming)
                changed = True
        if changed:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("failed to backfill attribution on ambassador %d", amb.id)

    # ── Push milestone tags to GHL so workflows there can move the contact
    # to the right pipeline stage (class started / completed / webinar joined
    # / purchased). Fire-and-forget in a background thread so a slow GHL API
    # call never blocks the Lovable frontend.
    if amb is not None and amb.ghl_contact_id:
        from app.services.ghl import LEAD_EVENT_TAG_MAP, add_tags
        tag = LEAD_EVENT_TAG_MAP.get(event_type)
        if tag:
            import threading
            contact_id = amb.ghl_contact_id

            def _push_tag(cid=contact_id, t=tag, ev=event_type, em=email):
                try:
                    add_tags(cid, [t])
                except Exception:
                    logger.exception("ghl tag push failed event=%s email=%s tag=%s", ev, em, t)
            threading.Thread(target=_push_tag, daemon=True).start()

    return _add_cors_headers(jsonify({
        "ok": True,
        "stored": event_type,
        "linked_ambassador": amb is not None,
    }))
