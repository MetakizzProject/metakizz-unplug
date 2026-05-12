import csv
import io
import logging
import os
import threading
from collections import defaultdict
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, session, current_app, Response,
)
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification, EmailEvent, PendingReferral, PrizeDelivery, LeadEvent, Reservation, RaffleState, PartnerInvite, CirclePayment, SavedAudience, EmailDraft
from app.services.temperature import bucket_from_event_set
from app.mailer import (
    build_refund_confirmation_html,
    build_no_phone_outreach_html,
    send_invoice_email,
    send_refund_confirmation_email,
    send_no_phone_outreach_email,
    send_welcome_email,
    send_activation_nudge_email,
    send_activation_push_email,
    send_first_unplug_email,
    send_guaranteed_prize_email,
    send_midway_reminder_email,
    send_final_48h_email,
    send_last_6h_email,
    send_results_announcement_email,
    send_you_won_email,
    send_class1_ready_email,
    send_class2_ready_email,
    send_class3_ready_email,
    send_webinar_reminder_email,
    send_masterclass_invitation_email,
    send_carrots_landing_email,
    send_final_signal_email,
    send_live_imminent_email,
    send_class1_rewatch_reminder_email,
    send_class2_rewatch_reminder_email,
    send_class3_rewatch_reminder_email,
    send_reservation_first50_email,
    send_custom_html_email,
    render_custom_html_preview,
    _send as _mailer_send,  # low-level Resend POST, used by /admin/broadcast
    # legacy:
    send_first_referral_email,
    send_referral_notification_email,
    send_milestone_email,
    send_almost_there_email,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger(__name__)


def _generate_and_send_invoice(circle_payment, force=False):
    """Generate the invoice PDF for a CirclePayment and email it to the
    customer. Idempotent — returns False (without re-sending) if already
    sent unless force=True.

    Returns True on success, False otherwise. On success, stamps
    invoice_id (correlative) and invoice_sent_at on the row.
    """
    from app.services.invoice_pdf import generate_invoice_pdf
    from app.services.invoice_numbering import assign_invoice_number

    if not circle_payment or not circle_payment.email:
        return False
    if circle_payment.invoice_sent_at and not force:
        return False

    invoice_number = assign_invoice_number(circle_payment, commit=True)

    line_description = circle_payment.description or "Digital services — MetaKizz Project"
    amount = circle_payment.amount_cents or 0

    try:
        pdf_bytes = generate_invoice_pdf(
            invoice_number=invoice_number,
            customer_email=circle_payment.email,
            customer_name=circle_payment.customer_name,
            line_items=[{
                "description": line_description,
                "qty": 1,
                "unit_price_cents": amount,
            }],
            currency=(circle_payment.currency or "usd").upper(),
            stripe_charge_id=circle_payment.stripe_charge_id,
            issue_date=circle_payment.paid_at or datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("invoice PDF generation failed for CirclePayment %s", circle_payment.id)
        return False

    try:
        sent = bool(send_invoice_email(circle_payment, invoice_number, pdf_bytes))
    except Exception:
        logger.exception("invoice email send failed for CirclePayment %s", circle_payment.id)
        return False

    if sent:
        circle_payment.invoice_sent_at = datetime.now(timezone.utc)
        # Persist the exact bytes we sent so future re-downloads return the
        # same document (immutability — the customer-received version).
        circle_payment.invoice_pdf_bytes = pdf_bytes
        db.session.commit()
        logger.info(
            "invoice sent: cp=%s invoice_number=%s email=%s pdf_bytes=%d",
            circle_payment.id, invoice_number, circle_payment.email, len(pdf_bytes),
        )
        return True

    logger.warning("invoice email returned False for CirclePayment %s", circle_payment.id)
    return False


def _send_refund_email_and_stamp(reservation):
    """Send the refund confirmation email and stamp refund_email_sent_at
    on the reservation row. Idempotent: if already sent, skip. Returns
    True if a new email was sent, False otherwise.
    """
    if not reservation or not reservation.email:
        return False
    if reservation.refund_email_sent_at:
        return False
    try:
        ok = bool(send_refund_confirmation_email(reservation))
    except Exception:
        logger.exception("send_refund_confirmation_email failed for reservation %s", reservation.id)
        return False
    if ok:
        reservation.refund_email_sent_at = datetime.now(timezone.utc)
        db.session.commit()
        return True
    return False


def _reservation_has_phone(reservation):
    """True if we have a usable phone number for this buyer (via the
    matched Ambassador). Used to decide which buyers get the
    "no-phone outreach" email.
    """
    if not reservation:
        return False
    amb = getattr(reservation, "ambassador", None)
    if amb is None:
        return False
    phone = (getattr(amb, "phone_number", None) or "").strip()
    return bool(phone)


def _send_no_phone_email_and_stamp(reservation):
    """Send the "tried-to-reach-you-on-WhatsApp" email and stamp
    no_phone_email_sent_at on the reservation. Idempotent: skips if
    already sent. Returns True if a fresh email was sent.
    """
    if not reservation or not reservation.email:
        return False
    if reservation.no_phone_email_sent_at:
        return False
    try:
        ok = bool(send_no_phone_outreach_email(reservation))
    except Exception:
        logger.exception("send_no_phone_outreach_email failed for reservation %s", reservation.id)
        return False
    if ok:
        reservation.no_phone_email_sent_at = datetime.now(timezone.utc)
        db.session.commit()
        return True
    return False


def _is_current_edition(circle_payment):
    """True if a CirclePayment belongs to the current MKOT edition.

    Primary filter is by **payment date**: anything paid before
    MKOT3_START_AT (ISO 8601 in env, default 2026-04-01) is treated as
    a past-edition payment and excluded. This is the most reliable
    filter because product names from old editions are unpredictable.

    Optional secondary filters by description (mostly for legacy):
      - MKOT_EDITION_KEYWORDS (whitelist): if set, ALSO require
        description match. Strict mode.
      - MKOT_EDITION_EXCLUDE (blacklist): exclude descriptions matching
        these keywords (in addition to the date filter).
    """
    if not circle_payment:
        return False

    # Primary: date filter.
    cutoff_iso = os.getenv("MKOT3_START_AT", "2026-05-01T00:00:00+00:00")
    try:
        cutoff = datetime.fromisoformat(cutoff_iso)
    except Exception:
        cutoff = datetime(2026, 5, 1, tzinfo=timezone.utc)
    paid_at = circle_payment.paid_at
    if paid_at is None:
        return False  # no date = can't trust it, exclude
    # Make timezone-aware for comparison.
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    if paid_at < cutoff:
        return False

    desc = (circle_payment.description or "").lower()

    # Secondary: optional description blacklist.
    exclude_raw = os.getenv("MKOT_EDITION_EXCLUDE", "").strip()
    if exclude_raw:
        excludes = [kw.strip().lower() for kw in exclude_raw.split(",") if kw.strip()]
        if desc and any(kw in desc for kw in excludes):
            return False

    # Optional: description whitelist (strict mode).
    whitelist_raw = os.getenv("MKOT_EDITION_KEYWORDS", "").strip()
    if whitelist_raw:
        keywords = [kw.strip().lower() for kw in whitelist_raw.split(",") if kw.strip()]
        if not desc:
            return False
        return any(kw in desc for kw in keywords)

    return True


def _safe(fn, default, *args, **kwargs):
    """Wrap a heavy helper call so a single failure doesn't 500 the page.
    Logs the exception and returns `default`. Used inside Overview /
    Leads / Insights routes around _compute_* helpers.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("safe wrapper caught exception in %s", getattr(fn, "__name__", fn))
        return default


# ════════════════════════════════════════════════════════════════════
# Marketing helpers — segments + chart data
# ════════════════════════════════════════════════════════════════════

# Funnel events used by the SQL-aggregation classification helpers.
# Derived from the canonical class-event taxonomy in temperature.py so
# adding a hypothetical class 4 later means updating one place. Includes
# class 3 = the live-replay (Bunny upload, tracked identically to 1/2).
def _build_funnel_event_keys():
    from app.services.temperature import (
        class_started_event_types, class_completed_event_types,
        class_visited_event_types,
    )
    keys = ["purchase_completed", "webinar_joined"]
    for cn in (1, 2, 3):
        keys += class_started_event_types(cn)
        keys += class_completed_event_types(cn)
        keys += class_visited_event_types(cn)
    # Dedup but preserve list semantics
    return sorted(set(keys))

_FUNNEL_EVENT_KEYS = _build_funnel_event_keys()


# Bucket classifier moved to app.services.temperature so that the temp
# filter, distribution counters, and per-row badge in compute_temperature()
# all agree. Local alias kept for callers below.
_email_to_bucket = bucket_from_event_set


def _build_email_buckets() -> dict:
    """Return {email_lower: bucket_key} for every lead with any signal.

    Single source of truth for temperature classification across:
      - /admin/leads ?temp= filter (_emails_in_temp_bucket)
      - /admin/leads top distribution counters (_quick_temp_dist_sql)
      - /admin/leads default sort by score (sort=temp)

    Combines three data sources:
      1. LeadEvent rows with email set (Lovable-tracked class views)
      2. LeadEvent rows linked only by ambassador_id (Zoom guests rematched
         by name) → joins Ambassador to attribute events to the correct email
      3. Reservation rows with paid_at IS NOT NULL → promotes bucket to ≥burning

    Empty dict on error so callers degrade gracefully.
    """
    from app.models import LeadEvent, Reservation
    try:
        email_rows = (
            db.session.query(LeadEvent.email, LeadEvent.event_type)
            .filter(LeadEvent.event_type.in_(_FUNNEL_EVENT_KEYS))
            .filter(LeadEvent.email.isnot(None))
            .distinct().all()
        )
        amb_rows = (
            db.session.query(Ambassador.email, LeadEvent.event_type)
            .join(LeadEvent, LeadEvent.ambassador_id == Ambassador.id)
            .filter(LeadEvent.event_type.in_(_FUNNEL_EVENT_KEYS))
            .filter(or_(LeadEvent.email.is_(None), LeadEvent.email == ""))
            .filter(Ambassador.email.isnot(None))
            .distinct().all()
        )
        paid_emails = {
            (em or "").lower() for (em,) in
            db.session.query(Reservation.email)
            .filter(Reservation.paid_at.isnot(None))
            .all() if em
        }
    except Exception:
        logger.exception("_build_email_buckets query failed")
        return {}

    by_email = defaultdict(set)
    for em, et in email_rows:
        if em:
            by_email[em.lower()].add(et)
    for em, et in amb_rows:
        if em:
            by_email[em.lower()].add(et)
    for em in paid_emails:
        if em not in by_email:
            by_email[em] = set()

    return {
        em: bucket_from_event_set(evts, has_paid_reservation=(em in paid_emails))
        for em, evts in by_email.items()
    }


def _emails_in_temp_bucket(bucket: str) -> set:
    """Subset of _build_email_buckets() — emails matching the given bucket."""
    return {em for em, b in _build_email_buckets().items() if b == bucket}


def _quick_temp_dist_sql():
    """Temperature distribution via the unified _build_email_buckets()
    helper. Returns dict {bucket_key: count}.
    """
    buckets = _build_email_buckets()
    total_reachable = (
        Ambassador.query.filter(Ambassador.unsubscribed_at.is_(None)).count()
    )
    counts = {"customer": 0, "burning": 0, "hot": 0, "warm": 0, "cool": 0, "cold": 0}
    for bucket in buckets.values():
        if bucket in counts:
            counts[bucket] += 1
    cold = max(0, total_reachable - sum(counts.values()))
    counts["cold"] = cold
    return counts


def _quick_origin_dist_sql():
    """Approximate origin distribution via SQL on Ambassador UTM columns.

    Returns dict {bucket_key: count}.
    """
    from app.services.temperature import SOURCE_BUCKETS
    counts = {key: 0 for key, _ in SOURCE_BUCKETS}

    # Pull distinct utm_source counts (and fbclid/gclid presence)
    rows = (
        db.session.query(Ambassador.utm_source, Ambassador.utm_medium,
                         Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid,
                         func.count(Ambassador.id))
        .group_by(Ambassador.utm_source, Ambassador.utm_medium,
                  Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid)
        .all()
    )

    def _to_bucket(src, med, fbclid, gclid, ttclid):
        s = (src or "").lower()
        m = (med or "").lower()
        is_paid = any(k in m for k in ("cpc", "paid", "ads", "ad ")) or m in ("ad", "paid")
        if "tiktok" in s or ttclid:
            return "tiktok_ad" if is_paid else "tiktok"
        if "google" in s or gclid:
            return "google_ad" if (is_paid or gclid) else "google"
        if "instagram" in s or "insta" in s or s == "ig":
            return "instagram_ad" if is_paid else "instagram"
        if "facebook" in s or "fb" in s or "meta" in s or fbclid:
            return "facebook_ad" if (is_paid or fbclid) else "facebook"
        if "referral" in s or "referral" in m:
            return "referral"
        if "email" in s or m == "email":
            return "email"
        if s or m:
            return "other"
        return "direct"

    for src, med, fb, gc, tt, n in rows:
        bucket = _to_bucket(src, med, fb, gc, tt)
        counts[bucket] = counts.get(bucket, 0) + n

    return counts


_FUNNEL_STAGES_FOR_BAR = [
    ("Registered",        "#2EDB99", None),
    ("Started Class 1",   "#2EDB99", "class1_min25"),
    ("Finished Class 1",  "#FFC857", "class1_min95"),
    ("Started Class 2",   "#FFC857", "class2_min25"),
    ("Finished Class 2",  "#F97316", "class2_min95"),
    ("Joined Live",       "#DC2626", "webinar"),
    ("Started Class 3",   "#DC2626", "class3_min25"),
    ("Finished Class 3",  "#A78BFA", "class3_min95"),
    ("Purchased",         "#A78BFA", "customer"),
]


def _compute_launch_funnel(total_leads):
    """Build the launch-funnel data shape used by /admin/leads and
    /admin/leads/insights. Returns:

        {
          "steps": [{label, count, color, pct_of_total, dropoff_pct, key}, ...],
          "visited": {1: int, 2: int}  # page-loaders only (didn't engage)
        }

    Uses the canonical `class_started_event_types` and
    `class_completed_event_types` from temperature.py so this matches
    every other counter on the site (PLF totals on /admin/leads,
    insights funnel, etc.).

    Single SQL query for all funnel events; counts via in-memory union
    of distinct emails per group. ~30k events / ~2500 leads runs in
    well under a second.
    """
    from app.services.temperature import (
        class_started_event_types, class_completed_event_types,
        class_visited_event_types,
    )

    # Pull every event type we care about in ONE query. Class 3 is the
    # live-replay (Bunny Stream upload after the Zoom session) and now
    # part of the conversion path.
    funnel_event_keys = set()
    for cn in (1, 2, 3):
        funnel_event_keys.update(class_started_event_types(cn))
        funnel_event_keys.update(class_completed_event_types(cn))
        funnel_event_keys.update(class_visited_event_types(cn))
    funnel_event_keys.update(["webinar_joined", "purchase_completed"])

    rows = _safe(
        lambda: db.session.query(LeadEvent.email, LeadEvent.event_type)
            .filter(LeadEvent.event_type.in_(list(funnel_event_keys)))
            .distinct().all(),
        [],
    )
    by_event = defaultdict(set)
    for em, et in rows:
        if em:
            by_event[et].add(em.lower())

    def _union(event_types):
        out = set()
        for et in event_types:
            out |= by_event.get(et, set())
        return out

    started_1 = _union(class_started_event_types(1))
    started_2 = _union(class_started_event_types(2))
    started_3 = _union(class_started_event_types(3))
    finished_1 = _union(class_completed_event_types(1))
    finished_2 = _union(class_completed_event_types(2))
    finished_3 = _union(class_completed_event_types(3))
    visited_1 = _union(class_visited_event_types(1)) - started_1
    visited_2 = _union(class_visited_event_types(2)) - started_2
    visited_3 = _union(class_visited_event_types(3)) - started_3

    counts = {
        "registered":   total_leads,
        "class1_min25": len(started_1),
        "class1_min95": len(finished_1),
        "class2_min25": len(started_2),
        "class2_min95": len(finished_2),
        "class3_min25": len(started_3),
        "class3_min95": len(finished_3),
        "webinar":      len(by_event.get("webinar_joined", set())),
        "customer":     len(by_event.get("purchase_completed", set())),
    }

    funnel_steps = []
    for i, (label, color, key) in enumerate(_FUNNEL_STAGES_FOR_BAR):
        count = total_leads if i == 0 else counts.get(key, 0)
        funnel_steps.append({"label": label, "count": count, "color": color, "key": key})

    for i, step in enumerate(funnel_steps):
        step["pct_of_total"] = round(100 * step["count"] / total_leads, 1) if total_leads else 0
        if i == 0:
            step["dropoff_pct"] = 0
        else:
            prev = funnel_steps[i - 1]["count"]
            step["dropoff_pct"] = round(100 * (prev - step["count"]) / prev, 1) if prev else 0

    return {
        "steps": funnel_steps,
        "visited": {1: len(visited_1), 2: len(visited_2), 3: len(visited_3)},
    }


def _compute_7d_activity():
    """Return per-day counts for the last 7 days as a Chart.js-ready dict.

    {
      "labels": ["Apr 29", ..., "May 5"],
      "signups":  [2, 3, 0, ...],
      "class1":   [0, 1, 4, ...],
      "class2":   [0, 0, 0, ...],
    }

    Two cheap SQL aggregates keep this under ~50ms.
    """
    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    day_keys = [d.isoformat() for d in days]
    labels = [d.strftime("%b %d") for d in days]

    cutoff = datetime.combine(days[0], datetime.min.time(), tzinfo=timezone.utc)

    # Signups per day
    signup_by_day = {k: 0 for k in day_keys}
    sig_rows = _safe(
        lambda: db.session.query(
            func.date(Ambassador.created_at), func.count(Ambassador.id)
        ).filter(Ambassador.created_at >= cutoff)
         .group_by(func.date(Ambassador.created_at)).all(),
        [],
    )
    for d, n in sig_rows:
        if d is None:
            continue
        # SQLite returns str, Postgres returns date
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        if key in signup_by_day:
            signup_by_day[key] = n

    # Class viewers per day (distinct emails, viewer-or-better events)
    class_by_day = {1: {k: 0 for k in day_keys}, 2: {k: 0 for k in day_keys}}
    for cn in (1, 2):
        keys = [f"class{cn}_viewed", f"class{cn}_progress_25",
                f"class{cn}_progress_50", f"class{cn}_progress_75",
                f"class{cn}_progress_95", f"class{cn}_completed"]
        rows = _safe(
            lambda keys=keys: db.session.query(
                func.date(LeadEvent.created_at),
                func.count(func.distinct(LeadEvent.email))
            ).filter(
                LeadEvent.event_type.in_(keys),
                LeadEvent.created_at >= cutoff,
            ).group_by(func.date(LeadEvent.created_at)).all(),
            [],
        )
        for d, n in rows:
            if d is None:
                continue
            key = d.isoformat() if hasattr(d, "isoformat") else str(d)
            if key in class_by_day[cn]:
                class_by_day[cn][key] = n

    return {
        "labels": labels,
        "signups": [signup_by_day[k] for k in day_keys],
        "class1":  [class_by_day[1][k] for k in day_keys],
        "class2":  [class_by_day[2][k] for k in day_keys],
    }


def _compute_ghost_summary():
    """Aggregate ghost LeadEvents (ambassador_id IS NULL) into per-email rows.

    Ghosts are people who watched a class video but whose email doesn't
    match any Ambassador record. We surface them so the admin can reach
    out / convert them.

    Single SQL pass against LeadEvent. Returns a list of dicts:

        {
          "email":          str,
          "first_seen":     datetime,
          "last_seen":      datetime,
          "event_count":    int,
          "class1_max":     int (0..100),
          "class2_max":     int (0..100),
          "class3_max":     int (0..100),
          "webinar_joined": bool,
          "event_types":    set[str],
          "bucket_key":     "cold"|"cool"|"warm"|"hot"|"burning"|"customer",
          "utm_source":     str|None,
          "utm_medium":     str|None,
          "utm_campaign":   str|None,
          "ref":            str|None,
          "fbclid":         str|None,
          "gclid":          str|None,
          "page_url":       str|None,
        }
    """
    rows = _safe(
        lambda: db.session.query(
            LeadEvent.email,
            LeadEvent.event_type,
            LeadEvent.pct,
            LeadEvent.class_number,
            LeadEvent.created_at,
            LeadEvent.utm_source,
            LeadEvent.utm_medium,
            LeadEvent.utm_campaign,
            LeadEvent.ref,
            LeadEvent.fbclid,
            LeadEvent.gclid,
            LeadEvent.page_url,
        ).filter(LeadEvent.ambassador_id.is_(None))
         .filter(LeadEvent.email.isnot(None))
         .order_by(LeadEvent.created_at.asc())
         .all(),
        [],
    )

    def _pct_from_event(event_type, pct_field):
        if pct_field is not None:
            try:
                return int(pct_field)
            except (TypeError, ValueError):
                pass
        et = event_type or ""
        if et.endswith("_completed"):
            return 100
        if et.endswith("_resource_unlocked"):
            return 95
        if et.endswith("_progress_95"):
            return 95
        if et.endswith("_progress_75"):
            return 75
        if et.endswith("_progress_50"):
            return 50
        if et.endswith("_progress_25"):
            return 25
        return 0

    by_email = {}
    for em, et, pct, cn, ts, utm_s, utm_m, utm_c, ref, fb, gc, page_url in rows:
        if not em:
            continue
        em_lc = em.lower()
        s = by_email.setdefault(em_lc, {
            "email": em_lc,
            "first_seen": ts,
            "last_seen": ts,
            "event_count": 0,
            "class1_max": 0,
            "class2_max": 0,
            "class3_max": 0,
            "event_types": set(),
            "utm_source": None,
            "utm_medium": None,
            "utm_campaign": None,
            "ref": None,
            "fbclid": None,
            "gclid": None,
            "page_url": None,
        })
        s["event_count"] += 1
        s["event_types"].add(et)
        if ts and (s["first_seen"] is None or ts < s["first_seen"]):
            s["first_seen"] = ts
        if ts and (s["last_seen"] is None or ts > s["last_seen"]):
            s["last_seen"] = ts
        # class_number column may be NULL on legacy rows — fall back to
        # parsing the event_type prefix (class1_/class2_/class3_).
        cn_eff = cn
        if cn_eff not in (1, 2, 3):
            ev = et or ""
            if ev.startswith("class") and len(ev) >= 6:
                try:
                    cn_eff = int(ev[5])
                except ValueError:
                    cn_eff = None
        if cn_eff in (1, 2, 3):
            p = _pct_from_event(et, pct)
            key = f"class{cn_eff}_max"
            if p > s[key]:
                s[key] = p
        # Latest non-null UTM/ref/page_url wins (events ordered ASC by created_at)
        if utm_s: s["utm_source"] = utm_s
        if utm_m: s["utm_medium"] = utm_m
        if utm_c: s["utm_campaign"] = utm_c
        if ref:   s["ref"] = ref
        if fb:    s["fbclid"] = fb
        if gc:    s["gclid"] = gc
        if page_url: s["page_url"] = page_url

    # Per-ghost bucket from event-set classifier (same as Ambassador rows).
    # Pass has_paid_reservation so a ghost who paid via Stripe (e.g. shared
    # link, paid, never registered) is correctly promoted to burning. One
    # SQL pass for all ghost emails — cheap, idempotent.
    from app.services.temperature import bucket_from_event_set
    from app.models import Reservation
    paid_ghost_emails = set()
    if by_email:
        ghost_email_list = list(by_email.keys())
        try:
            paid_rows = (
                db.session.query(Reservation.email)
                .filter(func.lower(Reservation.email).in_(ghost_email_list))
                .filter(Reservation.paid_at.isnot(None))
                .all()
            )
            paid_ghost_emails = {(r.email or "").lower() for r in paid_rows if r.email}
        except Exception:
            logger.exception("paid_ghost_emails lookup failed")
    for em_key, s in by_email.items():
        s["bucket_key"] = bucket_from_event_set(
            s["event_types"],
            has_paid_reservation=(em_key in paid_ghost_emails),
        )
        s["webinar_joined"] = "webinar_joined" in s["event_types"]

    return list(by_email.values())


def _compute_referral_network():
    """Build the full referral DAG for /admin/network.

    Returns a dict:
        {
          "nodes":     [{id, name, email, source, descendants, direct_count,
                         depth_in_tree, root_id}, ...],
          "links":     [{source: amb_id, target: amb_id, registered: bool}, ...],
          "top_viral": [<top 10 nodes by descendants desc>],
          "stats":     {total_trees, deepest_chain, biggest_tree_size,
                        conversion_rate_pct, total_referrals, total_orphans}
        }

    Performance: 2 SQL queries (ambassadors + referrals), all graph
    traversal in memory. ~50ms for 2,500 ambassadors / 1,000 referrals.

    Orphan referrals (referred email never registered as Ambassador) are
    excluded from `nodes` / `links` for the graph — they'd just be
    leaf nodes with no further info. They ARE counted in stats
    (`total_orphans`, `conversion_rate_pct`).
    """
    # 1. Pull ambassadors
    ambs = (
        db.session.query(
            Ambassador.id, Ambassador.name, Ambassador.email,
            Ambassador.source,
        ).all()
    )
    amb_by_id = {a.id: {
        "id": a.id,
        "name": a.name or "—",
        "email": a.email or "",
        "source": a.source or "",
        "direct_count": 0,
        "descendants": 0,
        "depth_in_tree": 0,  # filled later
        "root_id": a.id,     # filled later
    } for a in ambs}

    email_to_amb_id = {(a.email or "").lower(): a.id for a in ambs if a.email}

    # 2. Pull referrals
    referrals = (
        db.session.query(
            Referral.ambassador_id, Referral.email, Referral.name,
        ).all()
    )

    # 3. Build child-parent map. Only edges where target = a known ambassador
    # (referred person who actually registered). Orphan referrals are tracked
    # separately for stats.
    children_of = defaultdict(list)  # parent_id -> [child_id, ...]
    parent_of = {}                   # child_id  -> parent_id
    orphan_count = 0

    for ref_amb_id, ref_email, _ref_name in referrals:
        if ref_amb_id is None or ref_amb_id not in amb_by_id:
            continue
        amb_by_id[ref_amb_id]["direct_count"] += 1
        target_email = (ref_email or "").lower()
        target_id = email_to_amb_id.get(target_email)
        if target_id and target_id != ref_amb_id:
            # Only add edge if not already there (a referrer might have multiple
            # Referral rows for the same email — dedupe by parent-child pair).
            if target_id not in parent_of:
                parent_of[target_id] = ref_amb_id
                children_of[ref_amb_id].append(target_id)
        else:
            orphan_count += 1

    # 4. Compute descendants count per node (bottom-up via topological order).
    # Roots = nodes with no parent. Walk down from each root.
    roots = [aid for aid in amb_by_id if aid not in parent_of]

    def _walk(node_id, depth, root_id):
        amb_by_id[node_id]["depth_in_tree"] = depth
        amb_by_id[node_id]["root_id"] = root_id
        descendants = 0
        for child_id in children_of.get(node_id, []):
            descendants += 1 + _walk(child_id, depth + 1, root_id)
        amb_by_id[node_id]["descendants"] = descendants
        return descendants

    deepest_chain = 0
    biggest_tree = 0
    trees_with_branches = 0
    for root_id in roots:
        size = _walk(root_id, 0, root_id)
        if size > 0:
            trees_with_branches += 1
        if size > biggest_tree:
            biggest_tree = size
        # Track deepest chain (max depth in this tree)
        # We can pick from the children's depth recursively, but simpler:
        # walk again to find the max depth in this subtree.

    # Compute deepest chain across ALL nodes
    for node in amb_by_id.values():
        if node["depth_in_tree"] > deepest_chain:
            deepest_chain = node["depth_in_tree"]

    # 5. Build links list (parent -> child)
    links = []
    for child_id, parent_id in parent_of.items():
        links.append({
            "source": parent_id,
            "target": child_id,
            "registered": True,
        })

    # 6. Top viral: only nodes that have at least one descendant
    top_viral = sorted(
        [n for n in amb_by_id.values() if n["descendants"] > 0],
        key=lambda n: -n["descendants"],
    )[:10]

    total_referrals = len(referrals)
    total_registered_referrals = total_referrals - orphan_count
    conversion_rate_pct = (
        round(100.0 * total_registered_referrals / total_referrals, 1)
        if total_referrals else 0
    )

    biggest_root = max(top_viral, key=lambda n: n["descendants"]) if top_viral else None

    stats = {
        "total_trees": trees_with_branches,
        "deepest_chain": deepest_chain,
        "biggest_tree_size": biggest_tree,
        "biggest_tree_root": biggest_root["name"] if biggest_root else "—",
        "conversion_rate_pct": conversion_rate_pct,
        "total_referrals": total_referrals,
        "total_orphans": orphan_count,
        "total_ambassadors": len(amb_by_id),
    }

    return {
        "nodes": list(amb_by_id.values()),
        "links": links,
        "top_viral": top_viral,
        "stats": stats,
    }


def _get_referral_counts():
    """Map ambassador_id → referral count via a single SQL aggregation.

    Replaces lazy access to amb.referral_count in bulk loops, which was
    triggering N+1 queries (each .referral_count call → SELECT FROM
    referrals). With ~2500 ambassadors that meant ~2500 extra queries
    per page and 75s+ timeouts in production.

    Single query, ~10ms regardless of size.
    Callers should look up via `ref_counts.get(amb.id, 0)`.
    """
    rows = (
        db.session.query(Referral.ambassador_id, func.count(Referral.id))
        .group_by(Referral.ambassador_id)
        .all()
    )
    return dict(rows)


def _compute_segments(ambassadors, referral_counts=None):
    """Group reachable ambassadors into marketing-relevant buckets.

    Only includes opted-in (unsubscribed_at IS NULL) ambassadors. Each
    segment is a list of Ambassador instances.

    `referral_counts` (optional) is a dict {ambassador_id: count} from
    `_get_referral_counts()`. When provided, we read counts from the
    dict instead of triggering the lazy `.referral_count` property
    (which fires one SQL per row). Callers in bulk-loop contexts SHOULD
    pass it; falls back to the property for the legacy callers.
    """
    now = datetime.now(timezone.utc)
    reachable = [a for a in ambassadors if a.unsubscribed_at is None]

    def _count(a):
        if referral_counts is not None:
            return referral_counts.get(a.id, 0)
        return a.referral_count

    def days_since_last_referral(amb):
        # When referral_counts dict is provided, we know amb has 0 refs
        # without touching the relationship. Skip the .referrals access
        # if count is 0 — it would just be an empty list anyway and
        # accessing it triggers a query when not joinedloaded.
        if _count(amb) > 0 and amb.referrals:
            last = max(r.registered_at for r in amb.referrals)
            # SQLite returns naive datetimes; coerce to UTC for math
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (now - last).days
        created = amb.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).days

    cold = [a for a in reachable if _count(a) == 0]
    sleeping = [a for a in reachable if 1 <= _count(a) < 5]
    needs_activation = [a for a in reachable if _count(a) < 5]  # cold ∪ sleeping
    champions = [a for a in reachable if _count(a) >= 5]
    # "Public winners" = champions restricted to the public-source challenge.
    # These are the people who actually completed the Hacking the Urbankiz
    # Code unplug challenge and unlocked the musicality masterclass — used
    # by the Carrots & onions launch email so it doesn't blast the whole
    # ambassador list (community + public + non-winners alike).
    public_winners = [a for a in champions if a.source == "public"]
    top10 = sorted(reachable, key=lambda a: -_count(a))[:10]
    inactive_7d = [a for a in reachable if days_since_last_referral(a) >= 7]
    never_visited = [a for a in reachable if a.last_dashboard_visit_at is None]

    # Class rewatch sleepers: started watching class N during the launch
    # window but haven't returned during the weekend re-open window. The
    # cutoff comes from REWATCH_WINDOW_OPENS_AT — anything strictly before
    # the cutoff is "first view"; anything at or after is "rewatch".
    sleepers_per_class = {1: [], 2: [], 3: []}
    # Per-class cutoff: class 3 may have a different rewatch window than 1/2.
    cutoffs_per_class = {n: _rewatch_cutoff(n) for n in (1, 2, 3)}
    if any(cutoffs_per_class.values()):
        # One scan over class engagement events; categorize each (email, class)
        # pair into {viewed_before, viewed_after}. Sleeper = viewed_before
        # and not viewed_after — relative to that class's own cutoff.
        class_event_types = [
            "class1_viewed", "class2_viewed", "class3_viewed",
            "class1_completed", "class2_completed", "class3_completed",
        ]
        events = (
            db.session.query(LeadEvent.email, LeadEvent.event_type, LeadEvent.created_at)
            .filter(LeadEvent.event_type.in_(class_event_types))
            .filter(LeadEvent.email.isnot(None))
            .all()
        )
        by_pair = {}  # {(email_lower, class_n): {"before": bool, "after": bool}}
        for em, ev_type, ts in events:
            em_norm = (em or "").lower()
            if not em_norm:
                continue
            try:
                n = int(ev_type[5])
            except (IndexError, ValueError):
                continue
            if n not in (1, 2, 3):
                continue
            cls_cutoff = cutoffs_per_class.get(n)
            if cls_cutoff is None:
                continue
            ts_aware = ts if (ts and ts.tzinfo) else (ts.replace(tzinfo=timezone.utc) if ts else None)
            if ts_aware is None:
                continue
            d = by_pair.setdefault((em_norm, n), {"before": False, "after": False})
            if ts_aware < cls_cutoff:
                d["before"] = True
            else:
                d["after"] = True
        for amb in reachable:
            em_norm = (amb.email or "").lower()
            if not em_norm:
                continue
            for n in (1, 2, 3):
                d = by_pair.get((em_norm, n))
                if d and d["before"] and not d["after"]:
                    sleepers_per_class[n].append(amb)

    return {
        "cold": cold,                          # 0 unplugs (need a kick)
        "sleeping": sleeping,                  # 1-4 unplugs (need momentum)
        "needs_activation": needs_activation,  # 0-4 unplugs (haven't unlocked yet)
        "champions": champions,                # 5+ unplugs (lock the prize)
        "public_winners": public_winners,      # 5+ unplugs · public source only
        "top10": top10,                        # current top performers
        "inactive_7d": inactive_7d,            # no activity in 7 days
        "never_visited": never_visited,        # never opened their dashboard
        "sleepers_class1": sleepers_per_class[1],  # class1 viewed pre-weekend, no return
        "sleepers_class2": sleepers_per_class[2],
        "sleepers_class3": sleepers_per_class[3],
    }


def _rewatch_cutoff(class_n=None):
    """Parse the rewatch cutoff from config into a timezone-aware datetime.

    When `class_n` is 1/2/3, prefers REWATCH_WINDOW_OPENS_AT_CLASS{N}; falls
    back to the global REWATCH_WINDOW_OPENS_AT. Returns None if neither is
    parseable. Per-class overrides let class 3 (the live-replay) use a
    different cutoff than classes 1/2 if needed.
    """
    if not current_app:
        return None
    iso = None
    if class_n in (1, 2, 3):
        iso = current_app.config.get(f"REWATCH_WINDOW_OPENS_AT_CLASS{class_n}")
    if not iso:
        iso = current_app.config.get("REWATCH_WINDOW_OPENS_AT")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _compute_suspicion(ambassador):
    """Heuristic fraud check based on referral IP / UA clusters.

    Returns dict with:
      level:  'clean' | 'watch' | 'high'
      score:  0..100 (only meaningful when level != 'clean')
      reason: short human-readable explanation
      max_ip_count, max_ua_count, total: raw stats for debugging

    Logic (intentionally simple — admin reviews manually):
      - Need at least 2 referrals with IP data to make any call.
      - If 70%+ of referrals come from the SAME IP and total >= 3 → HIGH.
      - Otherwise if 50%+ same IP and total >= 5 → WATCH.
      - Otherwise if 70%+ share user agent and total >= 5 → WATCH.
    """
    refs = ambassador.referrals
    n = len(refs)
    if n < 2:
        return {"level": "clean", "score": 0, "reason": None, "total": n}

    ip_counts = {}
    ua_counts = {}
    refs_with_ip = 0
    for r in refs:
        if r.signup_ip:
            ip_counts[r.signup_ip] = ip_counts.get(r.signup_ip, 0) + 1
            refs_with_ip += 1
        if r.signup_user_agent:
            ua_counts[r.signup_user_agent] = ua_counts.get(r.signup_user_agent, 0) + 1

    # No IP data captured (e.g. all referrals are from before tracking was wired).
    if refs_with_ip == 0:
        return {"level": "clean", "score": 0, "reason": None, "total": n}

    max_ip_count = max(ip_counts.values()) if ip_counts else 0
    max_ua_count = max(ua_counts.values()) if ua_counts else 0
    ip_share = max_ip_count / n
    ua_share = max_ua_count / n if max_ua_count else 0

    # HIGH: ≥70% same IP and at least 3 referrals
    if n >= 3 and ip_share >= 0.7:
        return {
            "level": "high",
            "score": int(ip_share * 100),
            "reason": f"{max_ip_count}/{n} from same IP",
            "total": n, "max_ip_count": max_ip_count, "max_ua_count": max_ua_count,
        }
    # WATCH: ≥50% same IP and at least 5 referrals
    if n >= 5 and ip_share >= 0.5:
        return {
            "level": "watch",
            "score": int(ip_share * 100),
            "reason": f"{max_ip_count}/{n} from same IP",
            "total": n, "max_ip_count": max_ip_count, "max_ua_count": max_ua_count,
        }
    # WATCH (UA only): ≥70% same UA and at least 5 referrals
    if n >= 5 and ua_share >= 0.7:
        return {
            "level": "watch",
            "score": int(ua_share * 100),
            "reason": f"{max_ua_count}/{n} share user agent",
            "total": n, "max_ip_count": max_ip_count, "max_ua_count": max_ua_count,
        }
    return {"level": "clean", "score": 0, "reason": None, "total": n,
            "max_ip_count": max_ip_count, "max_ua_count": max_ua_count}


def _compute_email_stats():
    """Per-template aggregate stats from EmailEvent rows.

    For each template_key:
      sent     — count of 'sent' events
      opened   — count of distinct emails that got at least one 'opened' event
      clicked  — count of distinct emails with at least one 'clicked' event
      bounced  — count with at least one 'bounced'

    Open/click rates are computed against sent (delivered would be slightly
    more accurate but Resend reports both, and we want to show the simpler
    funnel).
    """
    # All sent rows grouped by template
    rows = (
        db.session.query(EmailEvent.template_key, EmailEvent.event_type, EmailEvent.resend_email_id)
        .filter(EmailEvent.template_key != "unknown")
        .all()
    )

    stats = {}
    seen_per_template = defaultdict(lambda: {"sent": set(), "opened": set(), "clicked": set(), "bounced": set()})

    for tpl, evt, rid in rows:
        if rid is None:
            continue
        bucket = seen_per_template[tpl]
        if evt in bucket:
            bucket[evt].add(rid)

    for tpl, sets in seen_per_template.items():
        sent = len(sets["sent"])
        opened = len(sets["opened"])
        clicked = len(sets["clicked"])
        bounced = len(sets["bounced"])
        stats[tpl] = {
            "sent": sent,
            "opened": opened,
            "clicked": clicked,
            "bounced": bounced,
            "open_rate": (round(100 * opened / sent, 1) if sent else 0),
            "click_rate": (round(100 * clicked / sent, 1) if sent else 0),
        }
    return stats


# ════════════════════════════════════════════════════════════════════
# Email Control Center — comprehensive email-system data
# ════════════════════════════════════════════════════════════════════

# Source-of-truth metadata for every template the system can send.
# Used to populate the Email Control Center; trigger + fires fields are
# human-readable strings the admin UI shows verbatim.
EMAIL_TEMPLATES_META = [
    ("welcome",              "Welcome",            "On every new signup",                              "real-time"),
    ("first_unplug",         "First Unplug",       "Referrer's count goes 0 → 1",                      "real-time"),
    ("guaranteed_prize",     "Guaranteed Prize",   "Referrer hits 5 unplugs (count 4 → 5)",            "real-time"),
    ("activation_nudge",     "Activation Nudge",   "Cron · count=0 and 48h+ since signup",             "cron daily"),
    ("activation_push",      "Activation Push",    "Admin manual · personalized 'X away' to count 0-4", "admin manual"),
    ("midway_reminder",      "Midway Reminder",    "Cron · 7d+ old and ≥5d to close",                  "cron daily"),
    ("final_48h",            "Final 48h",          "Cron one-shot · 2026-05-05 19:00 Madrid",          "cron one-shot"),
    ("last_6h",              "Last 6h",            "Cron one-shot · 2026-05-07 13:00 Madrid",          "cron one-shot"),
    ("results_announcement", "Results",            "Cron one-shot · 2026-05-08 10:00 Madrid",          "cron one-shot"),
    ("you_won",              "You Won",            "Cron one-shot · 2026-05-08 10:30 Madrid",          "cron one-shot"),
    ("broadcast",            "Broadcast",          "Admin manual via /admin (broadcast modal)",        "admin"),
]


def _compute_email_lifecycle():
    """Build a per-template email lifecycle dataset for the control center.

    For every template in EMAIL_TEMPLATES_META, returns:
      sent / opened / clicked / bounced  — distinct recipients per event
      open_rate / click_rate / bounce_rate — percentages over sent
      last_sent_at — most recent 'sent' EmailEvent timestamp
      health — 'good' | 'warn' | 'critical' (heuristic from rates)
    """
    rows = (
        db.session.query(
            EmailEvent.template_key,
            EmailEvent.event_type,
            EmailEvent.resend_email_id,
            EmailEvent.created_at,
        )
        .filter(EmailEvent.template_key != "unknown")
        .all()
    )

    raw = defaultdict(lambda: {
        "sent": set(), "opened": set(), "clicked": set(), "bounced": set(),
        "delivered": set(), "complained": set(),
        "last_sent_at": None,
    })
    for tpl, evt, rid, ts in rows:
        b = raw[tpl]
        if evt in b and rid is not None:
            b[evt].add(rid)
        if evt == "sent" and ts is not None:
            if b["last_sent_at"] is None or ts > b["last_sent_at"]:
                b["last_sent_at"] = ts

    out = []
    for key, label, trigger, fires in EMAIL_TEMPLATES_META:
        b = raw.get(key, {"sent": set(), "opened": set(), "clicked": set(),
                          "bounced": set(), "delivered": set(), "complained": set(),
                          "last_sent_at": None})
        sent = len(b["sent"])
        opened = len(b["opened"])
        clicked = len(b["clicked"])
        bounced = len(b["bounced"])
        delivered = len(b["delivered"])
        complained = len(b["complained"])
        open_rate = round(100 * opened / sent, 1) if sent else 0
        click_rate = round(100 * clicked / sent, 1) if sent else 0
        bounce_rate = round(100 * bounced / sent, 1) if sent else 0

        # Health heuristic: red on bounce >3% OR complained >0 OR (sent>50 and opens=0)
        if sent == 0:
            health = "idle"
        elif bounce_rate > 3 or complained > 0:
            health = "critical"
        elif sent > 50 and opened == 0:
            health = "critical"
        elif bounce_rate > 1 or open_rate < 15:
            health = "warn"
        else:
            health = "good"

        out.append({
            "key": key, "label": label, "trigger": trigger, "fires": fires,
            "sent": sent, "opened": opened, "clicked": clicked,
            "bounced": bounced, "delivered": delivered, "complained": complained,
            "open_rate": open_rate, "click_rate": click_rate, "bounce_rate": bounce_rate,
            "last_sent_at": b["last_sent_at"],
            "health": health,
        })
    return out


def _compute_email_health_summary():
    """Top-level health metrics shown in the page header strip."""
    now = datetime.now(timezone.utc)

    # Most recent webhook event of any kind — proxy for "Resend is talking to us"
    latest_evt = (
        EmailEvent.query
        .filter(EmailEvent.event_type != "sent")
        .order_by(EmailEvent.created_at.desc())
        .first()
    )
    latest_send = (
        EmailEvent.query
        .filter(EmailEvent.event_type == "sent")
        .order_by(EmailEvent.created_at.desc())
        .first()
    )

    last_webhook_at = latest_evt.created_at if latest_evt else None
    last_send_at = latest_send.created_at if latest_send else None

    # Total counts (last 24h vs all-time)
    cutoff = now - timedelta(hours=24)
    sent_24h = (
        EmailEvent.query
        .filter(EmailEvent.event_type == "sent")
        .filter(EmailEvent.created_at >= cutoff)
        .count()
    )
    sent_total = EmailEvent.query.filter(EmailEvent.event_type == "sent").count()
    bounced_24h = (
        EmailEvent.query
        .filter(EmailEvent.event_type == "bounced")
        .filter(EmailEvent.created_at >= cutoff)
        .count()
    )
    complained_total = EmailEvent.query.filter(EmailEvent.event_type == "complained").count()

    bounce_rate_24h = round(100 * bounced_24h / sent_24h, 2) if sent_24h else 0

    # Webhook age in hours (None if never)
    webhook_age_h = None
    if last_webhook_at is not None:
        delta = now - (last_webhook_at if last_webhook_at.tzinfo else last_webhook_at.replace(tzinfo=timezone.utc))
        webhook_age_h = round(delta.total_seconds() / 3600, 1)

    # Webhook health classification
    if webhook_age_h is None:
        webhook_status = "never"   # never received a webhook
    elif webhook_age_h > 24:
        webhook_status = "stale"   # >1 day silent
    elif webhook_age_h > 6:
        webhook_status = "warn"
    else:
        webhook_status = "good"

    return {
        "sent_24h": sent_24h,
        "sent_total": sent_total,
        "bounced_24h": bounced_24h,
        "complained_total": complained_total,
        "bounce_rate_24h": bounce_rate_24h,
        "last_webhook_at": last_webhook_at,
        "last_send_at": last_send_at,
        "webhook_age_h": webhook_age_h,
        "webhook_status": webhook_status,
        "unsubscribed_count": Ambassador.query.filter(Ambassador.unsubscribed_at.isnot(None)).count(),
    }


def _compute_turnstile_stats():
    """Aggregate Cloudflare Turnstile verification results across signups.

    Returns counts in two windows (24h and all-time) plus the enforce-mode
    flag, so the admin panel can show whether log-only or enforcement is
    active.
    """
    from app.services.turnstile import is_enforce_mode
    from app.models import TurnstileRejection

    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    def _bucket(rows):
        out = {"valid": 0, "invalid": 0, "missing": 0, "error": 0,
               "not_configured": 0, "legacy": 0}
        for status, in rows:
            if status is None:
                out["legacy"] += 1
            elif status in out:
                out[status] += 1
            else:
                # Unknown status string (forward-compat): bucket as legacy
                out["legacy"] += 1
        return out

    all_rows = db.session.query(Ambassador.turnstile_status).all()
    last24_rows = db.session.query(Ambassador.turnstile_status).filter(
        Ambassador.created_at >= cutoff_24h
    ).all()

    # Attacks blocked — counts of TurnstileRejection rows (only populated
    # while enforce-mode is on; in log-only the route doesn't reject).
    blocked_24h = TurnstileRejection.query.filter(
        TurnstileRejection.created_at >= cutoff_24h
    ).count()
    blocked_all = TurnstileRejection.query.count()
    recent_blocks = (
        TurnstileRejection.query
        .order_by(TurnstileRejection.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "all": _bucket(all_rows),
        "last24h": _bucket(last24_rows),
        "all_total": len(all_rows),
        "last24h_total": len(last24_rows),
        "enforce_mode": is_enforce_mode(),
        "blocked_24h": blocked_24h,
        "blocked_all": blocked_all,
        "recent_blocks": recent_blocks,
    }


def _compute_country_distribution(limit=40):
    """Aggregate ambassador counts by ISO country code.

    Returns:
      - labels / counts / flags  → bar chart (top `limit`, rest in 'Other')
      - geo  → {numeric_iso: {name, flag, count, alpha2}} for the world map
      - other_breakdown  → list of (label, count, flag) for what's lumped in 'Other'
      - coverage_pct, total, with_country, distinct_countries
    """
    from app.services.phone import lookup_country, iso_to_numeric

    rows = (
        db.session.query(Ambassador.country_code, func.count(Ambassador.id))
        .group_by(Ambassador.country_code)
        .all()
    )
    total = sum(c for _, c in rows)
    with_country = sum(c for code, c in rows if code)

    counts = [(code, c) for code, c in rows if code]
    counts.sort(key=lambda x: -x[1])

    top = counts[:limit]
    overflow = counts[limit:]
    other_count = sum(c for _, c in overflow)

    labels = []
    counts_list = []  # NOTE: not 'values' — Jinja shadows dict.values method
    flags = []
    for code, c in top:
        name, flag = lookup_country(code)
        labels.append(f"{flag} {name}".strip() or code)
        counts_list.append(c)
        flags.append(flag)
    if other_count:
        labels.append("Other")
        counts_list.append(other_count)
        flags.append("")

    # Detail of what's in "Other" so the user can see the long tail
    other_breakdown = []
    for code, c in overflow:
        name, flag = lookup_country(code)
        other_breakdown.append({
            "label": f"{flag} {name}".strip() or code,
            "count": c,
            "code": code,
            "flag": flag,
        })

    # Geo data for the choropleth — keyed by ISO numeric WITHOUT leading
    # zeros to match world-atlas TopoJSON ids ("8" not "008"). The
    # iso_to_numeric helper returns the zero-padded form for canonical
    # display elsewhere; here we strip via int() round-trip.
    geo = {}
    for code, c in counts:
        numeric = iso_to_numeric(code)
        if numeric:
            name, flag = lookup_country(code)
            geo[str(int(numeric))] = {
                "name": name,
                "flag": flag,
                "count": c,
                "alpha2": code,
            }

    return {
        "labels": labels,
        "counts": counts_list,
        "flags": flags,
        "geo": geo,
        "other_breakdown": other_breakdown,
        "total": total,
        "with_country": with_country,
        "coverage_pct": (round(100 * with_country / total, 1) if total else 0),
        "distinct_countries": len(counts),
        "max_count": max((c for _, c in counts), default=0),
    }


def _compute_chart_data(ambassadors=None, referral_counts=None):
    """Return JSON-serialisable data for the admin charts.

    PERF: callers in bulk-loop contexts (e.g. admin.index) should pass
    `ambassadors` (already-fetched list) and `referral_counts` (dict from
    _get_referral_counts) so we don't re-query and don't trigger N+1
    on the lazy .referral_count property. Falls back to internal
    queries when args are omitted.
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    # ── Signups timeline (last 14 days, split by source) ──
    # This intentionally re-queries with a date filter — it's much
    # smaller than the full table and we only need created_at + source.
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    day_keys = [d.isoformat() for d in days]
    counts_by_day = defaultdict(lambda: {"community": 0, "public": 0})
    cutoff = datetime.combine(days[0], datetime.min.time(), tzinfo=timezone.utc)
    for amb in Ambassador.query.filter(Ambassador.created_at >= cutoff).all():
        # Skip rows that would break the bucket dispatch: missing created_at
        # or a `source` outside the known {community, public} set (e.g. legacy
        # imports, GHL-synced rows with empty source). One stray value used to
        # KeyError out of the loop and `_safe` would zero ALL charts.
        if amb.created_at is None or amb.source not in ("community", "public"):
            continue
        d = amb.created_at.date().isoformat()
        counts_by_day[d][amb.source] += 1

    timeline = {
        "labels": [d.strftime("%b %d") for d in days],
        "community": [counts_by_day[k]["community"] for k in day_keys],
        "public": [counts_by_day[k]["public"] for k in day_keys],
    }

    # ── Activity distribution + funnel — both need referral_count per row ──
    if ambassadors is None:
        ambassadors = Ambassador.query.all()
    if referral_counts is None:
        referral_counts = _get_referral_counts()

    def _count(a):
        return referral_counts.get(a.id, 0)

    buckets = {"0": 0, "1-2": 0, "3-4": 0, "5-9": 0, "10+": 0}
    for amb in ambassadors:
        c = _count(amb)
        if c == 0:
            buckets["0"] += 1
        elif c <= 2:
            buckets["1-2"] += 1
        elif c <= 4:
            buckets["3-4"] += 1
        elif c <= 9:
            buckets["5-9"] += 1
        else:
            buckets["10+"] += 1

    distribution = {
        "labels": list(buckets.keys()),
        "values": list(buckets.values()),
    }

    # ── Funnel ──
    total = len(ambassadors)
    welcomed = sum(1 for a in ambassadors if a.welcome_sent_at is not None)
    first_unplug = sum(1 for a in ambassadors if _count(a) >= 1)
    five_plus = sum(1 for a in ambassadors if _count(a) >= 5)

    funnel = {
        "labels": ["Registered", "Welcomed", "1+ unplug", "5+ (locked)"],
        "values": [total, welcomed, first_unplug, five_plus],
    }

    return {
        "timeline": timeline,
        "distribution": distribution,
        "funnel": funnel,
    }


@admin_bp.route("/partner-invites")
def partner_invites():
    """Read-only list of MKOT 3.0 Couple-plan partner invites.

    Newest first, capped at 500 to keep the page snappy. Use the search bar
    on the page (client-side filter) for older rows; if we ever cross 500
    invites we'll add server-side filtering.
    """
    invites = (
        PartnerInvite.query
        .order_by(PartnerInvite.created_at.desc())
        .limit(500)
        .all()
    )

    total = PartnerInvite.query.count()
    failed_count = PartnerInvite.query.filter_by(circle_status="failed").count()
    followup_count = PartnerInvite.query.filter_by(needs_followup=True).count()

    return render_template(
        "admin_partner_invites.html",
        invites=invites,
        total=total,
        failed_count=failed_count,
        followup_count=followup_count,
        active_section="partner_invites",
        **_admin_layout_context(),
    )


@admin_bp.before_request
def require_admin():
    if request.endpoint == "admin.login":
        return
    if not session.get("is_admin"):
        return redirect(url_for("admin.login"))


def _admin_layout_context():
    """Common context dict for the sidebar layout. Computes countdown,
    pending-review badge, and which routes exist (so the sidebar can
    render placeholders gracefully when a section hasn't shipped yet).
    """
    ctx = {
        # Routes whose nav-link is enabled in the sidebar. Missing keys
        # render as href="#" (admin_base.html fallback).
        "admin_routes": [
            "overview", "live", "queue", "emails", "class_views",
            "security", "reach", "leads", "leads_insights",
            "ghosts", "network", "reservations", "raffle",
            "partner_invites", "invoices",
        ],
        "pending_review_count": PendingReferral.query.filter_by(status="pending").count(),
    }
    # Campaign close countdown — short label like "T-7D" or "6H".
    close_iso = current_app.config.get("CAMPAIGN_CLOSE_ISO", "")
    if close_iso:
        try:
            close_dt = datetime.fromisoformat(close_iso)
            now = datetime.now(close_dt.tzinfo)
            delta = close_dt - now
            secs = delta.total_seconds()
            if secs <= 0:
                ctx["countdown_short"] = "CLOSED"
            elif secs < 3600:
                ctx["countdown_short"] = f"{int(secs // 60)}M"
            elif secs < 86400:
                ctx["countdown_short"] = f"{int(secs // 3600)}H"
            else:
                ctx["countdown_short"] = f"T-{int(secs // 86400)}D"
        except Exception:
            ctx["countdown_short"] = None
    return ctx


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == current_app.config["ADMIN_PASSWORD"]:
            session["is_admin"] = True
            return redirect(url_for("admin.index"))
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@admin_bp.route("/security")
def security():
    """Security & anti-fraud center: Turnstile stats, attacks blocked,
    pending review queue summary, and high-risk ambassadors. Supports
    ?email=xxx to drill into all rejections for a single email — the
    investigation view used when a recurring email shows up in attacks.
    """
    from app.models import TurnstileRejection
    from collections import Counter

    email_filter = (request.args.get("email") or "").strip().lower() or None

    turnstile_stats = _compute_turnstile_stats()

    # ── Top emails by rejection count — surfaces patterns at a glance ──
    top_email_rows = (
        db.session.query(
            TurnstileRejection.email_attempted,
            func.count(TurnstileRejection.id).label("cnt"),
            func.max(TurnstileRejection.created_at).label("last_at"),
            func.count(func.distinct(TurnstileRejection.ip)).label("distinct_ips"),
        )
        .filter(TurnstileRejection.email_attempted.isnot(None))
        .group_by(TurnstileRejection.email_attempted)
        .order_by(func.count(TurnstileRejection.id).desc())
        .limit(15)
        .all()
    )

    # ── Email investigation drill-in ──
    investigation = None
    if email_filter:
        rows = (
            TurnstileRejection.query
            .filter(func.lower(TurnstileRejection.email_attempted) == email_filter)
            .order_by(TurnstileRejection.created_at.desc())
            .all()
        )
        # Aggregates
        ip_counter = Counter(r.ip or "—" for r in rows)
        ua_counter = Counter((r.user_agent or "—")[:200] for r in rows)
        source_counter = Counter(r.source or "—" for r in rows)
        status_counter = Counter(r.status or "—" for r in rows)

        # Decide whether the email belongs to an existing Ambassador
        existing_amb = (
            Ambassador.query
            .filter(func.lower(Ambassador.email) == email_filter)
            .first()
        )

        investigation = {
            "email": email_filter,
            "rows": rows,
            "total": len(rows),
            "distinct_ips": len(set(r.ip for r in rows if r.ip)),
            "distinct_uas": len(set(r.user_agent for r in rows if r.user_agent)),
            "first_at": min((r.created_at for r in rows if r.created_at), default=None),
            "last_at": max((r.created_at for r in rows if r.created_at), default=None),
            "ip_top": ip_counter.most_common(10),
            "ua_top": ua_counter.most_common(5),
            "source_top": source_counter.most_common(),
            "status_top": status_counter.most_common(),
            "existing_ambassador": existing_amb,
        }

    # High-risk ambassadors — sorted by suspicion score, top 30
    # PERF: eager-load referrals so _compute_suspicion doesn't N+1.
    all_amb = Ambassador.query.options(joinedload(Ambassador.referrals)).all()
    risk_rows = []
    for a in all_amb:
        risk = _compute_suspicion(a)
        if risk["level"] in ("high", "watch"):
            risk_rows.append({"amb": a, "risk": risk})
    risk_rows.sort(key=lambda r: -r["risk"]["score"])
    risk_rows = risk_rows[:30]

    # Recent pending review preview (last 10)
    recent_pending = (
        PendingReferral.query
        .filter_by(status="pending")
        .order_by(PendingReferral.received_at.desc())
        .limit(10)
        .all()
    )

    return render_template(
        "admin_security.html",
        page_title="Security",
        active_section="security",
        turnstile_stats=turnstile_stats,
        risk_rows=risk_rows,
        recent_pending=recent_pending,
        top_email_rows=top_email_rows,
        investigation=investigation,
        **_admin_layout_context(),
    )


@admin_bp.route("/reach")
def reach():
    """Worldwide reach: the illuminated world map + country distribution
    bar chart + phone backfill access. Moved out of the main Overview to
    declutter the admin home.
    """
    country_dist = _compute_country_distribution()
    return render_template(
        "admin_reach.html",
        page_title="Reach",
        active_section="reach",
        country_dist=country_dist,
        **_admin_layout_context(),
    )


# 61 ambassadors who received the activation_push email accidentally during a
# local smoke test on 2026-05-03 ~18:08 UTC. Hardcoded so the admin can
# one-click pre-flag them in PROD before the official mass send, avoiding
# duplicate emails. Auto-marked by /admin/emails/auto-mark-leaked.
LEAKED_ACTIVATION_PUSH_EMAILS = [
    "cameleonek@iinet.net.au",         "myriam.robert98@gmail.com",
    "rita.pant@icloud.com",            "pavlo.sherin@tanecvplzni.cz",
    "jana.kucerova@tanecvplzni.cz",    "erik9.9@web.de",
    "linke.sandra97@web.de",           "neznoummm@gmail.com",
    "akathelopez92@gmail.com",         "djbachakizcr@gmail.com",
    "georgemappouras07@gmail.com",     "lnky0823@gmail.com",
    "maria.christou.isaac@gmail.com",  "vandenengel.thijs@gmail.com",
    "knoblochjk@gmail.com",            "lbenes24@gmail.com",
    "martravelinside@gmail.com",       "przemekstolarski@outlook.com",
    "leontear@gmail.com",              "wc_5306@yahoo.ca",
    "pennywalthall@yahoo.com",         "alexander.rogalla@rub.de",
    "eldar.manishevizch@gmail.com",    "carole.mbinky@yahoo.com",
    "marcofilipefm@gmail.com",         "sandracfraga@gmail.com",
    "oliver.reluga@gmail.com",         "m.plitz@myway.de",
    "mirelvi.rojas@gmail.com",         "mari.nysaether@gmail.com",
    "girisisodiya01@gmail.com",        "sydboss@gmail.com",
    "rob333204@gmail.com",             "sophie.lincoln@hotmail.com",
    "peps86@gmail.com",                "esperanza123@hotmail.com",
    "lukmala@proton.me",               "petardonev5@gmail.com",
    "endless.move.events@gmail.com",   "nixe83@gmail.com",
    "natberg1001@gmail.com",           "carolwilczynski@hotmail.com",
    "julia.a.k.dick@gmail.com",        "nathanlundgaard@gmail.com",
    "pouran1996@gmail.com",            "silvio.seddio@gmail.com",
    "sharon.bottana@gmail.com",        "amedmbow@gmail.com",
    "raniero.schmidli@hotmail.com",    "dabrad@gmail.com",
    "brlarumbe@gmail.com",             "840214166@qq.com",
    "vivianeli@qq.com",                "melespada03@gmail.com",
    "marinatango5678@gmail.com",       "sorin.chis06@gmail.com",
    "anthony.gilbert96@hotmail.fr",    "berenice.caillot@outlook.fr",
    "borzasijudit@gmail.com",          "radu.gavozdea@gmail.com",
    "ruuber@gmail.com",
]


@admin_bp.route("/emails/auto-mark-leaked", methods=["POST"])
def auto_mark_leaked():
    """One-click: mark the 61 known leaked recipients as already pushed.

    Sets activation_push_sent_at=NOW for every email in
    LEAKED_ACTIVATION_PUSH_EMAILS that exists in the DB. Idempotent —
    running twice is a no-op for already-flagged rows.
    """
    now = datetime.now(timezone.utc)
    matched = (
        Ambassador.query
        .filter(func.lower(Ambassador.email).in_(LEAKED_ACTIVATION_PUSH_EMAILS))
        .all()
    )
    flagged = 0
    already = 0
    for a in matched:
        if a.activation_push_sent_at is None:
            a.activation_push_sent_at = now
            flagged += 1
        else:
            already += 1
    db.session.commit()

    not_found = len(LEAKED_ACTIVATION_PUSH_EMAILS) - len(matched)
    flash(
        f"Auto-marked {flagged} leaked recipients as already pushed. "
        f"{already} were already flagged. {not_found} not found in DB. "
        f"They will be skipped on the next mass send.",
        "success",
    )
    logger.warning(
        "ADMIN AUTO-MARK-LEAKED: matched=%d flagged=%d already=%d notfound=%d",
        len(matched), flagged, already, not_found,
    )
    return redirect(url_for("admin.emails"))


@admin_bp.route("/emails/mark-already-pushed", methods=["POST"])
def mark_already_pushed():
    """One-shot helper: paste a newline-separated list of emails, this sets
    activation_push_sent_at on each so the main send skips them. Used to
    avoid duplicate sends after a leak / pre-test send.
    """
    raw = (request.form.get("emails", "") or "").strip()
    if not raw:
        flash("No emails provided.", "error")
        return redirect(url_for("admin.emails"))

    emails = [
        line.strip().lower()
        for line in raw.replace(",", "\n").splitlines()
        if line.strip() and "@" in line
    ]
    if not emails:
        flash("Could not parse any valid emails from the paste.", "error")
        return redirect(url_for("admin.emails"))

    now = datetime.now(timezone.utc)
    matched = (
        Ambassador.query
        .filter(func.lower(Ambassador.email).in_(emails))
        .all()
    )
    matched_lower = {a.email.lower() for a in matched}
    not_found = [e for e in emails if e not in matched_lower]

    flagged = 0
    already_flagged = 0
    for a in matched:
        if a.activation_push_sent_at is None:
            a.activation_push_sent_at = now
            flagged += 1
        else:
            already_flagged += 1
    db.session.commit()

    msg = (
        f"Marked {flagged} ambassadors as already pushed (will be skipped). "
        f"{already_flagged} were already flagged. "
        f"{len(not_found)} email(s) not found in DB."
    )
    if not_found:
        msg += f" Sample not-found: {', '.join(not_found[:5])}"
    flash(msg, "success" if flagged or already_flagged else "info")
    logger.warning(
        "ADMIN MARK-ALREADY-PUSHED: requested=%d matched=%d flagged=%d already=%d notfound=%d",
        len(emails), len(matched), flagged, already_flagged, len(not_found),
    )
    return redirect(url_for("admin.emails"))


@admin_bp.route("/emails")
def emails():
    """Email Control Center — central visibility for every email the
    system can send. Per-template lifecycle stats, recent activity feed,
    Resend webhook health, unsubscribe count, scheduled sends.

    Also boots the Email Hub: seeds the baseline saved audiences
    (`public_unpaid` etc.) on first page load so the admin sees them
    immediately without needing a migration script.
    """
    # Idempotent — safe to call every page load.
    try:
        _seed_default_audiences()
    except Exception:
        logger.exception("_seed_default_audiences failed (continuing)")

    lifecycle = _compute_email_lifecycle()
    summary = _compute_email_health_summary()

    # Compute the eligible audience for the activation_push button.
    # Mirrors the filter chain inside segment_send_template so the modal
    # count matches the actual send size:
    #   - not unsubscribed
    #   - not already pushed (idempotency flag)
    #   - referral_count < 5
    #   - registered ≥ min_age_days ago (skip today's signups)
    push_min_age = _SEGMENT_TEMPLATES["activation_push"].get("min_age_days", 0)
    push_cutoff = datetime.now(timezone.utc) - timedelta(days=push_min_age) if push_min_age else None
    push_eligible = (
        Ambassador.query
        .options(joinedload(Ambassador.referrals))  # PERF: avoid N+1 on referral_count
        .filter(Ambassador.unsubscribed_at.is_(None))
        .filter(Ambassador.activation_push_sent_at.is_(None))
        .all()
    )

    def _push_age_ok(a):
        if push_cutoff is None:
            return True
        c = a.created_at
        if c is None:
            return False
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
        return c <= push_cutoff

    skipped_too_new = sum(
        1 for a in push_eligible
        if a.referral_count < 5 and not _push_age_ok(a)
    )
    push_eligible = [a for a in push_eligible if a.referral_count < 5 and _push_age_ok(a)]
    push_eligible_count = len(push_eligible)
    push_eligible_community = sum(1 for a in push_eligible if a.source == "community")
    push_eligible_public = sum(1 for a in push_eligible if a.source == "public")
    # Per-count breakdown (0/1/2/3/4) so the founder sees who's at what stage
    push_eligible_by_count = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for a in push_eligible:
        c = a.referral_count
        if 0 <= c <= 4:
            push_eligible_by_count[c] += 1

    # Recent activity feed — last 50 events (any type) with ambassador linked
    recent_events = (
        EmailEvent.query
        .order_by(EmailEvent.created_at.desc())
        .limit(50)
        .all()
    )
    # Pre-resolve ambassador objects to avoid N+1 in the template
    amb_ids = {e.ambassador_id for e in recent_events if e.ambassador_id}
    amb_lookup = {}
    if amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(amb_ids)).all():
            amb_lookup[a.id] = a

    # Eligible counts for each manual content-drop email. "All" segment =
    # every reachable (opted-in) ambassador minus those whose flag is set
    # AND minus those whose `exclude_if_event_in` filter would skip them
    # at send time (e.g. already watched the class). Mirroring the actual
    # send filter at line ~1548 so the card matches what will fire.
    reachable_total = Ambassador.query.filter(Ambassador.unsubscribed_at.is_(None)).count()

    _MANUAL_TEMPLATES = [
        ("masterclass_invitation", "masterclass_invitation_sent_at"),
        ("carrots_landing",  "carrots_landing_sent_at"),
        ("class1_ready",     "class1_email_sent_at"),
        ("class2_ready",     "class2_email_sent_at"),
        ("class3_ready",     "class3_email_sent_at"),
        ("webinar_reminder", "webinar_reminder_sent_at"),
        ("final_signal",     "final_signal_sent_at"),
        ("live_imminent",    "live_imminent_sent_at"),
    ]

    # Single union query: every (email, event_type) pair that could trigger
    # an exclusion in any of the manual-send templates above.
    all_exclude_events = set()
    for key, _flag in _MANUAL_TEMPLATES:
        all_exclude_events.update(_SEGMENT_TEMPLATES[key].get("exclude_if_event_in") or [])

    engaged_pairs = []
    if all_exclude_events:
        # Path A: events with email captured (Lovable class views).
        engaged_pairs = list(
            db.session.query(LeadEvent.email, LeadEvent.event_type)
            .filter(LeadEvent.event_type.in_(all_exclude_events))
            .filter(LeadEvent.email.isnot(None))
            .distinct()
            .all()
        )
        # Path B: events linked only by ambassador_id (Zoom guest rematch
        # left email empty). Resolve to Ambassador.email via inner join so
        # the eligibility counts match the actual send-time exclusion.
        engaged_pairs += list(
            db.session.query(Ambassador.email, LeadEvent.event_type)
            .join(LeadEvent, LeadEvent.ambassador_id == Ambassador.id)
            .filter(LeadEvent.event_type.in_(all_exclude_events))
            .filter(or_(LeadEvent.email.is_(None), LeadEvent.email == ""))
            .filter(Ambassador.email.isnot(None))
            .distinct()
            .all()
        )

    # Lowercased emails of all reachable ambassadors — used to intersect with
    # engaged emails so the count is consistent with `reachable_total`.
    reachable_emails = {
        (e or "").lower() for (e,) in
        db.session.query(Ambassador.email)
        .filter(Ambassador.unsubscribed_at.is_(None))
        .all() if e
    }

    # Compute the "public winners" pool once — same set of people for every
    # template card (5+ unplugs, source=public, reachable). What changes per
    # template is the *_sent_at idempotency subtraction. Done outside the
    # loop so we don't re-query for each template.
    ref_counts_for_winners = _get_referral_counts()
    public_winners_pool = [
        a for a in (
            Ambassador.query
            .filter(Ambassador.unsubscribed_at.is_(None))
            .filter(Ambassador.source == "public")
            .all()
        )
        if ref_counts_for_winners.get(a.id, 0) >= 5
    ]
    public_winners_total = len(public_winners_pool)

    manual_email_eligibles = {}
    for key, flag in _MANUAL_TEMPLATES:
        already = Ambassador.query.filter(
            Ambassador.unsubscribed_at.is_(None),
            getattr(Ambassador, flag).isnot(None),
        ).count()
        excl = set(_SEGMENT_TEMPLATES[key].get("exclude_if_event_in") or [])
        engaged_emails = {
            (em or "").lower()
            for (em, ev) in engaged_pairs
            if ev in excl and em
        }
        already_engaged = len(engaged_emails & reachable_emails)
        # Public winners variant: same pool minus the ones who already
        # received THIS template, minus those excluded by event filter
        # (so the count matches what segment_send_template would fire).
        # Use a single set of skipped IDs so someone who is BOTH already-
        # sent AND already-engaged gets counted once, not twice.
        pw_skipped_ids = set()
        pw_already_sent = 0
        pw_already_engaged = 0
        for a in public_winners_pool:
            sent = getattr(a, flag, None) is not None
            engaged = bool(a.email) and a.email.lower() in engaged_emails
            if sent:
                pw_already_sent += 1
            if engaged:
                pw_already_engaged += 1
            if sent or engaged:
                pw_skipped_ids.add(a.id)
        pw_eligible = max(public_winners_total - len(pw_skipped_ids), 0)
        manual_email_eligibles[key] = {
            "eligible": max(reachable_total - already - already_engaged, 0),
            "already_sent": already,
            "already_engaged": already_engaged,
            "reachable_total": reachable_total,
            "label": _SEGMENT_TEMPLATES[key]["label"],
            "public_winners_total": public_winners_total,
            "public_winners_eligible": pw_eligible,
            "public_winners_already_sent": pw_already_sent,
            "public_winners_already_engaged": pw_already_engaged,
        }

    # Cron kill-switch status (DISABLE_CRON_EMAILS env var).
    import os as _os
    cron_emails_disabled = _os.getenv("DISABLE_CRON_EMAILS", "").strip().lower() in ("1", "true", "yes", "on")

    # Zoom API credentials presence — controls the "Pull via API" button state.
    from app.services import zoom as _zoom_svc
    zoom_credentials_present = _zoom_svc.credentials_present()

    return render_template(
        "admin_emails.html",
        page_title="Emails",
        active_section="emails",
        lifecycle=lifecycle,
        summary=summary,
        recent_events=recent_events,
        amb_lookup=amb_lookup,
        push_eligible_count=push_eligible_count,
        push_eligible_community=push_eligible_community,
        push_eligible_public=push_eligible_public,
        push_eligible_by_count=push_eligible_by_count,
        push_skipped_too_new=skipped_too_new,
        push_min_age_days=push_min_age,
        manual_email_eligibles=manual_email_eligibles,
        cron_emails_disabled=cron_emails_disabled,
        zoom_credentials_present=zoom_credentials_present,
        now_ts=datetime.now(timezone.utc),
        **_admin_layout_context(),
    )


@admin_bp.route("/live")
def live():
    """Live Monitor — countdown to campaign close, last 50 signups feed,
    last 20 referrals (unplugs) so the admin can keep the page open during
    a viral moment or attack and watch what's coming in real-time.
    """
    now = datetime.now(timezone.utc)
    cutoff_1h = now - timedelta(hours=1)
    cutoff_24h = now - timedelta(hours=24)

    # Last 50 signups (newest first)
    recent_signups = (
        Ambassador.query
        .order_by(Ambassador.created_at.desc())
        .limit(50)
        .all()
    )

    # Last 20 referrals
    recent_refs = (
        Referral.query
        .order_by(Referral.registered_at.desc())
        .limit(20)
        .all()
    )
    # Pre-resolve referrers for the feed
    ref_amb_ids = {r.ambassador_id for r in recent_refs}
    ref_lookup = {a.id: a for a in Ambassador.query.filter(Ambassador.id.in_(ref_amb_ids)).all()} if ref_amb_ids else {}

    # Velocity counters
    signups_1h = Ambassador.query.filter(Ambassador.created_at >= cutoff_1h).count()
    signups_24h = Ambassador.query.filter(Ambassador.created_at >= cutoff_24h).count()
    refs_1h = Referral.query.filter(Referral.registered_at >= cutoff_1h).count()
    refs_24h = Referral.query.filter(Referral.registered_at >= cutoff_24h).count()

    # Campaign close info for the big countdown clock
    close_iso = current_app.config.get("CAMPAIGN_CLOSE_ISO", "")

    return render_template(
        "admin_live.html",
        page_title="Live Monitor",
        active_section="live",
        recent_signups=recent_signups,
        recent_refs=recent_refs,
        ref_lookup=ref_lookup,
        signups_1h=signups_1h,
        signups_24h=signups_24h,
        refs_1h=refs_1h,
        refs_24h=refs_24h,
        close_iso=close_iso,
        now_ts=now,
        **_admin_layout_context(),
    )


@admin_bp.route("/")
def index():
    channel = request.args.get("channel", "all")
    q = request.args.get("q", "").strip().lower()

    # PERF: full-table fetch ONCE with referrals JOINed (needed by
    # _compute_suspicion which inspects each Referral's IP/UA), plus a
    # single SQL aggregation for referral counts (passed into helpers
    # that only need the count, avoiding the lazy property in loops).
    # Together these turned a 2500+ query / 75s timeout into a single-
    # digit query / sub-second page load.
    all_amb_for_stats = Ambassador.query.options(joinedload(Ambassador.referrals)).all()
    ref_counts = _get_referral_counts()

    if channel == "all":
        ambassadors = all_amb_for_stats  # reuse — don't re-query
    else:
        ambassadors = [a for a in all_amb_for_stats if a.source == channel]

    if q:
        ambassadors = [
            a for a in ambassadors
            if q in (a.name or "").lower() or q in (a.email or "").lower()
        ]

    sorted_ambassadors_full = sorted(ambassadors, key=lambda a: ref_counts.get(a.id, 0), reverse=True)

    # PERF: paginate the ambassador table at the route level to keep the
    # rendered HTML small. With 2,500 rows the unpaginated table was
    # ~5–10 MB output → slow Jinja render → timeout on hot workers.
    # Stats below still iterate the full set (cheap in-memory now that
    # N+1 is gone); only the table slice is rendered.
    PER_PAGE = 50
    page = max(1, request.args.get("page", default=1, type=int))
    total_filtered = len(sorted_ambassadors_full)
    pages = max(1, (total_filtered + PER_PAGE - 1) // PER_PAGE)
    sorted_ambassadors = sorted_ambassadors_full[(page - 1) * PER_PAGE : page * PER_PAGE]

    # Top-line stats (cheap aggregate counts via SQL or in-memory)
    total_referrals = _safe(lambda: Referral.query.count(), 0)
    community_count = sum(1 for a in all_amb_for_stats if a.source == "community")
    public_count = sum(1 for a in all_amb_for_stats if a.source == "public")
    unsubscribed = sum(1 for a in all_amb_for_stats if a.unsubscribed_at is not None)
    prizes_earned = _safe(lambda: MilestoneNotification.query.count(), 0)
    prizes_pending = _safe(lambda: MilestoneNotification.query.filter_by(delivered=False).count(), 0)

    # Marketing segments + chart data + email stats — wrapped in _safe
    # so a single broken helper degrades the dashboard instead of 500ing.
    segments = _safe(lambda: _compute_segments(all_amb_for_stats, referral_counts=ref_counts), {})
    segment_counts = {k: len(v) for k, v in segments.items()}
    charts = _safe(
        lambda: _compute_chart_data(ambassadors=all_amb_for_stats, referral_counts=ref_counts),
        {"timeline": {"labels": [], "community": [], "public": []},
         "distribution": {"labels": [], "values": []},
         "funnel": {"labels": [], "values": []}},
    )
    email_stats = _safe(_compute_email_stats, {})
    turnstile_stats = _safe(_compute_turnstile_stats, {})
    country_dist = _safe(_compute_country_distribution, {
        "labels": [], "counts": [], "flags": [], "geo": {},
        "other_breakdown": [], "total": 0, "with_country": 0,
        "coverage_pct": 0, "distinct_countries": 0, "max_count": 0,
    })

    # Engagement: how many ambassadors have opened their dashboard at least once
    visited = sum(1 for a in all_amb_for_stats if a.last_dashboard_visit_at is not None)

    # Suspicion: compute ONLY for the current page slice (was: all 2,500).
    # high_risk_total still counts across the full set so the headline
    # stat is accurate.
    risk_by_id = {a.id: _safe(_compute_suspicion, {"level": "clean", "score": 0, "reason": None, "total": 0}, a) for a in sorted_ambassadors}
    high_risk_total = _safe(
        lambda: sum(1 for a in all_amb_for_stats if _compute_suspicion(a)["level"] == "high"),
        0,
    )

    # How many velocity-throttled signups are sitting in the review queue
    pending_review_count = PendingReferral.query.filter_by(status="pending").count()

    layout_ctx = _admin_layout_context()
    layout_ctx["pending_review_count"] = pending_review_count  # already computed below
    return render_template(
        "admin.html",
        page_title="Overview",
        active_section="overview",
        ambassadors=sorted_ambassadors,
        # Pagination context
        page=page,
        pages=pages,
        per_page=PER_PAGE,
        total_filtered=total_filtered,
        total_ambassadors=len(all_amb_for_stats),
        total_referrals=total_referrals,
        community_count=community_count,
        public_count=public_count,
        unsubscribed=unsubscribed,
        prizes_earned=prizes_earned,
        prizes_pending=prizes_pending,
        visited_count=visited,
        channel=channel,
        q=q,
        segment_counts=segment_counts,
        charts=charts,
        email_stats=email_stats,
        now_ts=datetime.now(timezone.utc),
        tz_utc=timezone.utc,
        risk_by_id=risk_by_id,
        high_risk_total=high_risk_total,
        turnstile_stats=turnstile_stats,
        country_dist=country_dist,
        **layout_ctx,
    )


# ════════════════════════════════════════════════════════════════════
# Segment-based marketing actions
# ════════════════════════════════════════════════════════════════════

# Templated emails available for one-click "send to segment" actions.
# Maps a logical name → (mailer fn, segment-key default, idempotency-flag attr,
# label, min_age_days). min_age_days mirrors the cron-driven dispatch logic so
# manual sends from admin behave identically to automatic sends.
_SEGMENT_TEMPLATES = {
    "activation_nudge": {
        "fn": send_activation_nudge_email,
        "default_segment": "cold",
        "flag": "activation_nudge_sent_at",
        "label": "Activation nudge",
        "min_age_days": 2,  # don't pester ambassadors registered in the last 48h
    },
    "activation_push": {
        "fn": send_activation_push_email,
        "default_segment": "needs_activation",
        "flag": "activation_push_sent_at",
        "label": "Activation push (0-4 unplugs)",
        "min_age_days": 1,  # don't email people who registered today — too soon
    },
    "midway_reminder": {
        "fn": send_midway_reminder_email,
        "default_segment": "sleeping",
        "flag": "midway_sent_at",
        "label": "Midway reminder",
        "min_age_days": 7,  # midway reminder only for those ≥7 days in
    },
    # ── Manual class/webinar announcements (fired by admin from /admin/emails)
    # Audience: every active ambassador (no temperature/count gate). Default
    # segment "all" lets the route layer compute the eligible list.
    "class1_ready": {
        "fn": send_class1_ready_email,
        "default_segment": "all",
        "flag": "class1_email_sent_at",
        "label": "Class 1 ready (announcement)",
        "min_age_days": 0,
        # Skip recipients who already engaged with Class 1 — no point
        # nudging them to "watch" something they've already seen.
        "exclude_if_event_in": [
            "class1_viewed", "class1_progress_25", "class1_progress_50",
            "class1_progress_75", "class1_progress_95", "class1_completed",
        ],
    },
    "class2_ready": {
        "fn": send_class2_ready_email,
        "default_segment": "all",
        "flag": "class2_email_sent_at",
        "label": "Class 2 ready (announcement)",
        "min_age_days": 0,
        "exclude_if_event_in": [
            "class2_viewed", "class2_progress_25", "class2_progress_50",
            "class2_progress_75", "class2_progress_95", "class2_completed",
        ],
    },
    "webinar_reminder": {
        "fn": send_webinar_reminder_email,
        "default_segment": "all",
        "flag": "webinar_reminder_sent_at",
        "label": "Webinar reminder (1h before)",
        "min_age_days": 0,
        # Skip those who've already joined or clicked the link
        "exclude_if_event_in": ["webinar_joined", "webinar_link_clicked"],
    },
    "masterclass_invitation": {
        "fn": send_masterclass_invitation_email,
        "default_segment": "all",
        "flag": "masterclass_invitation_sent_at",
        "label": "Masterclass invitation (Zoom link · save the date)",
        "min_age_days": 0,
        # No event-based exclusion: this template invites winners to the
        # NEW masterclass with a fresh Zoom link, so anyone who attended a
        # past live (e.g. the launch sales webinar = webinar_joined event)
        # should ALSO receive this invite. Dedup is handled by the
        # masterclass_invitation_sent_at flag so it can never be sent twice
        # to the same person regardless.
    },
    "carrots_landing": {
        "fn": send_carrots_landing_email,
        "default_segment": "all",
        "flag": "carrots_landing_sent_at",
        "label": "Carrots & onions (landing page · 2 doors)",
        "min_age_days": 0,
    },
    "final_signal": {
        "fn": send_final_signal_email,
        "default_segment": "all",
        "flag": "final_signal_sent_at",
        "label": "Final signal (T-3h · class 2 closing + live tonight)",
        "min_age_days": 0,
    },
    "live_imminent": {
        "fn": send_live_imminent_email,
        "default_segment": "all",
        "flag": "live_imminent_sent_at",
        "label": "Live imminent (T-30min · join Zoom now)",
        "min_age_days": 0,
        # Skip those who've already joined the live or clicked the link
        "exclude_if_event_in": ["webinar_joined", "webinar_link_clicked"],
    },
    "class3_ready": {
        "fn": send_class3_ready_email,
        "default_segment": "all",
        "flag": "class3_email_sent_at",
        "label": "Class 3 ready (live masterclass replay)",
        "min_age_days": 0,
        "exclude_if_event_in": [
            "class3_viewed", "class3_progress_25", "class3_progress_50",
            "class3_progress_75", "class3_progress_95", "class3_completed",
        ],
    },
    # ── Weekend re-open: rewatch reminders for the 3 classes
    # Audience for each is computed dynamically by _compute_segments based
    # on the REWATCH_WINDOW_OPENS_AT cutoff (sleepers = first-watched
    # before, no view since). Empty exclude_if_event_in here on purpose:
    # the sleeper logic already filters out anyone who came back, and
    # we WANT past viewers (that's the whole audience).
    "class1_rewatch_reminder": {
        "fn": send_class1_rewatch_reminder_email,
        "default_segment": "sleepers_class1",
        "flag": "class1_rewatch_reminder_sent_at",
        "label": "Class 1 rewatch reminder (weekend re-open)",
        "min_age_days": 0,
    },
    "class2_rewatch_reminder": {
        "fn": send_class2_rewatch_reminder_email,
        "default_segment": "sleepers_class2",
        "flag": "class2_rewatch_reminder_sent_at",
        "label": "Class 2 rewatch reminder (weekend re-open)",
        "min_age_days": 0,
    },
    "class3_rewatch_reminder": {
        "fn": send_class3_rewatch_reminder_email,
        "default_segment": "sleepers_class3",
        "flag": "class3_rewatch_reminder_sent_at",
        "label": "Class 3 rewatch reminder (weekend re-open)",
        "min_age_days": 0,
    },
}


# Per-template lock to guarantee only ONE background send runs per template at
# a time. If a user clicks twice while a send is in flight, the second click
# returns immediately with an "already in progress" message instead of starting
# a parallel thread that could double-send to the small race window between
# "check sent_at" and "set sent_at".
_SEGMENT_SEND_LOCKS = {}
_SEGMENT_SEND_LOCKS_GUARD = threading.Lock()


def _get_segment_send_lock(key):
    with _SEGMENT_SEND_LOCKS_GUARD:
        if key not in _SEGMENT_SEND_LOCKS:
            _SEGMENT_SEND_LOCKS[key] = threading.Lock()
        return _SEGMENT_SEND_LOCKS[key]


@admin_bp.route("/segment/<segment_name>/send-template", methods=["POST"])
def segment_send_template(segment_name):
    """Send one of the pre-built emails to every ambassador in a segment.

    Two safeguards layered together:
      1. Per-template lock — only one background send for a given template
         can be in flight at any time. A second click is rejected, so a
         double-click can never produce duplicate emails.
      2. *_sent_at idempotency flag — within a thread, every ambassador is
         re-checked just before send. Already-sent ones are skipped.
      3. min_age_days filter — recently-registered ambassadors (less than
         N days since signup) are NOT pestered, mirroring cron logic.

    Sends in a background thread so the HTTP request doesn't hit Render's
    gunicorn worker timeout (~30s) when the segment has hundreds of targets.
    """
    template_key = request.form.get("template", "")
    cfg = _SEGMENT_TEMPLATES.get(template_key)
    if cfg is None:
        flash(f"Unknown template: {template_key}", "error")
        return redirect(url_for("admin.index"))

    # ── TEST / CANARY MODE: if only_email or only_emails is provided,
    # restrict the send to those specific addresses. Mirrors the full
    # route's code path (mailer fn, source-aware copy) so end-to-end is
    # verified end-to-end. Idempotency flag + min_age_days are skipped
    # so the admin can re-test on themselves repeatedly.
    #
    # only_email   — single address (legacy, used by activation_push UI)
    # only_emails  — comma OR newline separated, max 5 addresses (canary)
    #
    # Both forms are accepted on every template; we merge into one list.
    raw_single = (request.form.get("only_email", "") or "").strip()
    raw_multi = (request.form.get("only_emails", "") or "").strip()
    candidate_emails = []
    if raw_single:
        candidate_emails.append(raw_single)
    if raw_multi:
        for line in raw_multi.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                candidate_emails.append(line)
    # Dedupe + lowercase + cap at 5 to make accidental "paste 200 emails" impossible
    candidate_emails = list(dict.fromkeys(e.lower() for e in candidate_emails))[:5]

    if candidate_emails:
        flag = cfg["flag"]
        label = cfg["label"]
        fn = cfg["fn"]
        sent_results = []
        not_found = []
        for em in candidate_emails:
            amb = Ambassador.query.filter(func.lower(Ambassador.email) == em).first()
            if amb is None:
                not_found.append(em)
                continue
            try:
                ok = fn(amb, current_app.config["APP_URL"])
                sent_results.append((em, ok))
            except Exception as e:
                logger.exception("canary send failed for %s", em)
                sent_results.append((em, False))

        ok_count = sum(1 for _, ok in sent_results if ok)
        fail_count = len(sent_results) - ok_count
        msg_parts = [f"{label} CANARY · sent to {ok_count}/{len(sent_results)}"]
        if fail_count:
            failed_emails = ", ".join(em for em, ok in sent_results if not ok)
            msg_parts.append(f"failed: {failed_emails}")
        if not_found:
            msg_parts.append(f"not in DB: {', '.join(not_found)}")
        flash(
            " · ".join(msg_parts),
            "success" if (ok_count and not fail_count and not not_found) else "error",
        )
        return redirect(url_for("admin.emails"))

    # PERF: joinedload referrals + single referral_counts dict so
    # _compute_segments doesn't fire N+1 over 2500+ rows.
    all_amb = Ambassador.query.options(joinedload(Ambassador.referrals)).all()
    ref_counts = _get_referral_counts()
    segments = _compute_segments(all_amb, referral_counts=ref_counts)
    # "all" = every reachable (opted-in) ambassador. Used by class/webinar
    # announcements that should hit everyone, not a behavioural sub-segment.
    if segment_name == "all":
        targets = [a for a in all_amb if a.unsubscribed_at is None]
    else:
        targets = segments.get(segment_name, [])
    if not targets:
        flash(f"No ambassadors in segment '{segment_name}'.", "info")
        return redirect(url_for("admin.index"))

    flag = cfg["flag"]
    label = cfg["label"]
    fn = cfg["fn"]
    min_age_days = cfg.get("min_age_days", 0)
    exclude_if_event_in = cfg.get("exclude_if_event_in") or []

    # Filter 1: already-sent
    eligible = [a for a in targets if getattr(a, flag, None) is None]
    skipped_already_sent = len(targets) - len(eligible)

    # Filter 2: too recently registered (< min_age_days)
    skipped_too_new = 0
    if min_age_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)

        def _old_enough(a):
            c = a.created_at
            if c is None:
                return False
            if c.tzinfo is None:
                c = c.replace(tzinfo=timezone.utc)
            return c <= cutoff

        old_enough = [a for a in eligible if _old_enough(a)]
        skipped_too_new = len(eligible) - len(old_enough)
        eligible = old_enough

    # Filter 3: skip recipients who already engaged with this content
    # (e.g. don't send "Class 1 ready" to people who already viewed it).
    # Two-pathway lookup: events linked by email (Lovable class views) AND
    # events linked by ambassador_id with empty email (Zoom guest rematch).
    # The OR catches both — without this, ~169 attendees rematched by name
    # would slip through and get re-emailed.
    skipped_already_engaged = 0
    if exclude_if_event_in:
        from app.models import LeadEvent
        engaged_emails = set()
        # Path A: events that have an email captured
        for (em,) in (
            db.session.query(LeadEvent.email)
            .filter(LeadEvent.event_type.in_(exclude_if_event_in))
            .filter(LeadEvent.email.isnot(None))
            .distinct().all()
        ):
            if em:
                engaged_emails.add(em.lower())
        # Path B: events linked only by ambassador_id (empty email).
        # Resolve to canonical Ambassador.email via inner join.
        for (em,) in (
            db.session.query(Ambassador.email)
            .join(LeadEvent, LeadEvent.ambassador_id == Ambassador.id)
            .filter(LeadEvent.event_type.in_(exclude_if_event_in))
            .filter(or_(LeadEvent.email.is_(None), LeadEvent.email == ""))
            .filter(Ambassador.email.isnot(None))
            .distinct().all()
        ):
            if em:
                engaged_emails.add(em.lower())
        before = len(eligible)
        eligible = [
            a for a in eligible
            if a.email and a.email.lower() not in engaged_emails
        ]
        skipped_already_engaged = before - len(eligible)

    if not eligible:
        flash(
            f"{label}: nothing to send. {skipped_already_sent} already received, "
            f"{skipped_too_new} too recent (<{min_age_days} days since signup), "
            f"{skipped_already_engaged} already engaged with this content.",
            "info",
        )
        return redirect(url_for("admin.index"))

    # Concurrency lock — refuse second click while a send is in flight
    lock = _get_segment_send_lock(template_key)
    if not lock.acquire(blocking=False):
        flash(
            f"{label}: already sending in background. Wait a few minutes and refresh "
            f"to see how many got sent.",
            "info",
        )
        return redirect(url_for("admin.index"))

    target_ids = [a.id for a in eligible]
    app = current_app._get_current_object()
    app_url = current_app.config["APP_URL"]

    def background_send():
        try:
            with app.app_context():
                from app.models import db, Ambassador
                sent_count = failed_count = skipped_in_thread = 0
                for amb_id in target_ids:
                    amb = Ambassador.query.get(amb_id)
                    if amb is None:
                        continue
                    if getattr(amb, flag, None) is not None:
                        # Race-safe: another thread (or admin click) flipped this
                        # flag while we were processing. Skip silently.
                        skipped_in_thread += 1
                        continue
                    try:
                        if template_key == "midway_reminder":
                            ok = fn(amb, position=None, days_left=None, app_url=app_url)
                        else:
                            ok = fn(amb, app_url)
                        if ok:
                            setattr(amb, flag, datetime.now(timezone.utc))
                            db.session.commit()
                            sent_count += 1
                        else:
                            failed_count += 1
                    except Exception:
                        db.session.rollback()
                        logger.exception("bg send failed for ambassador_id=%d", amb_id)
                        failed_count += 1
                logger.warning(
                    "BG segment send DONE: segment=%s template=%s sent=%d failed=%d "
                    "skipped_inthread=%d total_queued=%d",
                    segment_name, template_key, sent_count, failed_count,
                    skipped_in_thread, len(target_ids),
                )
        finally:
            lock.release()

    thread = threading.Thread(target=background_send, daemon=True)
    thread.start()

    flash(
        f"{label}: started sending to {len(eligible)} ambassadors in background "
        f"(skipped {skipped_already_sent} already received, "
        f"{skipped_too_new} too recent <{min_age_days} days). "
        f"Refresh /admin in 3-5 min for progress.",
        "success",
    )
    logger.warning(
        "ADMIN segment send STARTED: segment=%s template=%s eligible=%d "
        "skipped_sent=%d skipped_new=%d",
        segment_name, template_key, len(eligible), skipped_already_sent, skipped_too_new,
    )
    return redirect(url_for("admin.index"))


@admin_bp.route("/broadcast", methods=["POST"])
def broadcast():
    """Send a custom subject+body email to a chosen segment.

    Body is plain text; we wrap it in the brand HTML shell. Skips opt-outs.
    """
    segment_name = request.form.get("segment", "")
    subject = request.form.get("subject", "").strip()
    body_text = request.form.get("body", "").strip()

    if not subject or not body_text:
        flash("Subject and body are required.", "error")
        return redirect(url_for("admin.index"))

    # PERF: joinedload + ref_counts to avoid N+1 in _compute_segments
    all_amb = Ambassador.query.options(joinedload(Ambassador.referrals)).all()
    ref_counts = _get_referral_counts()
    segments = _compute_segments(all_amb, referral_counts=ref_counts)
    targets = segments.get(segment_name, [])
    if not targets:
        flash(f"No ambassadors in segment '{segment_name}'.", "info")
        return redirect(url_for("admin.index"))

    app_url = current_app.config["APP_URL"]
    sent, failed = 0, 0

    # Render a minimal brand HTML wrapper around the plain body. We deliberately
    # keep this dead simple: bold paragraph breaks + a "go to dashboard" footer.
    body_html_template = """\
<!doctype html><html><body style="margin:0;padding:0;background:#000000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#ffffff;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#000000;padding:24px 0;">
  <tr><td align="center">
    <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;background:#0a0f0c;border:1px solid rgba(46,219,153,0.25);border-radius:12px;">
      <tr><td style="padding:28px 28px 8px 28px;">
        <p style="font-family:'Share Tech Mono','Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#2EDB99;margin:0 0 16px 0;">▌ METAKIZZ // BROADCAST</p>
        <p style="font-size:18px;line-height:1.5;color:#ffffff;margin:0;font-weight:700;">Hey {name},</p>
      </td></tr>
      <tr><td style="padding:8px 28px 24px 28px;font-size:15px;line-height:1.6;color:#d1d5db;">
        {body}
      </td></tr>
      <tr><td style="padding:0 28px 28px 28px;">
        <a href="{dashboard_url}" style="display:inline-block;background:#2EDB99;color:#000000;font-weight:900;text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px;letter-spacing:1px;text-transform:uppercase;">Open my dashboard →</a>
      </td></tr>
      <tr><td style="padding:0 28px 24px 28px;border-top:1px solid rgba(46,219,153,0.15);">
        <p style="font-size:11px;color:#6b7280;margin:16px 0 0 0;">Jesus & Anni · MetaKizz Project</p>
        <p style="font-size:10px;color:#4b5563;margin:6px 0 0 0;">Don't want these? <a href="{unsub_url}" style="color:#6b7280;text-decoration:underline;">Unsubscribe</a>.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""

    # Body paragraphs → wrap each line in <p>
    paragraphs = "".join(f"<p style=\"margin:0 0 14px 0;\">{p}</p>" for p in body_text.split("\n\n") if p.strip())

    for amb in targets:
        if amb.unsubscribed_at is not None:
            continue
        dashboard_url = f"{app_url.rstrip('/')}/dashboard/{amb.dashboard_code}"
        unsub_url = f"{app_url.rstrip('/')}/unsubscribe/{amb.unsubscribe_token}"
        html = body_html_template.format(
            name=(amb.name or "dancer").split()[0],
            body=paragraphs,
            dashboard_url=dashboard_url,
            unsub_url=unsub_url,
        )
        try:
            ok = _mailer_send(
                amb.email, subject, html,
                from_name="Jesus & Anni",
                template_key="broadcast",
                ambassador=amb,
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception:
            logger.exception("broadcast failed for %s", amb.email)
            failed += 1

    flash(f"Broadcast to '{segment_name}': sent {sent}, failed {failed} (skipped opt-outs).",
          "success" if sent else "error")
    logger.warning("ADMIN BROADCAST: segment=%s subject=%r sent=%d failed=%d",
                   segment_name, subject, sent, failed)
    return redirect(url_for("admin.index"))


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


# ════════════════════════════════════════════════════════════════════
# Prize structure — source of truth for which physical prize each
# winner gets, computed from referral_count + source bucket. Centralized
# here so the rewards page, CSV export, and any future automation
# all read the same labels.
# ════════════════════════════════════════════════════════════════════

PRIZE_GUARANTEED = {
    "community": "1 month of MetaDancers, free",
    "public":    "Live musicality masterclass with Jesus & Anni (€97)",
}

PRIZE_TOP3 = {
    "community": [
        "1 year of MetaDancers, free (€1,000+)",
        "Video feedback on your dancing (€150+)",
        "Personalized MetaKizz hoodie (€60+)",
    ],
    "public": [
        "Video feedback on your dancing (€150+)",
        "Personalized MetaKizz hoodie (€60+)",
        "Personalized MetaKizz t-shirt (€30+)",
    ],
}


def _build_winners():
    """Compute the live list of prize winners from current ambassador state.

    Returns a tuple (guaranteed_winners, top3_by_source, delivery_lookup):
      - guaranteed_winners: list of dicts (one per ambassador with 5+ unplugs)
      - top3_by_source:     {'community': [up to 3 dicts], 'public': [...]}
      - delivery_lookup:    {(ambassador_id, slot): PrizeDelivery row}

    Excludes ambassadors flagged under_review_at — they're hidden from
    the public leaderboard, so they shouldn't claim ranking prizes.
    """
    # PERF: eager-load referrals so referral_count doesn't N+1
    all_amb = Ambassador.query.options(joinedload(Ambassador.referrals)).all()

    # Pull existing delivery records once so we can decorate each winner
    deliveries = PrizeDelivery.query.all()
    delivery_lookup = {(d.ambassador_id, d.slot): d for d in deliveries}

    # ── Guaranteed (5+ unplugs, any source) ──
    qualifying = [a for a in all_amb if a.referral_count >= 5]
    qualifying.sort(key=lambda a: (-a.referral_count, a.created_at))
    guaranteed_winners = []
    for a in qualifying:
        prize_label = PRIZE_GUARANTEED.get(a.source, "Guaranteed reward")
        guaranteed_winners.append({
            "amb": a,
            "prize": prize_label,
            "slot": "guaranteed",
            "delivered": delivery_lookup.get((a.id, "guaranteed")) is not None
                         and delivery_lookup[(a.id, "guaranteed")].delivered_at is not None,
            "delivery": delivery_lookup.get((a.id, "guaranteed")),
        })

    # ── Top 3 per source bucket (excluding under-review) ──
    top3_by_source = {}
    for src in ("community", "public"):
        eligible = [a for a in all_amb
                    if a.source == src
                    and a.under_review_at is None
                    and a.referral_count > 0]
        eligible.sort(key=lambda a: (-a.referral_count, a.created_at))
        prizes = PRIZE_TOP3.get(src, [])
        rows = []
        for i, a in enumerate(eligible[:3]):
            slot = f"top3_{src}_{i+1}"
            prize_label = prizes[i] if i < len(prizes) else f"Top {i+1}"
            rows.append({
                "amb": a,
                "rank": i + 1,
                "prize": prize_label,
                "slot": slot,
                "delivered": delivery_lookup.get((a.id, slot)) is not None
                             and delivery_lookup[(a.id, slot)].delivered_at is not None,
                "delivery": delivery_lookup.get((a.id, slot)),
            })
        top3_by_source[src] = rows

    return guaranteed_winners, top3_by_source, delivery_lookup


@admin_bp.route("/rewards")
def rewards():
    """Live prize delivery list — who has won what + contact info +
    delivery status. Recomputed on every load from current ambassador
    state so the list reflects the leaderboard as it stands right now.
    """
    guaranteed_winners, top3_by_source, _ = _build_winners()

    total_guaranteed = len(guaranteed_winners)
    total_top3 = sum(len(rows) for rows in top3_by_source.values())
    total_delivered = sum(1 for w in guaranteed_winners if w["delivered"]) \
                      + sum(1 for rows in top3_by_source.values() for w in rows if w["delivered"])
    total_to_deliver = total_guaranteed + total_top3
    total_pending = total_to_deliver - total_delivered

    return render_template(
        "admin_rewards.html",
        page_title="Rewards",
        active_section="rewards",
        guaranteed_winners=guaranteed_winners,
        top3_by_source=top3_by_source,
        total_guaranteed=total_guaranteed,
        total_top3=total_top3,
        total_delivered=total_delivered,
        total_pending=total_pending,
        total_to_deliver=total_to_deliver,
        **_admin_layout_context(),
    )


@admin_bp.route("/rewards/<int:ambassador_id>/<slot>/mark", methods=["POST"])
def mark_prize_delivered(ambassador_id, slot):
    """Toggle delivery state for a single (ambassador, slot) prize."""
    amb = Ambassador.query.get_or_404(ambassador_id)
    delivered_now = request.form.get("delivered", "1") == "1"
    notes = (request.form.get("notes", "") or "").strip()
    prize_label = (request.form.get("prize_label", "") or "").strip() or "(unspecified)"

    row = PrizeDelivery.query.filter_by(
        ambassador_id=ambassador_id, slot=slot
    ).first()

    if row is None:
        row = PrizeDelivery(
            ambassador_id=ambassador_id,
            slot=slot,
            prize_label=prize_label,
        )
        db.session.add(row)

    if delivered_now:
        row.delivered_at = datetime.now(timezone.utc)
    else:
        row.delivered_at = None
    if notes:
        row.delivered_notes = notes
    if prize_label and prize_label != "(unspecified)":
        row.prize_label = prize_label

    db.session.commit()
    action = "marked delivered" if delivered_now else "reverted to pending"
    flash(f"{amb.name} · {slot} · {action}.", "success")
    logger.warning("PRIZE %s: amb=%s (id=%d) slot=%s", action.upper(), amb.email, amb.id, slot)
    return redirect(url_for("admin.rewards"))


@admin_bp.route("/rewards/export")
def rewards_export():
    """CSV export of all winners with full contact info — for prize fulfillment."""
    guaranteed_winners, top3_by_source, _ = _build_winners()

    output = io.StringIO()
    w = csv.writer(output)
    w.writerow([
        "slot", "rank", "name", "email", "phone", "country",
        "source", "unplugs", "prize", "delivered_at", "delivered_notes",
        "dashboard_url",
    ])

    app_url = current_app.config.get("APP_URL", "").rstrip("/")

    for row in guaranteed_winners:
        a = row["amb"]
        d = row.get("delivery")
        w.writerow([
            "guaranteed", "",
            a.name, a.email, a.phone_number or "", a.country_code or "",
            a.source, a.referral_count, row["prize"],
            d.delivered_at.isoformat() if d and d.delivered_at else "",
            (d.delivered_notes or "") if d else "",
            f"{app_url}/dashboard/{a.dashboard_code}" if app_url else a.dashboard_code,
        ])

    for src, rows in top3_by_source.items():
        for row in rows:
            a = row["amb"]
            d = row.get("delivery")
            w.writerow([
                f"top3-{src}", row["rank"],
                a.name, a.email, a.phone_number or "", a.country_code or "",
                a.source, a.referral_count, row["prize"],
                d.delivered_at.isoformat() if d and d.delivered_at else "",
                (d.delivered_notes or "") if d else "",
                f"{app_url}/dashboard/{a.dashboard_code}" if app_url else a.dashboard_code,
            ])

    csv_data = output.getvalue()
    response = Response(csv_data, mimetype="text/csv")
    response.headers["Content-Disposition"] = (
        f"attachment; filename=metakizz_winners_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    )
    return response


# Old MilestoneNotification routes kept for backward-compat with any
# in-flight links. The new rewards page uses PrizeDelivery instead.
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
        fake.id = None  # so EmailEvent rows from tests use ambassador_id=NULL (no real-user pollution)
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

            elif email_type == "activation_push":
                # Test the personalized "X away from your reward" push.
                # Default count = 3 so the recipient sees "2 unplugs left".
                fake.referral_count = 3
                success = send_activation_push_email(fake, app_url)

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

            elif email_type == "class1_ready":
                fake.referral_count = 0
                success = send_class1_ready_email(fake, app_url)

            elif email_type == "class2_ready":
                fake.referral_count = 0
                success = send_class2_ready_email(fake, app_url)

            elif email_type == "webinar_reminder":
                fake.referral_count = 0
                success = send_webinar_reminder_email(fake, app_url)

            elif email_type == "masterclass_invitation":
                fake.referral_count = 0
                success = send_masterclass_invitation_email(fake, app_url)

            elif email_type == "carrots_landing":
                fake.referral_count = 0
                success = send_carrots_landing_email(fake, app_url)

            elif email_type == "final_signal":
                fake.referral_count = 0
                success = send_final_signal_email(fake, app_url)

            elif email_type == "live_imminent":
                fake.referral_count = 0
                success = send_live_imminent_email(fake, app_url)

            elif email_type == "class3_ready":
                fake.referral_count = 0
                success = send_class3_ready_email(fake, app_url)

            elif email_type == "class1_rewatch_reminder":
                fake.referral_count = 0
                success = send_class1_rewatch_reminder_email(fake, app_url)

            elif email_type == "class2_rewatch_reminder":
                fake.referral_count = 0
                success = send_class2_rewatch_reminder_email(fake, app_url)

            elif email_type == "class3_rewatch_reminder":
                fake.referral_count = 0
                success = send_class3_rewatch_reminder_email(fake, app_url)

            elif email_type == "reservation_first50":
                # Reservation-based template — build a fake Reservation row.
                class _FakeRes:
                    pass
                fr = _FakeRes()
                fr.email = to_email
                fr.name = fake.name
                fr.amount_cents = 10000  # 100€ for preview
                fr.ambassador = None
                success = send_reservation_first50_email(fr)

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


@admin_bp.route("/ambassador/<int:ambassador_id>")
def ambassador_detail(ambassador_id):
    """Per-ambassador deep dive: profile, email timeline, referrals with IP
    clusters, dashboard visit history. The single place to investigate
    a suspicious ambassador or answer "what happened with this person".
    """
    amb = Ambassador.query.get_or_404(ambassador_id)

    referrals = (
        Referral.query
        .filter_by(ambassador_id=amb.id)
        .order_by(Referral.registered_at.desc())
        .all()
    )

    email_events = (
        EmailEvent.query
        .filter_by(ambassador_id=amb.id)
        .order_by(EmailEvent.created_at.desc())
        .all()
    )

    # Group email events per template, then per event_type, so the template
    # can render rows like:
    #   welcome:  sent ✓ · opened ✓ · clicked —
    #   activation_nudge: sent ✓ · opened —
    emails_by_template = {}
    for e in email_events:
        bucket = emails_by_template.setdefault(e.template_key, {
            "sent": None, "delivered": None, "opened": None,
            "clicked": None, "bounced": None, "complained": None,
        })
        # Keep the EARLIEST occurrence of each event type (first sent, first opened, etc.)
        if e.event_type in bucket and bucket[e.event_type] is None:
            bucket[e.event_type] = e
    # Convert into a sortable list ordered by 'sent' time desc
    template_order = [
        "welcome", "first_unplug", "activation_nudge", "guaranteed_prize",
        "midway_reminder", "final_48h", "last_6h", "results", "you_won", "broadcast",
    ]
    emails_summary = []
    for key in template_order:
        if key in emails_by_template:
            emails_summary.append((key, emails_by_template[key]))
    # Append unknown templates at end
    for key, value in emails_by_template.items():
        if key not in template_order:
            emails_summary.append((key, value))

    # IP cluster breakdown — what IPs are repeated across this ambassador's referrals?
    ip_buckets = {}
    ua_buckets = {}
    for ref in referrals:
        if ref.signup_ip:
            ip_buckets.setdefault(ref.signup_ip, []).append(ref)
        if ref.signup_user_agent:
            ua_buckets.setdefault(ref.signup_user_agent, []).append(ref)
    # Keep only IPs with >1 referral (the suspicious clusters)
    ip_clusters = {ip: refs for ip, refs in ip_buckets.items() if len(refs) > 1}

    risk = _compute_suspicion(amb)

    # Who invited THIS ambassador? Look for a Referral row where the email
    # matches their email — that row's ambassador_id is the inviter.
    invited_by = None
    invited_by_referral = (
        Referral.query.filter_by(email=amb.email).first()
    )
    if invited_by_referral is not None:
        invited_by = Ambassador.query.get(invited_by_referral.ambassador_id)

    # ── Forensic engagement check on each referral ──
    # For each person they referred, look up the Ambassador row by email
    # and pull: welcome-email events (sent/delivered/opened/clicked/bounced)
    # and dashboard_visit_count. Builds a per-referral health badge so the
    # admin can spot fakes (bounced welcome = fake email; signed up but
    # never opened email AND never visited dashboard = ghost signup).
    referral_engagement = {}
    summary = {
        "total": len(referrals), "opened": 0, "clicked": 0,
        "bounced": 0, "delivered": 0, "visited": 0,
        "ghost": 0,  # no email events AND no dashboard visit
    }
    if referrals:
        emails_lower = [r.email.lower() for r in referrals if r.email]
        ref_ambs = (
            Ambassador.query
            .filter(func.lower(Ambassador.email).in_(emails_lower))
            .all()
        )
        amb_by_email = {a.email.lower(): a for a in ref_ambs}

        # Group EmailEvent welcome rows by ambassador_id, keyed by event_type
        welcome_events_by_amb = defaultdict(set)
        if ref_ambs:
            ref_amb_ids = [a.id for a in ref_ambs]
            evt_rows = (
                EmailEvent.query
                .filter(EmailEvent.ambassador_id.in_(ref_amb_ids))
                .filter(EmailEvent.template_key == "welcome")
                .all()
            )
            for e in evt_rows:
                welcome_events_by_amb[e.ambassador_id].add(e.event_type)

        for r in referrals:
            target = amb_by_email.get((r.email or "").lower())
            events = welcome_events_by_amb.get(target.id, set()) if target else set()
            visits = (target.dashboard_visit_count or 0) if target else 0

            engagement = {
                "has_ambassador": target is not None,
                "sent": "sent" in events,
                "delivered": "delivered" in events,
                "opened": "opened" in events,
                "clicked": "clicked" in events,
                "bounced": "bounced" in events,
                "visits": visits,
            }
            # Health classification
            if engagement["bounced"]:
                engagement["health"] = "bounced"
            elif engagement["clicked"] or engagement["opened"]:
                engagement["health"] = "engaged"
            elif visits > 0:
                engagement["health"] = "visited"
            elif engagement["delivered"] or engagement["sent"]:
                engagement["health"] = "silent"
            else:
                engagement["health"] = "ghost"

            referral_engagement[r.id] = engagement
            if engagement["opened"]:
                summary["opened"] += 1
            if engagement["clicked"]:
                summary["clicked"] += 1
            if engagement["bounced"]:
                summary["bounced"] += 1
            if engagement["delivered"]:
                summary["delivered"] += 1
            if visits > 0:
                summary["visited"] += 1
            if engagement["health"] == "ghost":
                summary["ghost"] += 1

    # Country lookup for the metadata block (flag + name)
    from app.services.phone import lookup_country
    country_name, country_flag = lookup_country(amb.country_code)

    # Detect duplicate-by-typo emails inside this ambassador's referral list
    # (e.g. letasha617@gmail.com vs letasha617@gmail.co — telltale of a fake
    # second registration with the same prefix on a near-miss domain).
    dup_prefix_groups = {}
    for r in referrals:
        if not r.email or "@" not in r.email:
            continue
        prefix = r.email.split("@", 1)[0].lower()
        dup_prefix_groups.setdefault(prefix, []).append(r)
    duplicate_prefix_refs = {
        pfx: rs for pfx, rs in dup_prefix_groups.items() if len(rs) > 1
    }

    return render_template(
        "admin_ambassador_detail.html",
        amb=amb,
        referrals=referrals,
        email_events=email_events,
        emails_summary=emails_summary,
        risk=risk,
        ip_clusters=ip_clusters,
        ip_buckets=ip_buckets,
        ua_buckets=ua_buckets,
        invited_by=invited_by,
        invited_by_referral=invited_by_referral,
        referral_engagement=referral_engagement,
        engagement_summary=summary,
        duplicate_prefix_refs=duplicate_prefix_refs,
        country_name=country_name,
        country_flag=country_flag,
        now_ts=datetime.now(timezone.utc),
    )


@admin_bp.route("/backfill-phones", methods=["GET", "POST"])
def backfill_phones():
    """Bulk-import phone numbers from a GHL CSV export.

    Accepts a CSV upload with at least these columns (header row required):
      email,phone

    Other columns are ignored. For each row:
    - Lower-cases the email and looks up the matching Ambassador
    - Parses the phone via libphonenumber → E.164 + ISO country
    - Updates phone_number + country_code on that ambassador

    Idempotent: re-running with the same CSV is a no-op for already-set
    rows. Phones that fail to parse are logged but don't block the rest.
    Returns a summary (matched / updated / skipped / unparseable).
    """
    if request.method == "GET":
        return render_template("admin_backfill_phones.html")

    from app.services.phone import parse as parse_phone

    f = request.files.get("file")
    if f is None or not f.filename:
        flash("No file uploaded.", "error")
        return redirect(url_for("admin.backfill_phones"))

    try:
        text_data = f.stream.read().decode("utf-8-sig", errors="replace")
    except Exception:
        flash("Could not decode the file as UTF-8 CSV.", "error")
        return redirect(url_for("admin.backfill_phones"))

    reader = csv.DictReader(io.StringIO(text_data))
    if reader.fieldnames is None:
        flash("CSV has no header row.", "error")
        return redirect(url_for("admin.backfill_phones"))

    # Find email + phone columns case-insensitively
    fname_lower = {fn.lower().strip(): fn for fn in reader.fieldnames}
    email_col = next((fname_lower[k] for k in ("email", "email address", "contact email") if k in fname_lower), None)
    phone_col = next((fname_lower[k] for k in ("phone", "phone number", "contact phone", "phone_number") if k in fname_lower), None)

    if not email_col or not phone_col:
        flash(
            f"CSV must include 'email' and 'phone' columns. "
            f"Found columns: {', '.join(reader.fieldnames)}",
            "error",
        )
        return redirect(url_for("admin.backfill_phones"))

    stats = {"rows": 0, "matched": 0, "updated": 0, "unparseable": 0, "no_match": 0, "already_set": 0}
    no_match_emails = []

    for row in reader:
        stats["rows"] += 1
        email = (row.get(email_col) or "").strip().lower()
        raw_phone = (row.get(phone_col) or "").strip()
        if not email or not raw_phone:
            continue

        amb = Ambassador.query.filter(func.lower(Ambassador.email) == email).first()
        if amb is None:
            stats["no_match"] += 1
            if len(no_match_emails) < 12:
                no_match_emails.append(email)
            continue
        stats["matched"] += 1

        if amb.phone_number and amb.country_code:
            stats["already_set"] += 1
            continue

        parsed = parse_phone(raw_phone)
        if not parsed:
            stats["unparseable"] += 1
            continue

        amb.phone_number = parsed["e164"]
        amb.country_code = parsed["country_code"]
        stats["updated"] += 1

    db.session.commit()

    msg = (
        f"Backfill complete. {stats['rows']} rows · "
        f"{stats['matched']} matched · "
        f"{stats['updated']} updated · "
        f"{stats['already_set']} already had a phone · "
        f"{stats['no_match']} no Ambassador match · "
        f"{stats['unparseable']} bad phone numbers."
    )
    if no_match_emails:
        msg += f" Sample no-match: {', '.join(no_match_emails)}"
    flash(msg, "success")
    logger.warning("ADMIN PHONE BACKFILL: %s", stats)
    return redirect(url_for("admin.backfill_phones"))


@admin_bp.route("/referral/<int:referral_id>/delete", methods=["POST"])
def remove_referral(referral_id):
    """Remove ONE Referral row (the attribution). The new Ambassador row
    that the referral pointed to is left intact — they keep their dashboard
    and stay registered, just no longer credited to this referrer.

    Use cases:
    - Admin attributed by mistake
    - Referrer's referral turned out to be a fake/bot
    - Referrer asked to drop a specific person

    Note: this does NOT clear guaranteed_prize_sent_at, even if the count
    drops below 5. The email already went out; we don't unsend.
    """
    ref = Referral.query.get_or_404(referral_id)
    referrer_id = ref.ambassador_id
    referrer = Ambassador.query.get(referrer_id)
    referrer_name = referrer.name if referrer else "(deleted)"
    ref_name = ref.name
    ref_email = ref.email

    db.session.delete(ref)
    db.session.commit()

    flash(
        f"Removed {ref_name} ({ref_email}) from {referrer_name}'s referrals. "
        f"Their Ambassador record was kept — they still have access to their dashboard.",
        "success",
    )
    logger.warning(
        "ADMIN REMOVE REFERRAL: referrer=%s (id=%s) <- removed %s (%s)",
        referrer.email if referrer else "(none)", referrer_id, ref_email, ref_name,
    )
    if referrer_id is None:
        return redirect(url_for("admin.index"))
    return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer_id))


@admin_bp.route("/api/ambassadors/search")
def api_ambassadors_search():
    """Live-search existing ambassadors by name or email for the manual-
    referral picker. Returns up to `limit` results as JSON.

    Each result includes `has_referrer` so the picker can grey out anyone
    already attributed (we'd refuse the manual add anyway).
    """
    from flask import jsonify

    q = (request.args.get("q") or "").strip().lower()
    limit = min(int(request.args.get("limit") or 8), 25)

    if len(q) < 2:
        return jsonify([])

    pattern = f"%{q}%"
    rows = (
        Ambassador.query
        .filter(
            db.or_(
                func.lower(Ambassador.name).like(pattern),
                func.lower(Ambassador.email).like(pattern),
            )
        )
        .order_by(Ambassador.created_at.desc())
        .limit(limit)
        .all()
    )

    # Bulk-check who already has a referrer (one Referral row per email)
    emails_lower = [a.email.lower() for a in rows]
    referred = set()
    if emails_lower:
        ref_rows = (
            Referral.query
            .filter(func.lower(Referral.email).in_(emails_lower))
            .all()
        )
        referred = {r.email.lower() for r in ref_rows}

    results = []
    for a in rows:
        results.append({
            "id": a.id,
            "name": a.name,
            "email": a.email,
            "source": a.source,
            "referral_count": a.referral_count,
            "has_referrer": a.email.lower() in referred,
            "created_at": a.created_at.strftime("%b %d") if a.created_at else None,
        })
    return jsonify(results)


@admin_bp.route("/ambassador/<int:ambassador_id>/add-referral", methods=["POST"])
def add_referral_manually(ambassador_id):
    """Admin override: attribute a referral to this ambassador without going
    through the normal signup flow.

    Use case: people claim they registered via someone's link but the
    attribution didn't capture (forgot to click ref link, used different
    device, etc.). The admin manually credits them.

    Logic:
    - Validates email syntax
    - Refuses self-referral
    - If a Referral row with this email already exists (regardless of
      attributed ambassador) → refuses with explanatory error
    - If an Ambassador with this email exists → links via new Referral row
    - If no Ambassador with this email → creates one (source='public')
      then links via Referral row
    - DOES NOT send any emails (admin override). The admin can trigger
      guaranteed_prize via the existing Backfill #4 button if applicable.
    """
    from app.services.email_validation import is_valid_email_syntax
    import secrets

    referrer = Ambassador.query.get_or_404(ambassador_id)
    name = (request.form.get("name", "") or "").strip()
    email = (request.form.get("email", "") or "").strip().lower()

    if not name or not email:
        flash("Name and email are required.", "error")
        return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer.id))

    if not is_valid_email_syntax(email):
        flash(f"Email '{email}' doesn't look valid.", "error")
        return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer.id))

    if email == (referrer.email or "").lower():
        flash("Can't credit someone with referring themselves.", "error")
        return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer.id))

    # Already credited to anyone (this referrer or another)?
    existing_ref = Referral.query.filter_by(email=email).first()
    if existing_ref is not None:
        existing_referrer = Ambassador.query.get(existing_ref.ambassador_id)
        existing_name = existing_referrer.name if existing_referrer else "(deleted)"
        flash(
            f"{email} is already credited to {existing_name}. "
            f"Reset that referral first if you want to reattribute.",
            "error",
        )
        return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer.id))

    # Find or create the Ambassador for this email (so they get a dashboard
    # too — same as a normal signup but without emails/Turnstile/velocity).
    target = Ambassador.query.filter_by(email=email).first()
    target_was_created = False
    if target is None:
        # Generate unique codes (same approach as create_signup)
        def _gen():
            return secrets.token_urlsafe(6)[:8]
        ref_code = _gen()
        while Ambassador.query.filter_by(referral_code=ref_code).first():
            ref_code = _gen()
        dash_code = _gen()
        while Ambassador.query.filter_by(dashboard_code=dash_code).first():
            dash_code = _gen()

        target = Ambassador(
            name=name,
            email=email,
            referral_code=ref_code,
            dashboard_code=dash_code,
            source="public",
        )
        db.session.add(target)
        db.session.flush()  # get target.id without full commit
        target_was_created = True

    # Create the Referral row crediting `referrer`. No IP/UA — admin manual.
    referral = Referral(
        ambassador_id=referrer.id,
        name=name,
        email=email,
    )
    db.session.add(referral)
    db.session.commit()

    if target_was_created:
        flash(
            f"Manually credited {name} ({email}) to {referrer.name}. "
            f"Created a new Ambassador row for them too.",
            "success",
        )
    else:
        flash(
            f"Manually credited existing ambassador {target.name} ({email}) "
            f"to {referrer.name}.",
            "success",
        )

    logger.warning(
        "ADMIN MANUAL REFERRAL: %s (id=%d) <- %s (%s)",
        referrer.email, referrer.id, email, name,
    )
    return redirect(url_for("admin.ambassador_detail", ambassador_id=referrer.id))


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


# ════════════════════════════════════════════════════════════════════
# Pending referrals review queue (velocity-throttled signups)
# ════════════════════════════════════════════════════════════════════

@admin_bp.route("/pending")
def pending_review():
    """Show signups queued for manual review (velocity-throttled).

    Each row represents a signup whose attribution to a referrer has been
    held because the referrer was receiving signups too fast. Approve to
    credit the referrer; reject to discard.
    """
    status_filter = request.args.get("status", "pending")
    q = PendingReferral.query
    if status_filter in ("pending", "approved", "rejected"):
        q = q.filter_by(status=status_filter)
    items = q.order_by(PendingReferral.received_at.desc()).all()

    counts = {
        "pending": PendingReferral.query.filter_by(status="pending").count(),
        "approved": PendingReferral.query.filter_by(status="approved").count(),
        "rejected": PendingReferral.query.filter_by(status="rejected").count(),
    }

    # Group pending by referrer for the bulk-action UI
    by_referrer = defaultdict(list)
    if status_filter == "pending":
        for p in items:
            by_referrer[p.referrer_ambassador_id].append(p)

    return render_template(
        "admin_pending.html",
        items=items,
        counts=counts,
        status_filter=status_filter,
        by_referrer=by_referrer,
    )


def _maybe_clear_under_review(referrer_ambassador_id):
    """If a referrer has no remaining pending items, lift their review flag.

    Called after each approve/reject. Idempotent and safe to call when there
    is no referrer (NULL ambassador_id) — does nothing in that case.
    """
    if not referrer_ambassador_id:
        return
    has_more = PendingReferral.query.filter_by(
        referrer_ambassador_id=referrer_ambassador_id, status="pending",
    ).count()
    if has_more == 0:
        amb = Ambassador.query.get(referrer_ambassador_id)
        if amb and amb.under_review_at is not None:
            amb.under_review_at = None
            db.session.commit()
            logger.warning(
                "Cleared under_review_at for ambassador %d (%s) — all pending processed",
                amb.id, amb.email,
            )


@admin_bp.route("/pending/<int:pending_id>/approve", methods=["POST"])
def pending_approve(pending_id):
    """Approve a pending referral → create the real Referral row."""
    p = PendingReferral.query.get_or_404(pending_id)
    if p.status != "pending":
        flash(f"Already {p.status}.", "info")
        return redirect(url_for("admin.pending_review"))

    # Don't double-credit if a real Referral already exists for that email
    existing = Referral.query.filter_by(email=p.email).first()
    if existing is None and p.referrer_ambassador_id is not None:
        db.session.add(Referral(
            ambassador_id=p.referrer_ambassador_id,
            name=p.name,
            email=p.email,
            signup_ip=p.signup_ip,
            signup_user_agent=p.signup_user_agent,
        ))

    p.status = "approved"
    p.reviewed_at = datetime.now(timezone.utc)
    db.session.commit()
    _maybe_clear_under_review(p.referrer_ambassador_id)

    flash(f"Approved: {p.name} <{p.email}> credited to referrer.", "success")
    logger.warning("ADMIN PendingReferral APPROVED: id=%d email=%s referrer_id=%s",
                   p.id, p.email, p.referrer_ambassador_id)
    return redirect(url_for("admin.pending_review"))


@admin_bp.route("/pending/<int:pending_id>/reject", methods=["POST"])
def pending_reject(pending_id):
    """Reject a pending referral. No real Referral row is created."""
    p = PendingReferral.query.get_or_404(pending_id)
    if p.status != "pending":
        flash(f"Already {p.status}.", "info")
        return redirect(url_for("admin.pending_review"))

    p.status = "rejected"
    p.reviewed_at = datetime.now(timezone.utc)
    p.reviewed_notes = request.form.get("notes", "").strip() or None
    db.session.commit()
    _maybe_clear_under_review(p.referrer_ambassador_id)

    flash(f"Rejected: {p.name} <{p.email}>.", "success")
    logger.warning("ADMIN PendingReferral REJECTED: id=%d email=%s referrer_id=%s",
                   p.id, p.email, p.referrer_ambassador_id)
    return redirect(url_for("admin.pending_review"))


@admin_bp.route("/pending/bulk-reject-from/<int:referrer_id>", methods=["POST"])
def pending_bulk_reject(referrer_id):
    """Reject ALL pending referrals from a single referrer in one click.
    Useful when you confirm a bot attack and want to nuke 40 fake signups.
    """
    pendings = PendingReferral.query.filter_by(
        referrer_ambassador_id=referrer_id, status="pending",
    ).all()
    now = datetime.now(timezone.utc)
    n = 0
    for p in pendings:
        p.status = "rejected"
        p.reviewed_at = now
        p.reviewed_notes = "bulk_reject_from_referrer"
        n += 1
    db.session.commit()
    _maybe_clear_under_review(referrer_id)

    flash(f"Bulk-rejected {n} pending referrals from referrer #{referrer_id}.", "success")
    logger.warning("ADMIN bulk reject: referrer_id=%d count=%d", referrer_id, n)
    return redirect(url_for("admin.pending_review"))


# ════════════════════════════════════════════════════════════════════
# GHL SYNC — pull contacts from GoHighLevel into our Ambassador table
# ════════════════════════════════════════════════════════════════════

# Module-level state for sync progress (single-instance deploy on Render
# means this is fine; if we ever scale to multiple workers, move to DB).
_GHL_SYNC_STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "stats": None,
    "error": None,
}


@admin_bp.route("/sync-ghl/cleanup-old-launch", methods=["POST"])
def sync_ghl_cleanup_old_launch():
    """Delete ghost leads (source='ghl_import') that don't carry the
    current-launch tag mkot3_registrado. Removes leftover ghosts from
    previous campaigns (masterclass march17th, webinnar 17 marzo)
    while keeping all current-launch leads.
    """
    from app.services import ghl as ghl_service
    try:
        stats = ghl_service.cleanup_ghost_leads_without_required_tag("mkot3_registrado")
        flash(
            f"Old-launch cleanup done: scanned {stats['scanned']} ghost leads, "
            f"kept {stats['kept_with_tag']} (current launch), "
            f"deleted {stats['deleted']} (previous campaigns only).",
            "success",
        )
    except Exception as e:
        logger.exception("old-launch ghost cleanup failed")
        flash(f"Cleanup failed: {e}", "error")
    return redirect(url_for("admin.sync_ghl"))


@admin_bp.route("/sync-ghl/cleanup", methods=["POST"])
def sync_ghl_cleanup():
    """Delete ghost leads (source='ghl_import') that don't carry the
    mkot3_registrado tag. Used to undo a sync that ran without the tag
    filter (which would have pulled in past-masterclass attendees, etc.).
    """
    from app.services import ghl as ghl_service
    try:
        stats = ghl_service.cleanup_ghost_leads_without_relevant_tag()
        flash(
            f"Cleanup done: scanned {stats['scanned']} ghost leads, "
            f"kept {stats['kept_with_tag']} (had launch tag), "
            f"deleted {stats['deleted']} (no launch tag).",
            "success",
        )
    except Exception as e:
        logger.exception("ghost cleanup failed")
        flash(f"Cleanup failed: {e}", "error")
    return redirect(url_for("admin.sync_ghl"))


@admin_bp.route("/sync-ghl", methods=["GET", "POST"])
def sync_ghl():
    """Page that shows GHL sync status + a button to trigger a fresh sync.

    GET → render status page (auto-refreshes while running).
    POST → kick off a background sync, redirect back to GET.
    """
    from app.services import ghl as ghl_service

    if request.method == "POST":
        if _GHL_SYNC_STATE["running"]:
            flash("A GHL sync is already running.", "info")
            return redirect(url_for("admin.sync_ghl"))

        if not ghl_service.is_configured():
            flash(
                "GHL not configured. Set GHL_PRIVATE_TOKEN and GHL_LOCATION_ID "
                "in Render env vars.",
                "error",
            )
            return redirect(url_for("admin.sync_ghl"))

        # Default: do NOT create new ghost leads. Only enrich existing
        # Ambassador rows. The user can opt in to ghost creation via a
        # checkbox in the form (rare; usually only useful right after
        # importing a fresh Ambassador list).
        create_missing_flag = (request.form.get("create_missing") == "1")

        flask_app = current_app._get_current_object()

        def _run():
            with flask_app.app_context():
                _GHL_SYNC_STATE["running"] = True
                _GHL_SYNC_STATE["started_at"] = datetime.now(timezone.utc)
                _GHL_SYNC_STATE["finished_at"] = None
                _GHL_SYNC_STATE["stats"] = None
                _GHL_SYNC_STATE["error"] = None
                try:
                    # User chose: only enrich existing leads, don't create
                    # ghosts from GHL contacts that aren't already in our DB.
                    # The form can override via the "create_missing" checkbox.
                    create_missing = create_missing_flag
                    stats = ghl_service.sync_all_contacts(create_missing=create_missing)
                    _GHL_SYNC_STATE["stats"] = stats
                except Exception as e:
                    logger.exception("GHL sync background thread failed")
                    _GHL_SYNC_STATE["error"] = str(e)
                finally:
                    _GHL_SYNC_STATE["finished_at"] = datetime.now(timezone.utc)
                    _GHL_SYNC_STATE["running"] = False

        threading.Thread(target=_run, daemon=True).start()
        flash("GHL sync started. Refresh this page to see progress.", "success")
        return redirect(url_for("admin.sync_ghl"))

    # ── GET: render status page ──
    state = _GHL_SYNC_STATE
    is_configured = ghl_service.is_configured()

    if state["running"]:
        elapsed = (datetime.now(timezone.utc) - state["started_at"]).total_seconds() if state["started_at"] else 0
        status_html = f'''
        <div style="padding:14px 18px; background:rgba(255,200,87,0.1); border:1px solid #FFC857; border-radius:6px;">
          <p style="color:#FFC857; font-size:13px; letter-spacing:2px;">▌ SYNC RUNNING · {int(elapsed)}s elapsed</p>
        </div>
        '''
    elif state["error"]:
        status_html = f'''
        <div style="padding:14px 18px; background:rgba(220,38,38,0.15); border:1px solid #DC2626; border-radius:6px;">
          <p style="color:#FCA5A5; font-size:13px;">▌ LAST RUN FAILED</p>
          <p style="color:#FCA5A5; font-size:12px; margin-top:6px;">{state["error"][:300]}</p>
        </div>
        '''
    elif state["stats"]:
        elapsed = (state["finished_at"] - state["started_at"]).total_seconds() if state["started_at"] and state["finished_at"] else 0
        rows = "".join(
            f'<tr><td style="padding:6px 10px; color:#9CA3AF;">{k}</td><td style="padding:6px 10px; color:#2EDB99; text-align:right;"><strong>{v}</strong></td></tr>'
            for k, v in state["stats"].items()
        )
        status_html = f'''
        <div style="padding:14px 18px; background:rgba(46,219,153,0.08); border:1px solid #2EDB99; border-radius:6px;">
          <p style="color:#2EDB99; font-size:13px; letter-spacing:2px;">▌ LAST SYNC OK · {int(elapsed)}s · finished {state["finished_at"].strftime('%H:%M:%S UTC')}</p>
          <table style="margin-top:12px; font-family:'Share Tech Mono',monospace; font-size:13px;">{rows}</table>
        </div>
        '''
    else:
        status_html = '<div style="color:#6B7280; font-size:13px;">No sync run yet.</div>'

    refresh_meta = '<meta http-equiv="refresh" content="5">' if state["running"] else ""
    config_warning = ""
    if not is_configured:
        config_warning = '''
        <div style="padding:14px 18px; background:rgba(220,38,38,0.15); border:1px solid #DC2626; border-radius:6px; margin-bottom:18px;">
          <p style="color:#FCA5A5; font-size:13px;">⚠ GHL not configured. Set <code style="color:#fff;">GHL_PRIVATE_TOKEN</code> + <code style="color:#fff;">GHL_LOCATION_ID</code> in Render → Environment Variables → save → redeploy.</p>
        </div>
        '''

    button_html = ""
    if is_configured and not state["running"]:
        # Count ghosts split by relevance: contacts that carry ANY of the
        # tracked launch/masterclass tags vs. those that carry none.
        from app.services.ghl import RELEVANT_LEAD_TAGS as _RELEVANT_TAGS
        ghost_total = Ambassador.query.filter(Ambassador.source == "ghl_import").count()
        from sqlalchemy import or_
        relevant_clauses = [Ambassador.ghl_tags.like(f"%{t}%") for t in _RELEVANT_TAGS]
        ghost_relevant = Ambassador.query.filter(
            Ambassador.source == "ghl_import",
            or_(*relevant_clauses),
        ).count()
        ghost_irrelevant = ghost_total - ghost_relevant
        relevant_tags_html = ", ".join(f'<code style="color:#FFC857;">{t}</code>' for t in sorted(_RELEVANT_TAGS))

        # DB breakdown so user can verify nothing is lost
        public_count = Ambassador.query.filter_by(source="public").count()
        community_count = Ambassador.query.filter_by(source="community").count()
        total_amb = Ambassador.query.count()

        # Ghost breakdown by tag — current launch vs past campaigns only
        ghost_current_launch = Ambassador.query.filter(
            Ambassador.source == "ghl_import",
            Ambassador.ghl_tags.like("%mkot3_registrado%"),
        ).count()
        ghost_past_only = ghost_total - ghost_current_launch  # in launch DB but not current launch

        button_html = f'''
        <div style="margin-top:24px; padding:14px 18px; background:rgba(46,219,153,0.05); border:1px solid rgba(46,219,153,0.25); border-radius:6px;">
          <p style="color:#2EDB99; font-size:11px; letter-spacing:2px; text-transform:uppercase; margin:0 0 10px 0;">▌ Current DB</p>
          <table style="font-family:'Share Tech Mono',monospace; font-size:13px; color:#C9CFD4;">
            <tr><td style="padding:3px 18px 3px 0;">Total Ambassadors</td><td style="color:#2EDB99; font-weight:bold;">{total_amb}</td></tr>
            <tr><td style="padding:3px 18px 3px 0;">→ Source: public (signup)</td><td style="color:#fff;">{public_count}</td></tr>
            <tr><td style="padding:3px 18px 3px 0;">→ Source: community</td><td style="color:#fff;">{community_count}</td></tr>
            <tr><td style="padding:3px 18px 3px 0;">→ Source: ghl_import (ghosts) total</td><td style="color:#FFC857;">{ghost_total}</td></tr>
            <tr><td style="padding:3px 18px 3px 18px; color:#9CA3AF;">  └ with mkot3_registrado (current launch)</td><td style="color:#2EDB99;">{ghost_current_launch}</td></tr>
            <tr><td style="padding:3px 18px 3px 18px; color:#9CA3AF;">  └ ONLY past campaigns (no current tag)</td><td style="color:#FCA5A5;">{ghost_past_only}</td></tr>
          </table>
          <p style="color:#6B7280; font-size:10px; margin:10px 0 0 0; line-height:1.5;">
            Sync NEVER deletes rows. matched_updated = leads that were enriched (UTMs, dance level, tags, phones).
          </p>
        </div>

        <form method="post" style="margin-top:20px;">
          <button type="submit" style="background:#2EDB99; color:#000; border:0; padding:14px 28px; font-family:'Orbitron',sans-serif; font-weight:900; letter-spacing:2px; text-transform:uppercase; cursor:pointer; box-shadow:0 0 16px rgba(46,219,153,0.45); font-size:13px;">▶ Enrich existing leads only</button>
          <p style="color:#6B7280; font-size:11px; margin-top:8px; line-height:1.6;">
            Default: pulls every GHL contact and updates fields on
            ambassadors that <strong>already exist</strong> in our DB.
            <strong style="color:#2EDB99;">Won't create new ghost rows.</strong>
            ~1-2 min, idempotent.
          </p>

          <label style="display:block; margin-top:14px; font-size:11px; color:#FCA5A5;">
            <input type="checkbox" name="create_missing" value="1" style="margin-right:6px;">
            Also create ghost rows for GHL contacts not in our DB (carrying any of: {relevant_tags_html})
          </label>
        </form>

        <div style="margin-top:32px; padding:18px; background:rgba(220,38,38,0.08); border:1px solid rgba(220,38,38,0.4); border-radius:6px;">
          <p style="color:#FCA5A5; font-size:12px; letter-spacing:2px; text-transform:uppercase; margin:0 0 10px 0;">▌ Cleanup ghosts</p>
          <p style="color:#C9CFD4; font-size:13px; line-height:1.5; margin:0 0 14px 0;">
            Ghost leads breakdown — only ghosts (source=ghl_import) are affected. Real signups never touched.
          </p>

          <!-- Primary cleanup: keep only current launch ghosts -->
          <div style="margin-bottom:16px; padding:14px; background:rgba(220,38,38,0.12); border-radius:4px;">
            <p style="color:#fff; font-size:13px; margin:0 0 4px 0;">
              <strong style="color:#FCA5A5;">{ghost_past_only}</strong> ghost{'s' if ghost_past_only != 1 else ''} from PREVIOUS campaigns (no <code style="color:#FFC857;">mkot3_registrado</code> tag)
            </p>
            <p style="color:#9CA3AF; font-size:11px; margin:0 0 12px 0;">
              These are masterclass/webinar attendees from past launches who didn't sign up to this one. Click to remove.
              <strong style="color:#2EDB99;">Current-launch ghosts ({ghost_current_launch}) are kept.</strong>
            </p>
            <form method="post" action="/admin/sync-ghl/cleanup-old-launch" onsubmit="return confirm('Delete {ghost_past_only} ghost leads that DON\\'t have mkot3_registrado tag? Current-launch ghosts ({ghost_current_launch}) and real signups stay intact.');">
              <button type="submit" {'disabled' if ghost_past_only == 0 else ''} style="background:#DC2626; color:#fff; border:0; padding:10px 20px; font-family:'Share Tech Mono',monospace; font-weight:bold; letter-spacing:1.5px; text-transform:uppercase; cursor:pointer; font-size:11px; border-radius:3px; {'opacity:0.4; cursor:not-allowed;' if ghost_past_only == 0 else ''}">Delete {ghost_past_only} past-campaign ghost{'s' if ghost_past_only != 1 else ''}</button>
            </form>
          </div>

          <!-- Secondary: remove ghosts with NO relevant tag at all (rare) -->
          <details style="margin-top:8px;">
            <summary style="color:#9CA3AF; font-size:11px; cursor:pointer; letter-spacing:0.1em; text-transform:uppercase;">Other cleanup options</summary>
            <p style="color:#9CA3AF; font-size:11px; margin:10px 0; line-height:1.5;">
              <strong style="color:#FCA5A5;">{ghost_irrelevant}</strong> ghost{'s' if ghost_irrelevant != 1 else ''} carry no relevant tag at all (would be unusual; current sync filters these out).
            </p>
            <form method="post" action="/admin/sync-ghl/cleanup" onsubmit="return confirm('Delete {ghost_irrelevant} ghost leads with NO relevant tag?');">
              <button type="submit" {'disabled' if ghost_irrelevant == 0 else ''} style="background:#7F1D1D; color:#fff; border:0; padding:8px 16px; font-family:'Share Tech Mono',monospace; font-weight:bold; letter-spacing:1px; text-transform:uppercase; cursor:pointer; font-size:10px; border-radius:3px; {'opacity:0.4; cursor:not-allowed;' if ghost_irrelevant == 0 else ''}">Delete {ghost_irrelevant} no-tag ghosts</button>
            </form>
          </details>
        </div>
        '''

    return f'''<!doctype html>
<html><head>
<meta charset="utf-8"/>
{refresh_meta}
<title>GHL Sync · MetaKizz</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
 body {{ background:#000; color:#fff; font-family:'Share Tech Mono','Courier New',monospace; padding:24px; max-width:720px; margin:0 auto; }}
 h1 {{ color:#2EDB99; font-size:18px; letter-spacing:2.5px; text-transform:uppercase; margin:0 0 6px 0; font-family:'Orbitron',sans-serif; font-weight:900; }}
 .sub {{ color:#9CA3AF; font-size:12px; letter-spacing:1.5px; text-transform:uppercase; margin-bottom:24px; }}
 a {{ color:#2EDB99; }}
</style>
</head><body>
<h1>▌ GHL Sync</h1>
<p class="sub">Pull contacts from GoHighLevel · enrich Ambassador rows with tags + UTMs + phones</p>
{config_warning}
{status_html}
{button_html}
<p style="margin-top:30px; font-size:11px; color:#4B5563;">
  <a href="/admin/leads-debug">▌ Lead events</a> · <a href="/admin/">▌ Back to admin</a>
</p>
</body></html>'''


# ════════════════════════════════════════════════════════════════════
# LEADS DASHBOARD — filtered + temperature-scored view of all leads
# ════════════════════════════════════════════════════════════════════

@admin_bp.route("/plf-status")
def plf_status():
    """Lightweight PLF tracking diagnostic — single-query aggregates.

    Built specifically for "is class viewing being recorded?" question
    during the launch. NO scoring, NO N+1 risk, NO memory pressure.
    Just SQL COUNT(DISTINCT email) per event type. Renders in <100ms
    even with millions of events.
    """
    from app.models import LeadEvent

    # Single SQL aggregate over distinct emails for the PLF funnel events.
    funnel_event_types = [
        "class1_viewed", "class1_progress_25", "class1_progress_50",
        "class1_progress_75", "class1_progress_95", "class1_completed",
        "class2_viewed", "class2_progress_25", "class2_progress_50",
        "class2_progress_75", "class2_progress_95", "class2_completed",
        "class3_viewed", "class3_progress_25", "class3_progress_50",
        "class3_progress_75", "class3_progress_95", "class3_completed",
        "webinar_link_clicked", "webinar_joined",
        "purchase_completed",
    ]
    rows = (
        db.session.query(LeadEvent.event_type, func.count(func.distinct(LeadEvent.email)))
        .filter(LeadEvent.event_type.in_(funnel_event_types))
        .group_by(LeadEvent.event_type)
        .all()
    )
    counts = {et: 0 for et in funnel_event_types}
    for et, n in rows:
        counts[et] = n

    # Total events of any type, plus events in the last hour (live signal)
    total_events = db.session.query(func.count(LeadEvent.id)).scalar() or 0
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    last_hour_count = (
        db.session.query(func.count(LeadEvent.id))
        .filter(LeadEvent.created_at >= one_hour_ago)
        .scalar() or 0
    )
    five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    last_5min_count = (
        db.session.query(func.count(LeadEvent.id))
        .filter(LeadEvent.created_at >= five_min_ago)
        .scalar() or 0
    )

    # Latest 10 events to show "live feed" — minimal columns only
    latest_rows = (
        db.session.query(
            LeadEvent.created_at, LeadEvent.email,
            LeadEvent.event_type, LeadEvent.pct,
        )
        .order_by(LeadEvent.created_at.desc())
        .limit(10)
        .all()
    )

    def _bar(n, max_n):
        if max_n == 0:
            return ""
        pct = int(round(100 * n / max_n))
        bar_len = int(round(pct / 2))  # 50 chars max
        return f"{'█' * bar_len}{'░' * (50 - bar_len)} {pct}%"

    # Build funnel rows for class 1, 2, 3, webinar, purchase
    class1_max = max(counts["class1_viewed"], 1)
    class2_max = max(counts["class2_viewed"], 1)

    rows_html = []
    def _add_row(label, n, max_for_bar, color="#2EDB99"):
        bar = _bar(n, max_for_bar) if max_for_bar > 0 else ""
        rows_html.append(f"""
        <tr>
          <td style="padding:10px 14px;color:#fff;font-family:'Share Tech Mono',monospace;">{label}</td>
          <td style="padding:10px 14px;color:{color};font-family:'Orbitron',sans-serif;font-weight:900;font-size:18px;text-align:right;text-shadow:0 0 10px rgba(46,219,153,0.4);">{n}</td>
          <td style="padding:10px 14px;color:#6B7280;font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:0.5px;">{bar}</td>
        </tr>""")

    _add_row("▌ Class 1 — Viewed (gate)",   counts["class1_viewed"],   class1_max)
    _add_row("→ 25% watched",                counts["class1_progress_25"], class1_max)
    _add_row("→ 50% watched",                counts["class1_progress_50"], class1_max, "#FFC857")
    _add_row("→ 75% watched",                counts["class1_progress_75"], class1_max, "#FFC857")
    _add_row("→ 95% watched",                counts["class1_progress_95"], class1_max, "#F97316")
    _add_row("→ Completed",                  counts["class1_completed"], class1_max, "#DC2626")
    rows_html.append('<tr><td colspan="3" style="padding:8px 0;"></td></tr>')
    _add_row("▌ Class 2 — Viewed",           counts["class2_viewed"],   class2_max)
    _add_row("→ 50% watched",                counts["class2_progress_50"], class2_max, "#FFC857")
    _add_row("→ 95% watched",                counts["class2_progress_95"], class2_max, "#F97316")
    _add_row("→ Completed",                  counts["class2_completed"], class2_max, "#DC2626")
    rows_html.append('<tr><td colspan="3" style="padding:8px 0;"></td></tr>')
    _add_row("▌ Webinar — Link clicked",     counts["webinar_link_clicked"], max(counts["class1_viewed"], 1))
    _add_row("▌ Webinar — Joined",           counts["webinar_joined"], max(counts["class1_viewed"], 1), "#A78BFA")
    _add_row("▌ Purchase — Completed",       counts["purchase_completed"], max(counts["class1_viewed"], 1), "#A78BFA")

    latest_html = []
    for ts, em, et, pct in latest_rows:
        ts_str = ts.strftime("%H:%M:%S") if ts else "—"
        pct_str = f" · {pct}%" if pct is not None else ""
        latest_html.append(f"""
        <tr>
          <td style="padding:6px 12px;color:#9CA3AF;font-size:11px;">{ts_str}</td>
          <td style="padding:6px 12px;color:#FFC857;font-size:11px;">{et}{pct_str}</td>
          <td style="padding:6px 12px;color:#fff;font-size:11px;">{em or '—'}</td>
        </tr>""")

    pulse_class = "live" if last_5min_count > 0 else "idle"
    pulse_color = "#2EDB99" if last_5min_count > 0 else "#6B7280"

    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="10">
<title>PLF Status · MetaKizz</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
 body {{ background:#000;color:#fff;font-family:'Share Tech Mono',monospace;padding:24px;max-width:1100px;margin:0 auto; }}
 h1 {{ color:#2EDB99;font-family:'Orbitron',sans-serif;font-weight:900;font-size:24px;letter-spacing:2px;text-transform:uppercase;margin:0 0 6px 0;text-shadow:0 0 14px rgba(46,219,153,0.4); }}
 .sub {{ color:#9CA3AF;font-size:11px;letter-spacing:0.25em;text-transform:uppercase;margin-bottom:24px; }}
 .pulse {{ display:inline-block;padding:4px 12px;background:rgba(46,219,153,0.15);border:1px solid {pulse_color};border-radius:4px;color:{pulse_color};font-size:11px;letter-spacing:0.2em;text-transform:uppercase; }}
 .kpis {{ display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:32px; }}
 .kpi {{ background:rgba(46,219,153,0.05);border:1px solid rgba(46,219,153,0.25);border-radius:8px;padding:14px 16px; }}
 .kpi .lbl {{ color:#9CA3AF;font-size:10px;letter-spacing:0.2em;text-transform:uppercase; }}
 .kpi .num {{ font-family:'Orbitron',sans-serif;font-weight:900;font-size:28px;color:#2EDB99;text-shadow:0 0 12px rgba(46,219,153,0.4);margin-top:2px; }}
 table {{ width:100%;border-collapse:collapse; }}
 .funnel-table th {{ text-align:left;padding:10px 14px;color:#2EDB99;font-size:10px;letter-spacing:0.2em;text-transform:uppercase;border-bottom:1px solid rgba(46,219,153,0.3); }}
 .funnel-table tbody tr {{ border-bottom:1px solid rgba(255,255,255,0.04); }}
 .latest-table th {{ text-align:left;padding:8px 12px;color:#2EDB99;font-size:9px;letter-spacing:0.2em;text-transform:uppercase;border-bottom:1px solid rgba(46,219,153,0.3); }}
 a {{ color:#2EDB99; }}
 .section-label {{ font-family:'Share Tech Mono',monospace;font-size:9px;color:#6B7280;letter-spacing:0.3em;text-transform:uppercase;margin:28px 0 8px 0; }}
</style>
</head><body>
<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px;">
  <div>
    <h1>▌ PLF Status</h1>
    <p class="sub">▌ live tracking · auto-refresh 10s</p>
  </div>
  <span class="pulse">● {pulse_class.upper()} · {last_5min_count} events / 5m</span>
</div>

<div class="kpis">
  <div class="kpi"><div class="lbl">Total events</div><div class="num">{total_events}</div></div>
  <div class="kpi"><div class="lbl">Last hour</div><div class="num">{last_hour_count}</div></div>
  <div class="kpi"><div class="lbl">Last 5 min</div><div class="num">{last_5min_count}</div></div>
</div>

<div class="section-label">▌ Funnel · distinct emails per event</div>
<table class="funnel-table">
  <thead><tr><th>Step</th><th style="text-align:right;">Count</th><th>vs class viewed</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>

<div class="section-label">▌ Latest 10 events</div>
<table class="latest-table">
  <thead><tr><th>Time UTC</th><th>Event</th><th>Email</th></tr></thead>
  <tbody>{''.join(latest_html) if latest_html else '<tr><td colspan="3" style="padding:14px;color:#9CA3AF;">No events yet</td></tr>'}</tbody>
</table>

<p style="margin-top:32px;font-size:11px;color:#4B5563;">
  <a href="/admin/leads-debug">▌ Lead events log</a> ·
  <a href="/admin/leads">▌ Leads</a> ·
  <a href="/admin/leads/insights">▌ Insights (heavy)</a> ·
  <a href="/admin/">▌ Overview</a>
</p>
</body></html>"""
    return html


@admin_bp.route("/leads/insights")
def leads_insights():
    """Marketer dashboard: funnel, source × temperature matrix, time-series,
    top countries, top referrers, action queue.

    PERF: this version uses SQL aggregations everywhere (no per-lead
    scoring loop). Only the action-queue candidates get scored — and
    even those are pre-filtered via SQL to <50 candidates with phones
    and recent hot events. Page loads in well under 1s on prod scale.
    """
    from app.services.temperature import (
        compute_temperature, fetch_signals_bulk,
        classify_source, SOURCE_BUCKETS, TEMP_BUCKETS, temp_label_to_key,
    )
    from app.models import LeadEvent, LeadNote
    ref_counts = _get_referral_counts()

    # ── KPIs from SQL ──
    total_leads = _safe(lambda: Ambassador.query.count(), 0)

    # Distinct emails per relevant funnel event — single GROUP BY query.
    # Use the canonical helpers from temperature.py so this matches every
    # other counter on the site (PLF totals on /admin/leads, the launch
    # funnel, etc.). Includes class 3 (live-replay) automatically.
    from app.services.temperature import (
        class_started_event_types, class_completed_event_types,
        class_visited_event_types,
    )
    funnel_event_keys = set()
    for cn in (1, 2, 3):
        funnel_event_keys.update(class_started_event_types(cn))
        funnel_event_keys.update(class_completed_event_types(cn))
        funnel_event_keys.update(class_visited_event_types(cn))
    funnel_event_keys.update(["webinar_joined", "purchase_completed"])
    funnel_event_keys = list(funnel_event_keys)

    rows = _safe(
        lambda: db.session.query(
            LeadEvent.event_type, func.count(func.distinct(LeadEvent.email))
        ).filter(LeadEvent.event_type.in_(funnel_event_keys))
         .group_by(LeadEvent.event_type)
         .all(),
        [],
    )
    event_counts = {k: 0 for k in funnel_event_keys}
    for et, n in rows:
        event_counts[et] = n

    # Canonical event-type lists from temperature.py — guarantees the
    # /admin/leads/insights funnel matches the /admin/leads funnel and
    # PLF counters.
    from app.services.temperature import (
        class_started_event_types as _started_evts,
        class_completed_event_types as _completed_evts,
    )

    def _ge(class_n, threshold):
        """Distinct emails who reached >= threshold% in class N (SQL-derived).
        Uses the canonical helpers — `class_viewed` (page-load only) is
        NOT counted as 'started'; that's tracked separately via Visited."""
        if threshold <= 25:
            keys = _started_evts(class_n)
        elif threshold <= 50:
            keys = [f"class{class_n}_progress_{p}" for p in (50, 75, 95)] + [f"class{class_n}_completed"]
        elif threshold <= 75:
            keys = [f"class{class_n}_progress_{p}" for p in (75, 95)] + [f"class{class_n}_completed"]
        else:  # >=95
            keys = _completed_evts(class_n)
        return _safe(
            lambda: db.session.query(func.count(func.distinct(LeadEvent.email)))
                .filter(LeadEvent.event_type.in_(keys)).scalar() or 0,
            0,
        )

    n_class1 = _ge(1, 25)
    n_class1_done = _ge(1, 95)
    n_class2 = _ge(2, 25)
    n_class2_done = _ge(2, 95)
    n_class3 = _ge(3, 25)
    n_class3_done = _ge(3, 95)
    n_webinar = event_counts.get("webinar_joined", 0)
    n_purchased = event_counts.get("purchase_completed", 0)

    # Hot/burning approximation: people who completed any class (≥95%) OR
    # joined webinar OR purchased. Uses canonical "completed" definition
    # across all 3 classes (class 3 is the live-replay).
    _hot_event_keys = (
        _completed_evts(1) + _completed_evts(2) + _completed_evts(3)
        + ["webinar_joined", "purchase_completed"]
    )
    n_hot_or_burning = _safe(
        lambda: db.session.query(func.count(func.distinct(LeadEvent.email)))
            .filter(LeadEvent.event_type.in_(_hot_event_keys)).scalar() or 0,
        0,
    )
    n_customers = n_purchased
    pct_hot_burning = round(100 * n_hot_or_burning / total_leads, 1) if total_leads else 0
    pct_customers = round(100 * n_customers / total_leads, 1) if total_leads else 0

    funnel_steps = [
        {"label": "Registered",        "count": total_leads,   "color": "#2EDB99"},
        {"label": "Started Class 1",   "count": n_class1,      "color": "#2EDB99"},
        {"label": "Finished Class 1",  "count": n_class1_done, "color": "#FFC857"},
        {"label": "Started Class 2",   "count": n_class2,      "color": "#FFC857"},
        {"label": "Finished Class 2",  "count": n_class2_done, "color": "#F97316"},
        {"label": "Joined Live",       "count": n_webinar,     "color": "#DC2626"},
        {"label": "Started Class 3",   "count": n_class3,      "color": "#DC2626"},
        {"label": "Finished Class 3",  "count": n_class3_done, "color": "#A78BFA"},
        {"label": "Purchased",         "count": n_purchased,   "color": "#A78BFA"},
    ]
    # Compute drop-off % between consecutive steps + width % for visual bar
    for i, step in enumerate(funnel_steps):
        step["pct_of_total"] = round(100 * step["count"] / total_leads, 1) if total_leads else 0
        if i == 0:
            step["dropoff_pct"] = 0
        else:
            prev = funnel_steps[i - 1]["count"]
            step["dropoff_pct"] = round(100 * (prev - step["count"]) / prev, 1) if prev else 0

    # ── Temperature × Origin matrix (SQL-driven) ──
    # We bucket each lead's strongest event into a temperature key,
    # then group by their utm_source bucket. Single query per
    # origin × temp pair would be many queries, so we do it in one
    # pass: pull (email, max_strongest_event) joined with Ambassador's
    # utm columns, then aggregate in Python over a small result set.
    temp_keys = ["burning", "hot", "warm", "cool", "cold", "customer"]
    origin_keys = [k for k, _ in SOURCE_BUCKETS]
    matrix = {ok: {tk: 0 for tk in temp_keys} for ok in origin_keys}

    # Step 1: per email, what is the strongest event tier they have?
    # Query distinct (email, event_type) pairs, then bucket in Python.
    bucket_query_events = list(set(funnel_event_keys))  # all funnel events
    rows = _safe(
        lambda: db.session.query(LeadEvent.email, LeadEvent.event_type)
            .filter(LeadEvent.event_type.in_(bucket_query_events))
            .distinct().all(),
        [],
    )
    by_email = defaultdict(set)
    for em, et in rows:
        if em:
            by_email[em.lower()].add(et)

    # Use the module-level launch-day classifier for consistency with
    # /admin/leads + /admin/plf-status. (Local closure removed.)

    # Step 2: pull Ambassador → utm columns map (single query)
    amb_origin_rows = _safe(
        lambda: db.session.query(
            func.lower(Ambassador.email),
            Ambassador.utm_source, Ambassador.utm_medium,
            Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid,
        ).filter(Ambassador.email.isnot(None)).all(),
        [],
    )

    def _utm_to_origin(src, med, fb, gc, tt):
        s = (src or "").lower()
        m = (med or "").lower()
        is_paid = any(k in m for k in ("cpc", "paid", "ads", "ad ")) or m in ("ad", "paid")
        if "tiktok" in s or tt:
            return "tiktok_ad" if is_paid else "tiktok"
        if "google" in s or gc:
            return "google_ad" if (is_paid or gc) else "google"
        if "instagram" in s or "insta" in s or s == "ig":
            return "instagram_ad" if is_paid else "instagram"
        if "facebook" in s or "fb" in s or "meta" in s or fb:
            return "facebook_ad" if (is_paid or fb) else "facebook"
        if "referral" in s or "referral" in m:
            return "referral"
        if "email" in s or m == "email":
            return "email"
        if s or m:
            return "other"
        return "direct"

    origin_by_email = {}
    for em, src, med, fb, gc, tt in amb_origin_rows:
        if em:
            origin_by_email[em] = _utm_to_origin(src, med, fb, gc, tt)

    for em, evts in by_email.items():
        tk = _email_to_bucket(evts)
        ok = origin_by_email.get(em, "direct")
        if ok in matrix and tk in matrix[ok]:
            matrix[ok][tk] += 1

    # Cold leads are ambassadors NOT in by_email — but for the matrix we
    # only show buckets with activity. Skip injecting cold fillers.

    matrix_rows = []
    for ok, label in SOURCE_BUCKETS:
        total = sum(matrix[ok].values())
        if total > 0:
            matrix_rows.append({
                "origin_key": ok,
                "origin_label": label,
                "total": total,
                "by_temp": matrix[ok],
                "hot_pct": round(100 * (matrix[ok]["hot"] + matrix[ok]["burning"]) / total, 1) if total else 0,
            })
    matrix_rows.sort(key=lambda r: -r["total"])

    # ── Top countries (SQL-driven) ──
    from app.services.phone import lookup_country
    country_total_rows = _safe(
        lambda: db.session.query(Ambassador.country_code, func.count(Ambassador.id))
            .filter(Ambassador.country_code.isnot(None))
            .group_by(Ambassador.country_code)
            .order_by(func.count(Ambassador.id).desc())
            .limit(10).all(),
        [],
    )
    # For each country, count distinct emails who reached >=25% of class 1
    class1_emails = {em.lower() for em in by_email if any(
        f"class1_{x}" in by_email[em.lower()]
        for x in ("viewed", "progress_25", "progress_50", "progress_75", "progress_95", "completed")
    )} if by_email else set()
    hot_emails = {em for em, evts in by_email.items() if _email_to_bucket(evts) in ("hot", "burning")}
    # Map ambassador emails to country
    amb_country_email = _safe(
        lambda: db.session.query(func.lower(Ambassador.email), Ambassador.country_code).all(),
        [],
    )
    country_class1 = defaultdict(int)
    country_hot = defaultdict(int)
    for em, cc in amb_country_email:
        if not cc or not em:
            continue
        if em in class1_emails:
            country_class1[cc] += 1
        if em in hot_emails:
            country_hot[cc] += 1

    top_countries = []
    for cc, total in country_total_rows:
        name, flag = lookup_country(cc)
        top_countries.append({
            "code": cc, "name": name or cc, "flag": flag,
            "total": total,
            "hot": country_hot.get(cc, 0),
            "hot_pct": round(100 * country_hot.get(cc, 0) / total, 1) if total else 0,
            "class1": country_class1.get(cc, 0),
            "class1_pct": round(100 * country_class1.get(cc, 0) / total, 1) if total else 0,
        })

    # ── Top referrers (from ref_counts dict, fast) ──
    top_ref_ids = sorted(ref_counts.items(), key=lambda kv: -kv[1])[:10]
    top_ref_amb_ids = [aid for aid, _ in top_ref_ids if aid]
    top_ref_ambs_map = {}
    if top_ref_amb_ids:
        # joinedload so the template's amb.referral_count (len of referrals)
        # doesn't fire N+1 across the 10 rows.
        for a in (
            Ambassador.query
            .options(joinedload(Ambassador.referrals))
            .filter(Ambassador.id.in_(top_ref_amb_ids))
            .all()
        ):
            top_ref_ambs_map[a.id] = a
    top_referrers = []
    for aid, cnt in top_ref_ids:
        a = top_ref_ambs_map.get(aid)
        if a is None:
            continue
        # Build a minimal temp dict just for the rendering (template
        # expects bucket + color); approximate from event signals.
        em = (a.email or "").lower()
        evts = by_email.get(em, set())
        bk = _email_to_bucket(evts)
        bucket_meta = {
            "burning": ("🔥 BURNING", "#DC2626"),
            "hot": ("🚀 HOT", "#F97316"),
            "warm": ("🌡 WARM", "#FFC857"),
            "cool": ("❄ COOL", "#60A5FA"),
            "cold": ("🧊 COLD", "#6B7280"),
            "customer": ("💎 CUSTOMER", "#A78BFA"),
        }
        bucket_label, color = bucket_meta.get(bk, ("🧊 COLD", "#6B7280"))
        t = {"bucket": bucket_label, "color": color, "score": 0,
             "signals": [], "max_pct": {1: 0, 2: 0, 3: 0}}
        # Patch in the ambassador's referral count directly
        a.referral_count_cached = cnt  # for template
        top_referrers.append((a, t))

    # ── Activity time-series (last 48h, hourly) ──
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    timestamp_rows = _safe(
        lambda: db.session.query(LeadEvent.created_at)
            .filter(LeadEvent.created_at >= cutoff).all(),
        [],
    )
    activity_by_hour = defaultdict(int)
    for (ts,) in timestamp_rows:
        if ts:
            hour_key = ts.replace(minute=0, second=0, microsecond=0)
            activity_by_hour[hour_key] += 1
    series = []
    now_h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h in range(48, 0, -1):
        ts = now_h - timedelta(hours=h - 1)
        series.append({"label": ts.strftime("%H:00"), "count": activity_by_hour.get(ts, 0)})

    # ── Action Queue: hot/warm/burning leads with phone, not contacted recently ──
    # PERF: pre-filter via SQL — only ambassadors with hot/warm event
    # activity AND a phone number. Then score those (typically <100).
    contacted_ids = _safe(
        lambda: {
            n.ambassador_id for n in
            LeadNote.query.filter(
                LeadNote.type.in_(["whatsapp_sent", "email_sent"]),
                LeadNote.created_at >= datetime.now(timezone.utc) - timedelta(hours=48),
            ).all()
        },
        set(),
    )
    # Find candidate ambassadors: have phone, have at least one warm+ event
    warm_event_keys = [
        f"class{n}_progress_{p}" for n in (1, 2, 3) for p in (50, 75, 95)
    ] + [f"class{n}_completed" for n in (1, 2, 3)] + ["webinar_joined", "purchase_completed"]
    candidate_emails = _safe(
        lambda: {em.lower() for (em,) in db.session.query(LeadEvent.email)
                 .filter(LeadEvent.event_type.in_(warm_event_keys))
                 .distinct().all() if em},
        set(),
    )
    candidate_ambs = []
    if candidate_emails:
        candidate_ambs = _safe(
            lambda: Ambassador.query
                .filter(func.lower(Ambassador.email).in_(candidate_emails))
                .filter(Ambassador.phone_number.isnot(None))
                .filter(~Ambassador.id.in_(contacted_ids) if contacted_ids else True)
                .limit(100).all(),
            [],
        )

    # Score the candidates only (typically <100 → fast)
    cand_ids = [a.id for a in candidate_ambs]
    cand_lead_evts, cand_email_evts = (
        fetch_signals_bulk(cand_ids, max_ids=None) if cand_ids else ({}, {})
    )
    # Bulk webinar duration + reservation paid for candidates. Each helper
    # returns (by_amb_id, by_email_lower) so we cover both pathways
    # (Lovable-tracked emails + Zoom guest rematches).
    from app.services.temperature import bulk_webinar_durations as _bulk_dur, bulk_paid_reservations as _bulk_paid
    cand_dur_amb, cand_dur_em = _bulk_dur(candidate_ambs)
    cand_paid_amb, cand_paid_em = _bulk_paid(candidate_ambs)
    scored_candidates = []
    for a in candidate_ambs:
        em_lower = (a.email or "").lower()
        webinar_dur = cand_dur_amb.get(a.id) or (cand_dur_em.get(em_lower) if em_lower else None)
        has_paid = (a.id in cand_paid_amb) or (em_lower and em_lower in cand_paid_em)
        t = compute_temperature(
            a,
            lead_events=cand_lead_evts.get(a.id, []),
            email_events=cand_email_evts.get(a.id, []),
            referral_count=ref_counts.get(a.id, 0),
            webinar_duration_min=webinar_dur,
            has_paid_reservation=has_paid,
        )
        scored_candidates.append((a, t))

    action_queue = []
    for a, t in sorted(scored_candidates, key=lambda at: -at[1]["score"]):
        if a.id in contacted_ids:
            continue
        if temp_label_to_key(t["bucket"]) not in ("hot", "burning", "warm"):
            continue
        if not a.phone_number:
            continue
        action_queue.append((a, t))
        if len(action_queue) >= 20:
            break
    # Pre-compute WA links for action queue
    from urllib.parse import quote
    from app.services.temperature import build_whatsapp_message
    for a, t in action_queue:
        msg = build_whatsapp_message(a, t)
        t["wa_msg_url"] = quote(msg, safe="")

    # Precompute rgba(r,g,b,0.15) strings per temperature bucket — Jinja
    # can't do hex→rgb conversion inline. Used for matrix cell backgrounds.
    def _hex_to_rgb_tint(hex_color, alpha=0.15):
        hex_color = hex_color.lstrip("#")
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
        return f"rgba({r}, {g}, {b}, {alpha})"

    temp_bucket_meta = {}
    for k, l, c in TEMP_BUCKETS:
        temp_bucket_meta[k] = {
            "label": l, "color": c, "tint": _hex_to_rgb_tint(c, 0.15),
        }

    return render_template(
        "admin_leads_insights.html",
        total_leads=total_leads,
        n_hot_or_burning=n_hot_or_burning,
        n_customers=n_customers,
        pct_hot_burning=pct_hot_burning,
        pct_customers=pct_customers,
        funnel_steps=funnel_steps,
        matrix_rows=matrix_rows,
        temp_keys=temp_keys,
        temp_buckets=temp_bucket_meta,
        top_countries=top_countries,
        top_referrers=top_referrers,
        activity_series=series,
        action_queue=action_queue,
        active_section="insights",
        **_admin_layout_context(),
    )


@admin_bp.route("/leads")
def leads():
    """Filterable list of leads with temperature scoring + class progress.

    Filters via query params:
      q          — substring search on name/email/phone
      source     — public | community | ghl_import (DB source field)
      origin     — instagram | facebook_ad | google | referral | direct | ... (UTM bucket)
      tag        — must contain this tag in ghl_tags
      temp       — cold | cool | warm | hot | burning | customer
      has_phone  — 1 to require phone
      class_1    — 1 to require ≥25% watched of class 1 (same for class_2)
      page       — pagination, 1-indexed
    """
    from app.services.temperature import (
        compute_temperature, fetch_signals_bulk, build_whatsapp_message,
        classify_source, SOURCE_BUCKETS, TEMP_BUCKETS, temp_label_to_key,
        SEGMENT_LABELS,
    )
    from app.services.ghl import RELEVANT_LEAD_TAGS
    from urllib.parse import quote

    q          = (request.args.get("q") or "").strip().lower()
    source     = (request.args.get("source") or "").strip()
    origin     = (request.args.get("origin") or "").strip()
    tag_filter = (request.args.get("tag") or "").strip()
    temp_bucket= (request.args.get("temp") or "").strip().lower()
    dance      = (request.args.get("dance") or "").strip()  # dance_level filter
    has_phone  = request.args.get("has_phone") == "1"
    class_1    = request.args.get("class_1") == "1"
    class_2    = request.args.get("class_2") == "1"
    class_3    = request.args.get("class_3") == "1"
    webinar_joined_filter = request.args.get("webinar") == "1"
    not_contacted_filter = request.args.get("not_contacted") == "1"
    seg        = (request.args.get("seg") or "").strip().lower()
    if seg and seg not in SEGMENT_LABELS:
        seg = ""  # ignore unknown segment names
    # Default sort = hottest first. The user wants opening /admin/leads to
    # immediately surface high-temperature leads for outreach without an
    # extra click. Pass ?sort=recent to fall back to recency-sorted view.
    sort_mode  = (request.args.get("sort") or "temp").strip().lower()  # "temp" (default) | "temp_asc" | "recent"
    page       = max(1, request.args.get("page", default=1, type=int))
    per_page   = 50

    # ── DB-level filters first (cheap) ──
    base = Ambassador.query

    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            func.lower(Ambassador.email).like(like),
            func.lower(Ambassador.name).like(like),
            Ambassador.phone_number.like(like),
        ))
    if source:
        base = base.filter(Ambassador.source == source)
    if tag_filter:
        base = base.filter(Ambassador.ghl_tags.like(f"%{tag_filter}%"))
    if has_phone:
        base = base.filter(Ambassador.phone_number.isnot(None))
    # Dance level filter — match on the substring that uniquely
    # identifies each level (full strings are long, e.g. "I've been
    # dancing 1-2 years and want to improve faster")
    if dance:
        base = base.filter(Ambassador.dance_level.like(f"%{dance}%"))

    # ── Global temperature filter via SQL (resolves before pagination) ──
    # Without this, the temp filter only narrowed the current page slice
    # and missed leads on other pages. Now we resolve the full set of
    # matching emails once and pass it to the base query.
    if temp_bucket:
        target_emails = _emails_in_temp_bucket(temp_bucket)
        if target_emails:
            base = base.filter(func.lower(Ambassador.email).in_(target_emails))
        else:
            # No matches → force empty result (avoids returning unfiltered)
            base = base.filter(Ambassador.id == -1)

    # PERF: per-class filters via SQL EXISTS subquery on lead_events,
    # not Python after loading everyone. The filter requires ≥25% watched
    # (i.e. any progress_25/50/75/95/completed/resource_unlocked event) —
    # `class{n}_viewed` alone (the page-load fire) is intentionally NOT
    # counted because it just means "loaded the page", not "actually
    # watched". This matches the user's mental model: "Class 2" filter
    # should return people who pressed play, not people who only visited.
    from app.models import LeadEvent
    def _add_class_filter(q_, class_n):
        progress_events = [
            f"class{class_n}_progress_25",
            f"class{class_n}_progress_50",
            f"class{class_n}_progress_75",
            f"class{class_n}_progress_95",
            f"class{class_n}_completed",
            f"class{class_n}_resource_unlocked",
        ]
        sub = (
            db.session.query(LeadEvent.email)
            .filter(func.lower(LeadEvent.email) == func.lower(Ambassador.email))
            .filter(LeadEvent.event_type.in_(progress_events))
            .exists()
        )
        return q_.filter(sub)

    if class_1:
        base = _add_class_filter(base, 1)
    if class_2:
        base = _add_class_filter(base, 2)
    if class_3:
        base = _add_class_filter(base, 3)

    # Webinar filter: leads who actually joined the live (webinar_joined event).
    # Match by EITHER email OR ambassador_id — the Zoom import wrote
    # ambassador_id directly when matching by name and left email empty
    # (Zoom guests have no email captured), so an email-only join misses
    # them. The OR catches both pathways.
    if webinar_joined_filter:
        sub = (
            db.session.query(LeadEvent.id)
            .filter(LeadEvent.event_type == "webinar_joined")
            .filter(or_(
                LeadEvent.ambassador_id == Ambassador.id,
                func.lower(LeadEvent.email) == func.lower(Ambassador.email),
            ))
            .exists()
        )
        base = base.filter(sub)

    # Origin filter via UTM column match (approximate, but DB-level)
    if origin in ("instagram", "instagram_ad"):
        base = base.filter(or_(
            func.lower(Ambassador.utm_source).like("%insta%"),
            func.lower(Ambassador.utm_source) == "ig",
        ))
    elif origin in ("facebook", "facebook_ad"):
        base = base.filter(or_(
            func.lower(Ambassador.utm_source).like("%facebook%"),
            func.lower(Ambassador.utm_source).like("%meta%"),
            Ambassador.fbclid.isnot(None),
        ))
    elif origin in ("google", "google_ad"):
        base = base.filter(or_(
            func.lower(Ambassador.utm_source).like("%google%"),
            Ambassador.gclid.isnot(None),
        ))
    elif origin in ("tiktok", "tiktok_ad"):
        base = base.filter(or_(
            func.lower(Ambassador.utm_source).like("%tiktok%"),
            Ambassador.ttclid.isnot(None),
        ))
    elif origin == "direct":
        base = base.filter(
            (Ambassador.utm_source.is_(None) | (Ambassador.utm_source == "")) &
            (Ambassador.fbclid.is_(None)) &
            (Ambassador.gclid.is_(None)) &
            (Ambassador.ttclid.is_(None))
        )
    # 'referral', 'email', 'other' fall through — no DB filter
    # (the user can still see them by source / tag instead)

    # Outreach status filter (?not_contacted=1)
    if not_contacted_filter:
        base = base.filter(Ambassador.last_outreach_at.is_(None))

    # ── Segment filter (?seg=NAME) ──
    # Each segment is a named filter combo that the WA button matches
    # with a specific Spanish template (see _segment_message). Designed
    # for the "segment first, then send" workflow on /admin/leads.
    if seg:
        from app.models import Reservation as _Res
        _paid_res_exists = (
            db.session.query(_Res.id)
            .filter(_Res.paid_at.isnot(None))
            .filter(or_(
                _Res.ambassador_id == Ambassador.id,
                func.lower(_Res.email) == func.lower(Ambassador.email),
            ))
            .exists()
        )
        if seg == "client_community":
            # Community = imported community members (already paid prior
            # programs, not part of the MKOT 3.0 reservation flow).
            base = base.filter(Ambassador.source == "community")
        elif seg == "hot_no_reserve":
            # Burning/hot bucket who haven't put €100 down yet.
            hot_emails = {
                em for em, b in _build_email_buckets().items()
                if b in ("burning", "hot")
            }
            if hot_emails:
                base = base.filter(func.lower(Ambassador.email).in_(hot_emails))
            else:
                base = base.filter(Ambassador.id == -1)
            base = base.filter(~_paid_res_exists)
        elif seg == "watched_no_reserve":
            # Touched any class progress event (≥25% watched), but no
            # paid reservation. The "almost there" segment.
            any_class_progress = [
                f"class{n}_progress_{p}" for n in (1, 2, 3)
                for p in (25, 50, 75, 95)
            ] + [f"class{n}_completed" for n in (1, 2, 3)]
            _watched = (
                db.session.query(LeadEvent.id)
                .filter(func.lower(LeadEvent.email) == func.lower(Ambassador.email))
                .filter(LeadEvent.event_type.in_(any_class_progress))
                .exists()
            )
            base = base.filter(_watched).filter(~_paid_res_exists)
        elif seg == "no_engagement":
            # No LeadEvent at all (signed up, never engaged). No reservation.
            _has_any_event = (
                db.session.query(LeadEvent.id)
                .filter(or_(
                    LeadEvent.ambassador_id == Ambassador.id,
                    func.lower(LeadEvent.email) == func.lower(Ambassador.email),
                ))
                .exists()
            )
            base = base.filter(~_has_any_event).filter(~_paid_res_exists)

    # Sort: default = hottest first (?sort=temp). ?sort=temp_asc reverses.
    # ?sort=recent falls back to dashboard-visit recency (the previous default).
    if sort_mode in ("temp", "temp_asc"):
        # Use the unified bucket helper so sort + temp filter + distribution
        # counters all classify identically (paid reservations promote to
        # burning, name-matched ghosts attribute via ambassador_id, etc.).
        email_to_bucket = _build_email_buckets()
        PRIORITY = {"customer": 6, "burning": 5, "hot": 4, "warm": 3, "cool": 2, "cold": 1}
        emails_by_priority = defaultdict(list)
        for em, bucket in email_to_bucket.items():
            emails_by_priority[PRIORITY.get(bucket, 1)].append(em)

        from sqlalchemy import case as sa_case
        priority_cases = []
        for p in (6, 5, 4, 3, 2):  # cold (1) is the implicit else
            if emails_by_priority[p]:
                priority_cases.append((func.lower(Ambassador.email).in_(emails_by_priority[p]), p))

        if priority_cases:
            priority_expr = sa_case(*priority_cases, else_=1)
            if sort_mode == "temp":
                base = base.order_by(
                    priority_expr.desc(),
                    Ambassador.last_dashboard_visit_at.desc().nullslast(),
                    Ambassador.created_at.desc().nullslast(),
                )
            else:  # temp_asc
                base = base.order_by(
                    priority_expr.asc(),
                    Ambassador.created_at.desc().nullslast(),
                )
        else:
            base = base.order_by(
                Ambassador.last_dashboard_visit_at.desc().nullslast(),
                Ambassador.created_at.desc().nullslast(),
            )
    else:  # "recent" or any other value
        base = base.order_by(
            Ambassador.last_dashboard_visit_at.desc().nullslast(),
            Ambassador.created_at.desc().nullslast(),
        )

    # ── Total count via SQL (cheap), then pull only the current page ──
    total_count = base.count()
    pages = max(1, (total_count + per_page - 1) // per_page)
    page_amb = base.offset((page - 1) * per_page).limit(per_page).all()

    # ref_counts is a single SQL aggregation — used for sorting and
    # passed into compute_temperature so it never touches the lazy
    # property in the hot loop.
    ref_counts = _get_referral_counts()

    # PERF: only fetch events for the page (≤50 IDs), not for everyone.
    page_ids = [a.id for a in page_amb]
    lead_evts_by_id, email_evts_by_id = (
        fetch_signals_bulk(page_ids, max_ids=None) if page_ids else ({}, {})
    )

    # Bulk-resolve webinar duration and paid-reservation status for the
    # page in two single queries — rather than 50 sub-queries from inside
    # compute_temperature. Each helper returns (by_amb_id, by_email_lower)
    # so we catch both pathways: events linked via Lovable (email key)
    # and events linked via Zoom name-rematch (ambassador_id key).
    from app.services.temperature import bulk_webinar_durations, bulk_paid_reservations
    webinar_dur_by_amb, webinar_dur_by_email = bulk_webinar_durations(page_amb)
    paid_amb_ids, paid_emails = bulk_paid_reservations(page_amb)

    now_ts = datetime.now(timezone.utc)
    def _outreach_ago(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((now_ts - dt).total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    rows_with_temp = []
    for a in page_amb:
        em_lower = (a.email or "").lower()
        # Try ambassador_id first (catches name-rematched Zoom guests
        # whose LeadEvent.email is empty), fall back to email match.
        webinar_dur = webinar_dur_by_amb.get(a.id)
        if webinar_dur is None and em_lower:
            webinar_dur = webinar_dur_by_email.get(em_lower)
        has_paid = (a.id in paid_amb_ids) or (em_lower and em_lower in paid_emails)
        t = compute_temperature(
            a,
            lead_events=lead_evts_by_id.get(a.id, []),
            email_events=email_evts_by_id.get(a.id, []),
            referral_count=ref_counts.get(a.id, 0),
            webinar_duration_min=webinar_dur,
            has_paid_reservation=has_paid,
        )
        t["source_info"] = classify_source(a)
        t["outreach_ago"] = _outreach_ago(a.last_outreach_at)
        rows_with_temp.append((a, t))

    # Temperature-bucket filter is already applied at the DB-filter
    # stage (see _emails_in_temp_bucket() upstream). The page slice is
    # already correctly narrowed.

    # PLF counters used to render top cards on /admin/leads. Removed
    # from the template when the funnel bar replaced them — kept here
    # as an empty dict for any legacy template ref. The funnel bar +
    # `funnel_visited` are now the source of truth.
    plf_counters = {}

    # ── Dance-level distribution counters (clickable filter cards) ──
    # Single SQL aggregation. Map each long form-answer string to a
    # short label for display.
    dance_level_short = {
        "I teach": "👨‍🏫 Instructor",
        "I'm just getting started": "🌱 Beginner",
        "1-2 years": "🎯 1-2 years",
        "3+ years": "🥋 3+ years",
    }
    def _short_dance(s):
        if not s:
            return "—"
        for needle, short in dance_level_short.items():
            if needle in s:
                return short
        return s[:40]

    dance_dist_rows = _safe(
        lambda: db.session.query(Ambassador.dance_level, func.count(Ambassador.id))
            .filter(Ambassador.dance_level.isnot(None))
            .group_by(Ambassador.dance_level)
            .all(),
        [],
    )
    dance_dist = []
    for raw, n in sorted(dance_dist_rows, key=lambda x: -x[1]):
        # Use a substring fragment as the filter key (passed to ?dance=)
        filter_key = ""
        for needle in dance_level_short:
            if needle in (raw or ""):
                filter_key = needle
                break
        dance_dist.append({
            "label": _short_dance(raw),
            "count": n,
            "filter_key": filter_key or raw,
            "raw": raw,
        })

    # ── Distributions: lightweight SQL approximations (not from scoring) ──
    # These power the clickable cards at the top. They count distinct
    # ambassadors that match each bucket via raw event types — close
    # enough for the headline counters; per-row temperature in the table
    # remains the precise scored value.
    temp_dist = _safe(_quick_temp_dist_sql, {key: 0 for key, _, _ in TEMP_BUCKETS})
    origin_dist = _safe(_quick_origin_dist_sql, {key: 0 for key, _ in SOURCE_BUCKETS})

    # Pre-compute WhatsApp message URLs (template-friendly)
    # When a segment is active, force its template so every row in the
    # filtered audience gets the same consistent pitch.
    for amb, t in rows_with_temp:
        if amb.phone_number:
            msg = build_whatsapp_message(amb, t, force_segment=seg or None)
            t["wa_msg_url"] = quote(msg, safe="")
        else:
            t["wa_msg_url"] = None

    # ── Top-of-page overall stats ──
    stats_overall = {
        "total":       Ambassador.query.count(),
        "with_phone":  Ambassador.query.filter(Ambassador.phone_number.isnot(None)).count(),
        "ghl_imported":Ambassador.query.filter(Ambassador.source == "ghl_import").count(),
        "community":   Ambassador.query.filter(Ambassador.source == "community").count(),
        "public":      Ambassador.query.filter(Ambassador.source == "public").count(),
    }

    from app.services.phone import lookup_country

    # Build active-filter chips in Python so the template doesn't need to
    # cope with Jinja's lack of dict comprehensions inside **kwargs.
    def _without(key):
        d = {k: v for k, v in request.args.items() if k != key and k != "page"}
        return url_for("admin.leads", **d)

    active_chips = []
    if q:          active_chips.append({"label": f"search: {q}", "url": _without("q")})
    if source:     active_chips.append({"label": f"source: {source}", "url": _without("source")})
    if origin:     active_chips.append({"label": f"origin: {origin}", "url": _without("origin")})
    if tag_filter: active_chips.append({"label": f"tag: {tag_filter}", "url": _without("tag")})
    if temp_bucket:active_chips.append({"label": f"temp: {temp_bucket}", "url": _without("temp")})
    if seg:
        _seg_emoji, _seg_label, _ = SEGMENT_LABELS.get(seg, ("", seg, ""))
        active_chips.append({"label": f"segmento: {_seg_emoji} {_seg_label}", "url": _without("seg")})
    if has_phone:  active_chips.append({"label": "has phone", "url": _without("has_phone")})
    if class_1:    active_chips.append({"label": "watched C1", "url": _without("class_1")})
    if class_2:    active_chips.append({"label": "watched C2", "url": _without("class_2")})
    if webinar_joined_filter:
        active_chips.append({"label": "joined webinar", "url": _without("webinar")})
    if sort_mode == "temp":
        active_chips.append({"label": "sort: 🔥 hottest first", "url": _without("sort")})
    elif sort_mode == "temp_asc":
        active_chips.append({"label": "sort: 🧊 coldest first", "url": _without("sort")})
    if dance:      active_chips.append({"label": f"dance: {dance}", "url": _without("dance")})

    # Launch funnel + 7-day activity sparkline. These are GLOBAL views
    # (not affected by the active filters) — the user wants to see how
    # the launch is performing regardless of which slice they're inspecting.
    grand_total = _safe(lambda: Ambassador.query.count(), 0)
    funnel_data = _safe(
        lambda: _compute_launch_funnel(grand_total),
        {"steps": [], "visited": {1: 0, 2: 0}},
    )
    funnel_steps = funnel_data.get("steps", [])
    funnel_visited = funnel_data.get("visited", {1: 0, 2: 0})
    activity_series = _safe(_compute_7d_activity, {
        "labels": [], "signups": [], "class1": [], "class2": [],
    })

    # ── Outreach KPI strip ──
    # contacted_today: marks made in the last 24h (rolling, not midnight-aligned —
    # the user works late, midnight-reset would feel weird).
    # in_queue: ambassadors classified as burning|hot via _build_email_buckets()
    # who have NOT been contacted yet — the natural daily target.
    one_day_ago = datetime.now(timezone.utc) - timedelta(hours=24)
    contacted_today_count = _safe(
        lambda: Ambassador.query
            .filter(Ambassador.last_outreach_at >= one_day_ago)
            .count(),
        0,
    )
    contacted_total = _safe(
        lambda: Ambassador.query
            .filter(Ambassador.last_outreach_at.isnot(None))
            .count(),
        0,
    )
    # Compute outreach queue size: burning + hot, not yet contacted.
    # Customers are deliberately EXCLUDED — they already purchased; they
    # don't belong in an outreach-to-buy queue. (Post-sale support can
    # use a separate filter if needed later.)
    burning_hot_emails = {
        em for em, b in _build_email_buckets().items()
        if b in ("burning", "hot")
    }
    in_queue_count = 0
    if burning_hot_emails:
        in_queue_count = _safe(
            lambda: Ambassador.query
                .filter(func.lower(Ambassador.email).in_(burning_hot_emails))
                .filter(Ambassador.last_outreach_at.is_(None))
                .filter(Ambassador.unsubscribed_at.is_(None))
                .count(),
            0,
        )
    outreach_stats = {
        "contacted_today": contacted_today_count,
        "contacted_total": contacted_total,
        "in_queue": in_queue_count,
    }

    # ── Segment audience counts ──
    # Quick approximations for the segment cards. Same filter semantics
    # as the seg=NAME branch above; recomputed in one batch so the cards
    # always reflect current state regardless of which seg is active.
    seg_counts = {k: 0 for k in SEGMENT_LABELS}
    try:
        # client_community
        seg_counts["client_community"] = (
            Ambassador.query.filter(Ambassador.source == "community").count()
        )
        # paid-reservation email/id sets (reused across hot/watched/no_eng)
        from app.models import Reservation as _R
        paid_rows = (
            _R.query.filter(_R.paid_at.isnot(None))
            .with_entities(_R.ambassador_id, _R.email).all()
        )
        paid_amb_ids_set = {r[0] for r in paid_rows if r[0]}
        paid_emails_set  = {(r[1] or "").lower() for r in paid_rows if r[1]}

        # hot_no_reserve: burning/hot bucket minus paid
        hot_emails = {
            em for em, b in _build_email_buckets().items()
            if b in ("burning", "hot")
        } - paid_emails_set
        if hot_emails:
            seg_counts["hot_no_reserve"] = (
                Ambassador.query
                .filter(func.lower(Ambassador.email).in_(hot_emails))
                .filter(~Ambassador.id.in_(paid_amb_ids_set) if paid_amb_ids_set else True)
                .count()
            )

        # watched_no_reserve: anyone with ≥25% on any class, no paid res
        any_class_progress = [
            f"class{n}_progress_{p}" for n in (1, 2, 3) for p in (25, 50, 75, 95)
        ] + [f"class{n}_completed" for n in (1, 2, 3)]
        watched_emails = {
            r[0].lower() for r in
            db.session.query(LeadEvent.email)
            .filter(LeadEvent.email.isnot(None))
            .filter(LeadEvent.event_type.in_(any_class_progress))
            .distinct()
            .all() if r[0]
        } - paid_emails_set
        if watched_emails:
            seg_counts["watched_no_reserve"] = (
                Ambassador.query
                .filter(func.lower(Ambassador.email).in_(watched_emails))
                .filter(~Ambassador.id.in_(paid_amb_ids_set) if paid_amb_ids_set else True)
                .count()
            )

        # no_engagement: any ambassador with NO LeadEvent and no paid res
        engaged_amb_ids = {
            r[0] for r in
            db.session.query(LeadEvent.ambassador_id)
            .filter(LeadEvent.ambassador_id.isnot(None))
            .distinct().all() if r[0]
        }
        engaged_emails = {
            r[0].lower() for r in
            db.session.query(LeadEvent.email)
            .filter(LeadEvent.email.isnot(None))
            .distinct().all() if r[0]
        }
        q_ne = Ambassador.query
        if engaged_amb_ids:
            q_ne = q_ne.filter(~Ambassador.id.in_(engaged_amb_ids))
        if engaged_emails:
            q_ne = q_ne.filter(~func.lower(Ambassador.email).in_(engaged_emails))
        if paid_amb_ids_set:
            q_ne = q_ne.filter(~Ambassador.id.in_(paid_amb_ids_set))
        if paid_emails_set:
            q_ne = q_ne.filter(~func.lower(Ambassador.email).in_(paid_emails_set))
        seg_counts["no_engagement"] = q_ne.count()
    except Exception:
        # Cards still render; counts just show 0 on failure.
        pass

    return render_template(
        "admin_leads.html",
        rows=rows_with_temp,
        total_count=total_count,
        stats=stats_overall,
        temp_dist=temp_dist,
        origin_dist=origin_dist,
        temp_buckets=TEMP_BUCKETS,
        source_buckets=SOURCE_BUCKETS,
        page=page,
        pages=pages,
        per_page=per_page,
        # Filter values to repopulate the UI
        f_q=q, f_source=source, f_origin=origin, f_tag=tag_filter,
        f_temp=temp_bucket, f_has_phone=has_phone,
        f_class_1=class_1, f_class_2=class_2, f_class_3=class_3,
        f_webinar=webinar_joined_filter,
        f_not_contacted=not_contacted_filter,
        f_seg=seg,
        f_sort=sort_mode,
        segment_labels=SEGMENT_LABELS,
        seg_counts=seg_counts,
        relevant_tags=sorted(RELEVANT_LEAD_TAGS),
        lookup_country=lookup_country,
        plf_counters=plf_counters,
        funnel_steps=funnel_steps,
        funnel_visited=funnel_visited,
        activity_series=activity_series,
        outreach_stats=outreach_stats,
        dance_dist=dance_dist,
        f_dance=dance,
        short_dance=_short_dance,
        active_chips=active_chips,
        clear_all_url=url_for("admin.leads"),
        active_section="leads",
        now_ts=datetime.now(timezone.utc),
        **_admin_layout_context(),
    )


# ════════════════════════════════════════════════════════════════════
# OUTREACH TRACKING — manual mark-as-contacted per lead
# ════════════════════════════════════════════════════════════════════

@admin_bp.route("/leads/<int:ambassador_id>/mark-contacted", methods=["POST"])
def mark_contacted(ambassador_id):
    """Record a 1:1 outreach attempt on this lead. Called by the small
    channel-icon buttons in each row of /admin/leads. Future: also
    called by the Playwright WhatsApp drafter after a draft is left
    in the input box (so the queue reflects pending sends).
    """
    a = Ambassador.query.get_or_404(ambassador_id)
    channel = (request.form.get("channel") or "whatsapp").strip().lower()
    if channel not in ("whatsapp", "email", "call", "sms"):
        channel = "whatsapp"
    note = (request.form.get("note") or "").strip() or None
    a.last_outreach_at = datetime.now(timezone.utc)
    a.last_outreach_channel = channel
    if note:
        a.last_outreach_notes = note
    db.session.commit()
    # Preserve the user's filters/page when redirecting back.
    return redirect(request.referrer or url_for("admin.leads"))


@admin_bp.route("/leads/<int:ambassador_id>/unmark-contacted", methods=["POST"])
def unmark_contacted(ambassador_id):
    """Undo a contact mark — for fat-finger clicks. Keeps last_outreach_notes
    intact since it has forensic value even after un-marking."""
    a = Ambassador.query.get_or_404(ambassador_id)
    a.last_outreach_at = None
    a.last_outreach_channel = None
    db.session.commit()
    return redirect(request.referrer or url_for("admin.leads"))


def _why_now(amb, t):
    """One-line 'why this lead is in the queue right now' summary.

    Precedence matches build_whatsapp_message branches so the reason
    aligns with the message that will be drafted. Returns a short string
    rendered inline on the queue row.
    """
    has_paid = t.get("has_paid_reservation")
    webinar_dur = t.get("webinar_duration_min")
    max_pct = t.get("max_pct", {})
    recency_bonus = t.get("recency_bonus", 0)
    if has_paid:
        return "Paid €100 — close the loop on form / answer questions"
    if webinar_dur and webinar_dur >= 60:
        return f"Stayed {webinar_dur}m in live — peak emotional intent"
    if webinar_dur and webinar_dur >= 30:
        return f"Watched {webinar_dur}m of live — strong intent, no commit yet"
    completed = [cn for cn, pct in max_pct.items() if pct >= 95]
    if completed:
        return f"Finished class {' + '.join(str(c) for c in completed)} — ready"
    if max_pct.get(3, 0) >= 50:
        return "Watching class 3 (replay) — currently engaged"
    started = [cn for cn, pct in max_pct.items() if pct >= 50]
    if started:
        return f"Started class {' + '.join(str(c) for c in started)} (≥50%)"
    if recency_bonus >= 30:
        return "Active in last 24h — momentum window"
    return "High base score — long-term engaged"


@admin_bp.route("/queue")
def queue():
    """Today's Action Queue — curated top-N ranked by action_score.

    Surfaces who to contact next, hides anyone marked contacted in the
    last 7 days, computes a "why now" reason per row. Designed for a
    solo founder doing 50-80 personal outreaches per day.

    Query params:
        ?limit=80         — max rows (default 80)
        ?include_warm=1   — include warm bucket (default: only burning+hot+customer)
    """
    from app.services.temperature import (
        compute_temperature, fetch_signals_bulk,
        bulk_webinar_durations, bulk_paid_reservations,
        build_whatsapp_message,
    )
    try:
        limit = int(request.args.get("limit") or 80)
    except (TypeError, ValueError):
        limit = 80
    limit = max(10, min(200, limit))
    include_warm = request.args.get("include_warm") == "1"

    # Eligible: opted-in AND (never contacted OR contacted >7d ago).
    # Excludes existing community members — they're already inside the
    # paid community, can't be sold to via this funnel. A community
    # member is anyone imported from Circle (source='community' OR
    # circle_member_id set) OR who self-declared "yes" in the GHL form.
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    yes_values = ("yes", "sí", "si", "true", "1", "y")
    candidates = (
        Ambassador.query
        .filter(Ambassador.unsubscribed_at.is_(None))
        .filter(or_(
            Ambassador.last_outreach_at.is_(None),
            Ambassador.last_outreach_at < seven_days_ago,
        ))
        .filter(or_(Ambassador.source.is_(None), Ambassador.source != "community"))
        .filter(Ambassador.circle_member_id.is_(None))
        .filter(or_(
            Ambassador.is_community_member.is_(None),
            func.lower(func.trim(Ambassador.is_community_member)).notin_(yes_values),
        ))
        .all()
    )

    if not candidates:
        return render_template(
            "admin_queue.html",
            page_title="Action Queue",
            active_section="queue",
            rows=[],
            limit=limit,
            include_warm=include_warm,
            total_eligible=0,
            **_admin_layout_context(),
        )

    # Bulk fetches (same pattern as /admin/leads).
    cand_ids = [a.id for a in candidates]
    lead_evts_by_id, email_evts_by_id = (
        fetch_signals_bulk(cand_ids, max_ids=None) if cand_ids else ({}, {})
    )
    ref_counts = _get_referral_counts()
    dur_by_amb, dur_by_em = bulk_webinar_durations(candidates)
    paid_amb_ids, paid_emails = bulk_paid_reservations(candidates)

    # Score each candidate; only keep burning/hot/customer (and warm if asked).
    scored = []
    target_buckets = ("burning", "hot", "customer")
    if include_warm:
        target_buckets = target_buckets + ("warm",)
    for a in candidates:
        em_lower = (a.email or "").lower()
        webinar_dur = dur_by_amb.get(a.id) or (dur_by_em.get(em_lower) if em_lower else None)
        has_paid = (a.id in paid_amb_ids) or (em_lower and em_lower in paid_emails)
        t = compute_temperature(
            a,
            lead_events=lead_evts_by_id.get(a.id, []),
            email_events=email_evts_by_id.get(a.id, []),
            referral_count=ref_counts.get(a.id, 0),
            webinar_duration_min=webinar_dur,
            has_paid_reservation=has_paid,
        )
        if t["bucket_key"] not in target_buckets:
            continue
        scored.append((a, t))

    # Sort by action_score (which already includes recency bonus). Tie-break
    # by last_activity_at desc so the freshest active leads bubble first.
    def _sort_key(at):
        a, t = at
        last = t.get("last_activity_at")
        last_ts = last.timestamp() if last else 0
        return (-t["score"], -last_ts)
    scored.sort(key=_sort_key)
    top = scored[:limit]

    # Build display rows with WA URL + why-now + recency string.
    from urllib.parse import quote
    now_ts = datetime.now(timezone.utc)
    def _ago(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        s = int((now_ts - dt).total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"

    rows = []
    for a, t in top:
        msg = build_whatsapp_message(a, t)
        wa_url = (
            f"https://wa.me/{a.phone_number.replace('+', '')}?text={quote(msg)}"
            if a.phone_number else None
        )
        rows.append({
            "amb": a,
            "t": t,
            "why_now": _why_now(a, t),
            "wa_url": wa_url,
            "wa_msg": msg,
            "active_ago": _ago(t.get("last_activity_at")),
        })

    return render_template(
        "admin_queue.html",
        page_title="Action Queue",
        active_section="queue",
        rows=rows,
        limit=limit,
        include_warm=include_warm,
        total_eligible=len(scored),
        **_admin_layout_context(),
    )


@admin_bp.route("/leads/<int:ambassador_id>/open-wa", methods=["POST"])
def open_wa(ambassador_id):
    """One-click contextual WhatsApp opener.

    JSON-friendly: returns 200 with {ok:true, wa_url, message_preview}.
    The browser-side JS opens the wa_url in a new tab AND records the
    contact in one user click. The DOM is reloaded by the caller.

    Marks contacted with channel=whatsapp and stores the drafted message
    in last_outreach_notes (truncated to 1KB) for audit.
    """
    from flask import jsonify
    from app.services.temperature import (
        compute_temperature, fetch_signals_bulk,
        bulk_webinar_durations, bulk_paid_reservations,
        build_whatsapp_message,
    )
    from urllib.parse import quote
    a = Ambassador.query.get_or_404(ambassador_id)
    if not a.phone_number:
        return jsonify({"ok": False, "error": "no_phone"}), 400

    # Recompute temperature for the freshest contextual message.
    lead_evts, email_evts = fetch_signals_bulk([a.id], max_ids=None)
    ref_count = _get_referral_counts().get(a.id, 0)
    dur_by_amb, dur_by_em = bulk_webinar_durations([a])
    paid_amb_ids, paid_emails = bulk_paid_reservations([a])
    em_lower = (a.email or "").lower()
    webinar_dur = dur_by_amb.get(a.id) or (dur_by_em.get(em_lower) if em_lower else None)
    has_paid = (a.id in paid_amb_ids) or (em_lower and em_lower in paid_emails)
    t = compute_temperature(
        a,
        lead_events=lead_evts.get(a.id, []),
        email_events=email_evts.get(a.id, []),
        referral_count=ref_count,
        webinar_duration_min=webinar_dur,
        has_paid_reservation=has_paid,
    )
    msg = build_whatsapp_message(a, t)
    wa_url = f"https://wa.me/{a.phone_number.replace('+', '')}?text={quote(msg)}"

    # Mark contacted (truncate notes to ~1KB to stay safely under TEXT limits).
    a.last_outreach_at = datetime.now(timezone.utc)
    a.last_outreach_channel = "whatsapp"
    a.last_outreach_notes = msg[:1000]
    db.session.commit()

    return jsonify({
        "ok": True,
        "wa_url": wa_url,
        "message_preview": msg[:200],
    })


@admin_bp.route("/leads/ghosts")
def leads_ghosts():
    """List people watching class videos with emails that don't match
    any Ambassador. They show up here so the admin can outreach to them
    even though they never registered via /community or /join.

    Filters: q (email substring), temp (bucket), class_1, class_2,
    webinar, sort. Same UX as /admin/leads (filter chips, pagination,
    temperature sort).
    """
    q          = (request.args.get("q") or "").strip().lower()
    temp_bucket= (request.args.get("temp") or "").strip().lower()
    class_1    = request.args.get("class_1") == "1"
    class_2    = request.args.get("class_2") == "1"
    class_3    = request.args.get("class_3") == "1"
    webinar    = request.args.get("webinar") == "1"
    # Default sort = hottest first, matching /admin/leads.
    sort_mode  = (request.args.get("sort") or "temp").strip().lower()
    page       = max(1, request.args.get("page", default=1, type=int))
    per_page   = 50

    ghosts = _safe(_compute_ghost_summary, [])

    # ── Apply filters in Python (small dataset relative to Ambassadors) ──
    if q:
        ghosts = [g for g in ghosts if q in (g["email"] or "")]
    if temp_bucket:
        ghosts = [g for g in ghosts if g["bucket_key"] == temp_bucket]
    if class_1:
        ghosts = [g for g in ghosts if g["class1_max"] >= 25]
    if class_2:
        ghosts = [g for g in ghosts if g["class2_max"] >= 25]
    if class_3:
        ghosts = [g for g in ghosts if g.get("class3_max", 0) >= 25]
    if webinar:
        ghosts = [g for g in ghosts if g["webinar_joined"]]

    # Stats (computed AFTER filters so cards reflect current view's denominator;
    # but we want the GLOBAL totals for context too)
    all_ghosts = _safe(_compute_ghost_summary, [])  # cheap; cached at SQL level
    stats = {
        "total":       len(all_ghosts),
        "class1":      sum(1 for g in all_ghosts if g["class1_max"] >= 25),
        "class2":      sum(1 for g in all_ghosts if g["class2_max"] >= 25),
        "class3":      sum(1 for g in all_ghosts if g.get("class3_max", 0) >= 25),
        "webinar":     sum(1 for g in all_ghosts if g["webinar_joined"]),
    }

    # Temp-bucket distribution (for the clickable cards)
    PRIORITY = {"customer": 6, "burning": 5, "hot": 4, "warm": 3, "cool": 2, "cold": 1}
    temp_dist = {k: 0 for k in PRIORITY}
    for g in all_ghosts:
        temp_dist[g["bucket_key"]] = temp_dist.get(g["bucket_key"], 0) + 1

    # ── Sort ──
    if sort_mode == "temp":
        ghosts.sort(key=lambda g: (-PRIORITY.get(g["bucket_key"], 0),
                                    -(g["last_seen"].timestamp() if g["last_seen"] else 0)))
    elif sort_mode == "temp_asc":
        ghosts.sort(key=lambda g: (PRIORITY.get(g["bucket_key"], 0),
                                   -(g["last_seen"].timestamp() if g["last_seen"] else 0)))
    else:
        # Default: most recent activity first
        ghosts.sort(key=lambda g: g["last_seen"] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)

    # ── Pagination ──
    total_count = len(ghosts)
    pages = max(1, (total_count + per_page - 1) // per_page)
    page_ghosts = ghosts[(page - 1) * per_page : page * per_page]

    # ── Active chips ──
    def _without(key):
        d = {k: v for k, v in request.args.items() if k != key and k != "page"}
        return url_for("admin.leads_ghosts", **d)

    active_chips = []
    if q:           active_chips.append({"label": f"search: {q}", "url": _without("q")})
    if temp_bucket: active_chips.append({"label": f"temp: {temp_bucket}", "url": _without("temp")})
    if class_1:     active_chips.append({"label": "watched C1", "url": _without("class_1")})
    if class_2:     active_chips.append({"label": "watched C2", "url": _without("class_2")})
    if class_3:     active_chips.append({"label": "watched C3", "url": _without("class_3")})
    if webinar:     active_chips.append({"label": "joined webinar", "url": _without("webinar")})
    if sort_mode == "temp":
        active_chips.append({"label": "sort: 🔥 hottest first", "url": _without("sort")})
    elif sort_mode == "temp_asc":
        active_chips.append({"label": "sort: 🧊 coldest first", "url": _without("sort")})

    from app.services.temperature import BUCKET_LABELS, TEMP_BUCKETS

    return render_template(
        "admin_leads_ghosts.html",
        page_title="Ghost Leads",
        active_section="ghosts",
        rows=page_ghosts,
        total_count=total_count,
        stats=stats,
        temp_dist=temp_dist,
        temp_buckets=TEMP_BUCKETS,
        bucket_labels=BUCKET_LABELS,
        page=page,
        pages=pages,
        per_page=per_page,
        f_q=q, f_temp=temp_bucket, f_class_1=class_1, f_class_2=class_2, f_class_3=class_3,
        f_webinar=webinar, f_sort=sort_mode,
        active_chips=active_chips,
        clear_all_url=url_for("admin.leads_ghosts"),
        now_ts=datetime.now(timezone.utc),
        **_admin_layout_context(),
    )


@admin_bp.route("/leads/ghosts/convert", methods=["POST"])
def convert_ghost_to_ambassador():
    """Promote a ghost (LeadEvent rows with ambassador_id=NULL) into a
    real Ambassador. Backfills UTMs/ref from the most recent event and
    relinks all matching LeadEvents to the new ambassador.

    Doesn't send a welcome email — the admin reviews + sends manually
    via /admin/test-email or /admin/emails after triggering convert.
    """
    from app.services.signup import _generate_unique_code

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("admin.leads_ghosts"))

    # Idempotent: if an Ambassador with this email already exists, just relink events.
    existing = Ambassador.query.filter(func.lower(Ambassador.email) == email).first()
    if existing:
        relinked = (
            db.session.query(LeadEvent)
            .filter(func.lower(LeadEvent.email) == email)
            .filter(LeadEvent.ambassador_id.is_(None))
            .update({"ambassador_id": existing.id}, synchronize_session=False)
        )
        db.session.commit()
        flash(f"Already an Ambassador. Relinked {relinked} event(s) to {existing.name or email}.", "info")
        return redirect(url_for("admin.leads_ghosts"))

    # Pull the most recent LeadEvent for this email to backfill UTMs / ref.
    last_evt = (
        LeadEvent.query
        .filter(func.lower(LeadEvent.email) == email)
        .order_by(LeadEvent.created_at.desc())
        .first()
    )

    # Use the email's local-part as a placeholder name. Admin can edit later.
    placeholder_name = email.split("@", 1)[0] or "Ghost"

    new_amb = Ambassador(
        name=placeholder_name[:80],
        email=email,
        referral_code=_generate_unique_code(),
        dashboard_code=_generate_unique_code(),
        source="video_only",
        utm_source=getattr(last_evt, "utm_source", None) if last_evt else None,
        utm_medium=getattr(last_evt, "utm_medium", None) if last_evt else None,
        utm_campaign=getattr(last_evt, "utm_campaign", None) if last_evt else None,
        utm_content=getattr(last_evt, "utm_content", None) if last_evt else None,
        utm_term=getattr(last_evt, "utm_term", None) if last_evt else None,
        fbclid=getattr(last_evt, "fbclid", None) if last_evt else None,
        gclid=getattr(last_evt, "gclid", None) if last_evt else None,
        ttclid=getattr(last_evt, "ttclid", None) if last_evt else None,
    )
    try:
        db.session.add(new_amb)
        db.session.flush()  # get the id without commit
        relinked = (
            db.session.query(LeadEvent)
            .filter(func.lower(LeadEvent.email) == email)
            .filter(LeadEvent.ambassador_id.is_(None))
            .update({"ambassador_id": new_amb.id}, synchronize_session=False)
        )
        db.session.commit()
        flash(
            f"Converted {email} to Ambassador. Relinked {relinked} event(s). "
            f"Edit name/phone in their detail page if you want.",
            "success",
        )
    except Exception:
        db.session.rollback()
        logger.exception("convert ghost to ambassador failed for %s", email)
        flash(f"Failed to convert {email}. Check logs.", "error")

    return redirect(url_for("admin.leads_ghosts"))


@admin_bp.route("/network")
def network():
    """Visualize the referral graph: who referred whom, recursively.

    D3 force-directed graph rendered on the client. The backend just
    computes the node/link/stats payload via _compute_referral_network().
    """
    data = _safe(_compute_referral_network, {
        "nodes": [], "links": [], "top_viral": [],
        "stats": {
            "total_trees": 0, "deepest_chain": 0, "biggest_tree_size": 0,
            "biggest_tree_root": "—", "conversion_rate_pct": 0,
            "total_referrals": 0, "total_orphans": 0, "total_ambassadors": 0,
        },
    })
    return render_template(
        "admin_network.html",
        page_title="Referral Network",
        active_section="network",
        network_data=data,
        stats=data["stats"],
        top_viral=data["top_viral"],
        **_admin_layout_context(),
    )


@admin_bp.route("/leads-debug")
def leads_debug():
    """Quick live view of LeadEvent rows arriving from /api/lead-event.
    Auto-refreshes every 5s. Filter by ?email=xxx if needed.

    This is the minimum-viable visibility for the launch — the full leads
    dashboard (filters, temperature, WhatsApp button, notes) ships post-7-may.
    """
    email_filter = (request.args.get("email") or "").strip().lower()
    event_filter = (request.args.get("event") or "").strip()

    q = LeadEvent.query
    if email_filter:
        q = q.filter(func.lower(LeadEvent.email) == email_filter)
    if event_filter:
        q = q.filter(LeadEvent.event_type == event_filter)
    events = q.order_by(LeadEvent.created_at.desc()).limit(200).all()

    total_events = LeadEvent.query.count()
    distinct_emails = db.session.query(func.count(func.distinct(LeadEvent.email))).scalar() or 0
    linked = LeadEvent.query.filter(LeadEvent.ambassador_id.isnot(None)).count()
    ghost = total_events - linked

    by_event = {}
    rows = (
        db.session.query(LeadEvent.event_type, func.count(LeadEvent.id))
        .group_by(LeadEvent.event_type)
        .all()
    )
    for et, c in rows:
        by_event[et] = c

    # Resolve ambassador names for the displayed events.
    amb_ids = {e.ambassador_id for e in events if e.ambassador_id}
    amb_by_id = {}
    if amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(amb_ids)).all():
            amb_by_id[a.id] = a

    # ── Per-email summary (max % watched per class) ─────────────────────
    # Pull a wider window so the summary is meaningful even if the latest
    # 200 raw events are dominated by one noisy user.
    summary_window = (
        LeadEvent.query.order_by(LeadEvent.created_at.desc()).limit(2000).all()
    )

    def _pct_from_event(e):
        """Best-known progress % implied by this single event."""
        if e.pct is not None:
            return int(e.pct)
        et = (e.event_type or "")
        if et.endswith("_completed"):
            return 100
        if et.endswith("_resource_unlocked"):
            return 95
        if et.endswith("_progress_95"):
            return 95
        if et.endswith("_progress_75"):
            return 75
        if et.endswith("_progress_50"):
            return 50
        if et.endswith("_progress_25"):
            return 25
        return 0

    summary_by_email = {}
    for e in summary_window:
        em = (e.email or "").lower()
        if not em:
            continue
        s = summary_by_email.setdefault(em, {
            "email": em,
            "ambassador_id": e.ambassador_id,
            "first_seen": e.created_at,
            "last_seen": e.created_at,
            "event_count": 0,
            "class_max": {1: 0, 2: 0},
        })
        s["event_count"] += 1
        if e.created_at and (s["first_seen"] is None or e.created_at < s["first_seen"]):
            s["first_seen"] = e.created_at
        if e.created_at and (s["last_seen"] is None or e.created_at > s["last_seen"]):
            s["last_seen"] = e.created_at
        cn = e.class_number
        if cn in (1, 2):
            p = _pct_from_event(e)
            if p > s["class_max"][cn]:
                s["class_max"][cn] = p
        # Backfill ambassador_id if a later event has it.
        if s["ambassador_id"] is None and e.ambassador_id:
            s["ambassador_id"] = e.ambassador_id

    # Resolve ambassador names for the summary table too.
    summary_amb_ids = {s["ambassador_id"] for s in summary_by_email.values() if s["ambassador_id"]}
    if summary_amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(summary_amb_ids)).all():
            amb_by_id[a.id] = a

    # Sort summary: most recent activity first.
    summary_sorted = sorted(
        summary_by_email.values(),
        key=lambda s: s["last_seen"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:50]

    def _pct_cell(p):
        if p >= 95:
            color = "#2EDB99"
        elif p >= 50:
            color = "#FFC857"
        elif p > 0:
            color = "#C9CFD4"
        else:
            color = "#6B7280"
        label = f"{p}%" if p > 0 else "—"
        return f'<span style="color:{color}; font-weight:bold;">{label}</span>'

    summary_rows_html = []
    for s in summary_sorted:
        amb = amb_by_id.get(s["ambassador_id"])
        amb_label = (
            f'<a href="/admin/ambassador/{amb.id}" style="color:#2EDB99;">{amb.name}</a>'
            if amb else '<span style="color:#9CA3AF;">ghost</span>'
        )
        summary_rows_html.append(f"""
        <tr>
          <td style="padding:6px 10px; color:#FFFFFF;">{s["email"]}</td>
          <td style="padding:6px 10px;">{amb_label}</td>
          <td style="padding:6px 10px; text-align:center;">{_pct_cell(s["class_max"][1])}</td>
          <td style="padding:6px 10px; text-align:center;">{_pct_cell(s["class_max"][2])}</td>
          <td style="padding:6px 10px; text-align:center; color:#C9CFD4;">{s["event_count"]}</td>
          <td style="padding:6px 10px; color:#9CA3AF; font-size:11px;">{s["last_seen"].strftime('%m-%d %H:%M:%S') if s["last_seen"] else '—'}</td>
        </tr>""")

    # ── Raw event log ───────────────────────────────────────────────────
    rows_html = []
    for e in events:
        amb = amb_by_id.get(e.ambassador_id)
        amb_label = (
            f'<a href="/admin/ambassador/{amb.id}" style="color:#2EDB99;">{amb.name}</a>'
            if amb else '<span style="color:#9CA3AF;">— ghost —</span>'
        )
        attribution_bits = []
        for label, val in (("src", e.utm_source), ("camp", e.utm_campaign), ("ref", e.ref)):
            if val:
                attribution_bits.append(f"{label}={val}")
        attribution = " · ".join(attribution_bits) or "—"
        progress = (
            f"{e.pct}% ({e.current_time_sec}s/{e.duration_sec}s)"
            if e.pct is not None else "—"
        )
        rows_html.append(f"""
        <tr>
          <td style="padding:6px 10px; color:#9CA3AF; font-size:11px;">{e.created_at.strftime('%m-%d %H:%M:%S')}</td>
          <td style="padding:6px 10px; color:#FFC857;">{e.event_type}</td>
          <td style="padding:6px 10px; color:#FFFFFF;">{e.email or '—'}</td>
          <td style="padding:6px 10px;">{amb_label}</td>
          <td style="padding:6px 10px; color:#C9CFD4;">{progress}</td>
          <td style="padding:6px 10px; color:#9CA3AF; font-size:11px;">{attribution}</td>
        </tr>""")

    by_event_html = " · ".join(
        f'<span style="color:#FFC857;">{c}</span> {et}' for et, c in sorted(by_event.items())
    ) or "<span style='color:#9CA3AF;'>(none yet)</span>"

    html = f"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="5">
<title>Leads Debug · MetaKizz</title>
<style>
 body {{ background:#000; color:#fff; font-family:'Share Tech Mono','Courier New',monospace; padding:20px; }}
 h1 {{ color:#2EDB99; font-size:18px; letter-spacing:2px; text-transform:uppercase; margin:0 0 8px 0; }}
 .stats {{ font-size:13px; color:#C9CFD4; margin-bottom:16px; }}
 .stats strong {{ color:#2EDB99; }}
 .filter {{ margin-bottom:14px; font-size:12px; }}
 .filter input {{ background:#0a0f0a; border:1px solid rgba(46,219,153,0.3); color:#fff; padding:6px 10px; font-family:inherit; }}
 .filter button {{ background:#2EDB99; color:#000; border:0; padding:6px 14px; cursor:pointer; font-weight:bold; margin-left:6px; }}
 table {{ width:100%; border-collapse:collapse; font-size:12px; }}
 th {{ text-align:left; padding:8px 10px; color:#2EDB99; font-size:10px; letter-spacing:1.5px; text-transform:uppercase; border-bottom:1px solid rgba(46,219,153,0.3); }}
 tr {{ border-bottom:1px solid rgba(255,255,255,0.05); }}
 .meta {{ font-size:10px; color:#6B7280; margin-top:14px; }}
</style>
</head><body>
<h1>▌ LEAD EVENTS · LIVE</h1>
<div class="stats">
  Total: <strong>{total_events}</strong> events ·
  Linked: <strong>{linked}</strong> · Ghost: <strong>{ghost}</strong> ·
  Distinct emails: <strong>{distinct_emails}</strong><br>
  By event: {by_event_html}
</div>
<form class="filter" method="get">
  <input type="email" name="email" placeholder="filter by email" value="{email_filter}">
  <input type="text" name="event" placeholder="filter by event_type" value="{event_filter}">
  <button type="submit">Filter</button>
  <a href="/admin/leads-debug" style="color:#9CA3AF; margin-left:10px; font-size:11px;">clear</a>
</form>

<h2 style="color:#2EDB99; font-size:14px; letter-spacing:1.5px; text-transform:uppercase; margin:20px 0 8px 0;">▌ PER-EMAIL SUMMARY · MAX % WATCHED</h2>
<table>
 <thead><tr>
  <th>Email</th><th>Ambassador</th>
  <th style="text-align:center;">Class 1</th>
  <th style="text-align:center;">Class 2</th>
  <th style="text-align:center;">Events</th>
  <th>Last seen (UTC)</th>
 </tr></thead>
 <tbody>{''.join(summary_rows_html) if summary_rows_html else '<tr><td colspan="6" style="padding:20px; text-align:center; color:#9CA3AF;">No leads yet</td></tr>'}</tbody>
</table>

<h2 style="color:#2EDB99; font-size:14px; letter-spacing:1.5px; text-transform:uppercase; margin:24px 0 8px 0;">▌ RAW EVENT LOG · LAST 200</h2>
<table>
 <thead><tr>
  <th>Time (UTC)</th><th>Event</th><th>Email</th><th>Ambassador</th><th>Progress</th><th>Attribution</th>
 </tr></thead>
 <tbody>{''.join(rows_html) if rows_html else '<tr><td colspan="6" style="padding:20px; text-align:center; color:#9CA3AF;">No events yet — submit the email gate on /class1 to test</td></tr>'}</tbody>
</table>
<div class="meta">
  Showing last 200 events · auto-refresh every 5s · server time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}
</div>
</body></html>"""
    return html


@admin_bp.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("home.index"))


# ─── MKOT 3.0 Raffle (live screen-share tool) ─────────────────────

def _get_raffle_state():
    """Return the singleton RaffleState row, creating it on demand."""
    state = RaffleState.query.get(1)
    if state is None:
        state = RaffleState(id=1)
        db.session.add(state)
        db.session.commit()
    return state


def _eligible_reservations(state):
    """Reservations that count for the raffle.

    Eligibility:
      - paid (paid_at IS NOT NULL)
      - form completed (form_completed_at IS NOT NULL)
      - if window is closed, form must have completed BEFORE the close timestamp
    """
    q = Reservation.query.filter(
        Reservation.paid_at.isnot(None),
        Reservation.form_completed_at.isnot(None),
    )
    if state.window_closed_at is not None:
        q = q.filter(Reservation.form_completed_at <= state.window_closed_at)
    return q.order_by(Reservation.form_completed_at.desc()).all()


# ─── MKOT 3.0 pricing matrix (used to project revenue from form choices) ───
# program_choice × modality_choice → full ticket price (EUR).
# When either field is 'not_sure', we use the platform average estimate.
MKOT_PRICING = {
    ("dancers",     "solo"): 997,
    ("dancers",     "duo"):  1247,
    ("instructors", "solo"): 1347,
    ("instructors", "duo"):  1797,
}
MKOT_AVG_ESTIMATE = 1300  # for any "not_sure" combination
MKOT_DEPOSIT_EUR = 100    # what they already paid


def _projected_price_eur(reservation):
    """Best-effort revenue projection from a single Reservation row.

    Returns None if no form has been completed (we don't know yet).
    Any 'not_sure' on either axis falls back to MKOT_AVG_ESTIMATE.
    """
    if not reservation.form_completed_at:
        return None
    program = (reservation.program_choice or "").lower()
    modality = (reservation.modality_choice or "").lower()
    if program == "not_sure" or modality == "not_sure":
        return MKOT_AVG_ESTIMATE
    return MKOT_PRICING.get((program, modality))


def _revenue_breakdown(reservations):
    """Aggregate revenue stats across a list of Reservation rows.

    Returns a dict ready to render: per-bucket counts + per-bucket revenue,
    grand totals (estimated revenue, deposits collected, outstanding).
    """
    buckets = [
        ("dancers_solo",     "Dancers · Solo",        997,  "#2EDB99"),
        ("dancers_duo",      "Dancers · Duo",         1247, "#27ba82"),
        ("instructors_solo", "Instructors · Solo",    1347, "#c026d3"),
        ("instructors_duo",  "Instructors · Duo",     1797, "#a21caf"),
        ("not_sure",         "Not sure (avg)",        1300, "#FFC857"),
        ("pending",          "Form pending",          0,    "#6B7280"),
    ]
    counts = {b[0]: 0 for b in buckets}
    revenue = {b[0]: 0 for b in buckets}

    paid_total = 0
    for r in reservations:
        if r.paid_at:
            paid_total += 1
        if not r.form_completed_at:
            counts["pending"] += 1
            continue
        prog = (r.program_choice or "").lower()
        mod = (r.modality_choice or "").lower()
        if prog == "not_sure" or mod == "not_sure":
            key = "not_sure"
            price = MKOT_AVG_ESTIMATE
        elif prog == "dancers" and mod == "solo":
            key, price = "dancers_solo", 997
        elif prog == "dancers" and mod == "duo":
            key, price = "dancers_duo", 1247
        elif prog == "instructors" and mod == "solo":
            key, price = "instructors_solo", 1347
        elif prog == "instructors" and mod == "duo":
            key, price = "instructors_duo", 1797
        else:
            # Unknown combo — should never hit if validation works
            continue
        counts[key] += 1
        revenue[key] += price

    estimated_total = sum(revenue.values())
    deposits_in = paid_total * MKOT_DEPOSIT_EUR
    outstanding = max(0, estimated_total - deposits_in)
    completed = sum(counts[k] for k in counts if k != "pending")
    avg_per_buyer = (estimated_total / completed) if completed > 0 else 0

    return {
        "buckets": buckets,
        "counts": counts,
        "revenue": revenue,
        "estimated_total": estimated_total,
        "deposits_in": deposits_in,
        "outstanding": outstanding,
        "completed": completed,
        "paid_total": paid_total,
        "avg_per_buyer": avg_per_buyer,
    }


@admin_bp.route("/email-preview/reservation-confirmed")
def preview_reservation_email():
    """Renders the reservation confirmation email in the browser, with sample
    data, so we can review the design without sending a real send."""
    sample_first_name = request.args.get("name", "Maria")
    sample_email = request.args.get("email", "maria@example.com")
    sample_amount = request.args.get("amount", "100")
    return render_template(
        "emails/reservation_confirmed.html",
        first_name=sample_first_name,
        email=sample_email,
        amount_eur=sample_amount,
    )


@admin_bp.route("/email-preview/reservation-first50")
def preview_reservation_first50_email():
    """Outreach email for paid reservations we haven't reached on WhatsApp.
    Frames the buyer as 'first 50' and asks them to start a WA chat with us."""
    sample_first_name = request.args.get("name", "Maria")
    sample_email = request.args.get("email", "maria@example.com")
    sample_amount = request.args.get("amount", "100")
    return render_template(
        "emails/reservation_first50.html",
        first_name=sample_first_name,
        email=sample_email,
        amount_eur=sample_amount,
    )


@admin_bp.route("/reservations/<int:reservation_id>/mark-contacted", methods=["POST"])
def reservation_mark_contacted(reservation_id):
    """Mark a Reservation as contacted (sets last_contacted_at to now).
    Idempotent — safe to call multiple times. Optional channel form param."""
    from flask import jsonify
    res = Reservation.query.get_or_404(reservation_id)
    res.last_contacted_at = datetime.now(timezone.utc)
    res.last_contacted_channel = (request.form.get("channel") or "manual")[:20]
    db.session.commit()
    return jsonify({
        "ok": True,
        "last_contacted_at": res.last_contacted_at.isoformat(),
        "last_contacted_channel": res.last_contacted_channel,
    })


@admin_bp.route("/reservations/<int:reservation_id>/unmark-contacted", methods=["POST"])
def reservation_unmark_contacted(reservation_id):
    """Clear contacted state — useful if you marked someone by accident."""
    from flask import jsonify
    res = Reservation.query.get_or_404(reservation_id)
    res.last_contacted_at = None
    res.last_contacted_channel = None
    db.session.commit()
    return jsonify({"ok": True})


@admin_bp.route("/reservations/<int:reservation_id>/save-note", methods=["POST"])
def reservation_save_note(reservation_id):
    """Persist the admin's free-text note for a Reservation. Posted on blur
    by the inline editor on /admin/reservations."""
    from flask import jsonify
    res = Reservation.query.get_or_404(reservation_id)
    note = (request.form.get("note") or "").strip()
    res.admin_notes = note or None
    db.session.commit()
    return jsonify({"ok": True, "admin_notes": res.admin_notes or ""})


@admin_bp.route("/reservations/<int:reservation_id>/delete", methods=["POST"])
def delete_reservation(reservation_id):
    """Hard-delete a Reservation and any CirclePayments for the same email.
    Used to clean up test data and customers who shouldn't appear in the
    dashboard. If the deleted row was the raffle winner, also clears the
    winner. Returns JSON when called via fetch (Accept includes JSON),
    otherwise redirects (legacy form submit).
    """
    from flask import jsonify
    res = Reservation.query.get(reservation_id)
    if res is None:
        if "application/json" in (request.headers.get("Accept") or ""):
            return jsonify(ok=False, error="not_found"), 404
        flash("Reservation not found.", "error")
        return redirect(url_for("admin.reservations"))

    state = _get_raffle_state()
    if state.winner_reservation_id == res.id:
        state.winner_reservation_id = None
        state.spun_at = None

    email = (res.email or "").lower()
    label = f"#{res.id} {res.email}"

    # Also delete any CirclePayments for the same email so the buyer
    # doesn't reappear as an orphan row after deletion.
    deleted_cps = 0
    if email:
        cps = CirclePayment.query.filter(CirclePayment.email.ilike(email)).all()
        for cp in cps:
            db.session.delete(cp)
            deleted_cps += 1

    db.session.delete(res)
    db.session.commit()

    msg = f"Deleted reservation {label}"
    if deleted_cps:
        msg += f" + {deleted_cps} Circle payment(s)"
    msg += "."

    if "application/json" in (request.headers.get("Accept") or ""):
        return jsonify(ok=True, deleted_reservation_id=reservation_id, deleted_circle_payments=deleted_cps)
    flash(msg, "success")
    return redirect(url_for("admin.reservations"))


@admin_bp.route("/circle-payments/<int:cp_id>/delete", methods=["POST"])
def delete_circle_payment(cp_id):
    """Hard-delete a CirclePayment. Used to remove orphan rows from the
    dashboard (direct buyers who shouldn't appear, test charges, etc.).
    Returns JSON. Idempotent — already-gone returns ok with deleted=False.
    """
    from flask import jsonify
    cp = CirclePayment.query.get(cp_id)
    if cp is None:
        return jsonify(ok=True, deleted=False, reason="not_found")
    label = f"{cp.email} (€{((cp.amount_cents or 0) / 100):.0f})"
    db.session.delete(cp)
    db.session.commit()
    logger.info("deleted CirclePayment %s — %s", cp_id, label)
    return jsonify(ok=True, deleted=True, label=label)


@admin_bp.route("/circle-payments/<int:cp_id>/link-ambassador", methods=["POST"])
def link_circle_payment_to_ambassador(cp_id):
    """Attach (or detach) an Ambassador to a CirclePayment.

    Body: {"ambassador_id": <int>} to link, {"ambassador_id": null} to unlink.
    Used for orphan payments (direct buyers without a matching Reservation)
    where the Stripe email doesn't equal the Ambassador's email — the admin
    knows it's the same person and links them by hand.
    """
    from flask import jsonify
    cp = CirclePayment.query.get_or_404(cp_id)
    body = request.get_json(silent=True) or {}
    raw = body.get("ambassador_id", "__missing__")
    if raw == "__missing__":
        return jsonify(ok=False, error="ambassador_id required (or null to unlink)"), 400

    if raw is None or raw == "":
        cp.ambassador_id = None
        db.session.commit()
        logger.info("unlinked CirclePayment %s from any ambassador", cp.id)
        return jsonify(ok=True, linked=False, ambassador=None)

    try:
        amb_id = int(raw)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="ambassador_id must be int or null"), 400

    amb = Ambassador.query.get(amb_id)
    if amb is None:
        return jsonify(ok=False, error="ambassador not found"), 404

    cp.ambassador_id = amb.id
    db.session.commit()
    logger.info("linked CirclePayment %s → Ambassador %s (%s)", cp.id, amb.id, amb.email)
    return jsonify(
        ok=True,
        linked=True,
        ambassador={
            "id": amb.id,
            "name": amb.name,
            "email": amb.email,
            "phone": amb.phone_number or "",
            "profile_picture_url": amb.profile_picture_url or "",
        },
    )


@admin_bp.route("/reservations")
def reservations():
    """Admin control room for the MKOT 3.0 live event.

    Shows every Reservation row (paid + form-completed + pending), with raffle
    controls (close window, spin, reset) and a link to open the public stage
    view in a new tab.
    """
    state = _get_raffle_state()
    eligible = _eligible_reservations(state)
    all_rows = (
        Reservation.query
        .order_by(Reservation.paid_at.desc().nullslast(), Reservation.created_at.desc())
        .all()
    )
    paid_total = Reservation.query.filter(Reservation.paid_at.isnot(None)).count()
    completed_total = Reservation.query.filter(
        Reservation.paid_at.isnot(None),
        Reservation.form_completed_at.isnot(None),
    ).count()
    pending_form = paid_total - completed_total
    revenue = _revenue_breakdown(all_rows)

    # Latest CirclePayment per email (descending) — used to render the
    # "Plan completo" column for each Reservation row. Only counts the
    # current edition (filters out historical MKOT 2.0 etc.).
    circle_by_email = {}
    full_paid_total = 0
    full_paid_amount_cents = 0
    for cp in CirclePayment.query.order_by(CirclePayment.paid_at.desc().nullslast()).all():
        if not cp.email or not _is_current_edition(cp):
            continue
        key = cp.email.lower()
        if key not in circle_by_email:
            circle_by_email[key] = cp
            full_paid_total += 1
            full_paid_amount_cents += (cp.amount_cents or 0)

    # Order index by Circle paid_at ASC — earliest payer = #1. Used to
    # award the "video feedback" gift to the first 50. Same filter so
    # last year's payments don't get a #N rank.
    ordered_cps = [
        cp for cp in
        CirclePayment.query.order_by(CirclePayment.paid_at.asc().nullslast()).all()
        if _is_current_edition(cp)
    ]
    # Rank by UNIQUE EMAIL (not by individual charge), so subscription
    # renewals don't push the rank forward. Buyer #5 stays #5 even if
    # they pay 6 monthly installments.
    order_index_by_email = {}
    rank_counter = 0
    for cp in ordered_cps:
        if not cp.email:
            continue
        key = cp.email.lower()
        if key in order_index_by_email:
            continue
        rank_counter += 1
        order_index_by_email[key] = rank_counter
    TOP_N_VIDEO = 50

    # Orphan buyers: paid the full plan but never went through reservation.
    reservation_emails = {r.email.lower() for r in all_rows if r.email}
    orphan_payments = [
        cp for cp in ordered_cps
        if cp.email and cp.email.lower() not in reservation_emails
    ]
    # De-dup by email (latest payment per orphan email wins for display).
    seen_orphan_emails = set()
    deduped_orphans = []
    for cp in sorted(orphan_payments, key=lambda c: c.paid_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        if cp.email.lower() in seen_orphan_emails:
            continue
        seen_orphan_emails.add(cp.email.lower())
        deduped_orphans.append(cp)

    # Cash collected (NET): deposits in + full payments in − refunds out.
    deposits_in_cents = sum(r.amount_cents or 0 for r in all_rows if r.paid_at)
    full_in_cents = sum(cp.amount_cents or 0 for cp in ordered_cps)
    refunds_out_cents = sum(
        r.refund_amount_cents or 0
        for r in all_rows
        if r.refund_status == "success"
    )
    cash_gross_cents = deposits_in_cents + full_in_cents
    cash_net_cents = cash_gross_cents - refunds_out_cents

    # Count refunded reservations whose buyer hasn't been notified yet.
    pending_refund_emails = sum(
        1 for r in all_rows
        if r.refund_status == "success" and not r.refund_email_sent_at
    )

    # Count CirclePayments (current edition) that haven't been invoiced.
    pending_invoices = sum(
        1 for cp in ordered_cps
        if not cp.invoice_sent_at
    )

    # Count paid reservations we can't WhatsApp (no phone on file) and
    # haven't yet sent the "trying to reach you" email to.
    pending_no_phone_emails = sum(
        1 for r in all_rows
        if r.paid_at and not _reservation_has_phone(r) and not r.no_phone_email_sent_at
    )

    # Pre-compute payment inference per CirclePayment so the template can
    # render "chose vs paid" badges without re-running the helper per row.
    from app.services.payment_inference import infer_from_payment
    inferred_by_cp_id = {cp.id: infer_from_payment(cp) for cp in ordered_cps}

    return render_template(
        "admin_reservations.html",
        page_title="Reservas",
        active_section="reservations",
        rows=all_rows,
        eligible_count=len(eligible),
        paid_total=paid_total,
        completed_total=completed_total,
        pending_form=pending_form,
        state=state,
        winner=state.winner if state.winner_reservation_id else None,
        revenue=revenue,
        circle_by_email=circle_by_email,
        full_paid_total=full_paid_total,
        full_paid_amount_cents=full_paid_amount_cents,
        order_index_by_email=order_index_by_email,
        top_n_video=TOP_N_VIDEO,
        orphan_payments=deduped_orphans,
        inferred_by_cp_id=inferred_by_cp_id,
        cash_net_cents=cash_net_cents,
        cash_gross_cents=cash_gross_cents,
        deposits_in_cents=deposits_in_cents,
        full_in_cents=full_in_cents,
        refunds_out_cents=refunds_out_cents,
        pending_refund_emails=pending_refund_emails,
        pending_invoices=pending_invoices,
        pending_no_phone_emails=pending_no_phone_emails,
        **_admin_layout_context(),
    )


@admin_bp.route("/reservations.json")
def reservations_json():
    """Live polling endpoint for the /admin/reservations page."""
    from flask import jsonify
    state = _get_raffle_state()
    rows = (
        Reservation.query
        .order_by(Reservation.paid_at.desc().nullslast(), Reservation.created_at.desc())
        .all()
    )
    eligible = _eligible_reservations(state)
    rev = _revenue_breakdown(rows)

    # Same join as /admin/reservations — latest CirclePayment per email.
    # Filter out historical/non-current-edition payments.
    circle_by_email = {}
    full_paid_total = 0
    full_paid_amount_cents = 0
    for cp in CirclePayment.query.order_by(CirclePayment.paid_at.desc().nullslast()).all():
        if not cp.email or not _is_current_edition(cp):
            continue
        key = cp.email.lower()
        if key not in circle_by_email:
            circle_by_email[key] = cp
            full_paid_total += 1
            full_paid_amount_cents += (cp.amount_cents or 0)

    # Order index by Circle paid_at ASC (current edition only).
    # Rank by UNIQUE EMAIL so subscription renewals don't shift positions.
    ordered_cps = [
        cp for cp in
        CirclePayment.query.order_by(CirclePayment.paid_at.asc().nullslast()).all()
        if _is_current_edition(cp)
    ]
    order_index_by_email = {}
    rank_counter = 0
    for cp in ordered_cps:
        if not cp.email:
            continue
        key = cp.email.lower()
        if key in order_index_by_email:
            continue
        rank_counter += 1
        order_index_by_email[key] = rank_counter
    TOP_N_VIDEO = 50

    # Cash collected (NET).
    deposits_in_cents = sum(r.amount_cents or 0 for r in rows if r.paid_at)
    full_in_cents = sum(cp.amount_cents or 0 for cp in ordered_cps)
    refunds_out_cents = sum(
        r.refund_amount_cents or 0 for r in rows if r.refund_status == "success"
    )
    cash_gross_cents = deposits_in_cents + full_in_cents
    cash_net_cents = cash_gross_cents - refunds_out_cents
    pending_refund_emails = sum(
        1 for r in rows
        if r.refund_status == "success" and not r.refund_email_sent_at
    )
    pending_invoices = sum(1 for cp in ordered_cps if not cp.invoice_sent_at)
    pending_no_phone_emails = sum(
        1 for r in rows
        if r.paid_at and not _reservation_has_phone(r) and not r.no_phone_email_sent_at
    )

    # Orphans for the JSON payload (emails that paid full but no Reservation).
    from app.services.payment_inference import infer_from_payment
    reservation_emails = {r.email.lower() for r in rows if r.email}
    orphan_seen = set()
    orphan_rows_payload = []
    for cp in sorted(ordered_cps, key=lambda c: c.paid_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        if not cp.email:
            continue
        key = cp.email.lower()
        if key in reservation_emails or key in orphan_seen:
            continue
        orphan_seen.add(key)
        inferred = infer_from_payment(cp)
        linked_amb = cp.ambassador if cp.ambassador_id else None
        orphan_rows_payload.append({
            "id": cp.id,
            "email": cp.email,
            "name": cp.customer_name or "",
            "amount_cents": cp.amount_cents or 0,
            "paid_at": cp.paid_at.isoformat() if cp.paid_at else None,
            "description": cp.description or "",
            "order_index": order_index_by_email.get(key),
            "is_top_50": (order_index_by_email.get(key) or 999) <= TOP_N_VIDEO,
            "invoice_sent_at": cp.invoice_sent_at.isoformat() if cp.invoice_sent_at else None,
            "invoice_id": cp.invoice_id or "",
            "inferred_program": inferred["program"],
            "inferred_modality": inferred["modality"],
            "inferred_payment_plan": inferred["payment_plan"],
            "inference_source": inferred["source"],
            "linked_ambassador": ({
                "id": linked_amb.id,
                "name": linked_amb.name,
                "email": linked_amb.email,
                "phone": linked_amb.phone_number or "",
                "profile_picture_url": linked_amb.profile_picture_url or "",
            } if linked_amb else None),
        })

    rows_payload = []
    for r in rows:
        cp = circle_by_email.get(r.email.lower()) if r.email else None
        order_idx = order_index_by_email.get(r.email.lower()) if r.email else None
        cp_inferred = infer_from_payment(cp) if cp else None
        rows_payload.append({
            "id": r.id,
            "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "form_completed_at": r.form_completed_at.isoformat() if r.form_completed_at else None,
            "email": r.email,
            "name": r.name or "",
            "surname": r.surname or "",
            "program_choice": r.program_choice or "",
            "modality_choice": r.modality_choice or "",
            "payment_plan": r.payment_plan or "",
            "clarity": r.clarity or "",
            "notes": r.notes or "",
            "amount_cents": r.amount_cents or 0,
            "stripe_session_id": r.stripe_session_id,
            "ambassador_id": r.ambassador_id,
            "phone": (r.ambassador.phone_number if (r.ambassador and r.ambassador.phone_number) else ""),
            "last_contacted_at": r.last_contacted_at.isoformat() if r.last_contacted_at else None,
            "last_contacted_channel": r.last_contacted_channel or "",
            "admin_notes": r.admin_notes or "",
            # Refund fields
            "refund_status": r.refund_status or "",
            "refunded_at": r.refunded_at.isoformat() if r.refunded_at else None,
            "refund_id": r.refund_id or "",
            "refund_error": r.refund_error or "",
            "refund_email_sent_at": r.refund_email_sent_at.isoformat() if r.refund_email_sent_at else None,
            "no_phone_email_sent_at": r.no_phone_email_sent_at.isoformat() if r.no_phone_email_sent_at else None,
            "has_phone": _reservation_has_phone(r),
            # CirclePayment join (full plan paid) + auto-inferred fields
            # so the admin can compare what the buyer ELECTED on the form
            # vs what they actually PAID for in Stripe.
            "circle_payment": ({
                "amount_cents": cp.amount_cents or 0,
                "paid_at": cp.paid_at.isoformat() if cp.paid_at else None,
                "description": cp.description or "",
                "invoice_sent_at": cp.invoice_sent_at.isoformat() if cp.invoice_sent_at else None,
                "invoice_id": cp.invoice_id or "",
                "inferred_program": cp_inferred["program"] if cp_inferred else None,
                "inferred_modality": cp_inferred["modality"] if cp_inferred else None,
                "inferred_payment_plan": cp_inferred["payment_plan"] if cp_inferred else None,
            } if cp else None),
            "order_index": order_idx,
            "is_top_50": (order_idx is not None and order_idx <= TOP_N_VIDEO),
        })

    return jsonify({
        "window_closed_at": state.window_closed_at.isoformat() if state.window_closed_at else None,
        "winner_id": state.winner_reservation_id,
        "winner": (
            {"name": state.winner.name or "", "surname": state.winner.surname or ""}
            if state.winner_reservation_id and state.winner else None
        ),
        "eligible_count": len(eligible),
        "paid_total": sum(1 for r in rows if r.paid_at),
        "completed_total": sum(1 for r in rows if r.paid_at and r.form_completed_at),
        "full_paid_total": full_paid_total,
        "full_paid_amount_cents": full_paid_amount_cents,
        "cash_net_cents": cash_net_cents,
        "cash_gross_cents": cash_gross_cents,
        "deposits_in_cents": deposits_in_cents,
        "full_in_cents": full_in_cents,
        "refunds_out_cents": refunds_out_cents,
        "pending_refund_emails": pending_refund_emails,
        "pending_invoices": pending_invoices,
        "pending_no_phone_emails": pending_no_phone_emails,
        "top_n_video": TOP_N_VIDEO,
        "revenue": {
            "estimated_total": rev["estimated_total"],
            "deposits_in":     rev["deposits_in"],
            "outstanding":     rev["outstanding"],
            "completed":       rev["completed"],
            "avg_per_buyer":   round(rev["avg_per_buyer"], 0),
            "counts":          rev["counts"],
            "revenue":         rev["revenue"],
        },
        "rows": rows_payload,
        "orphan_rows": orphan_rows_payload,
    })


@admin_bp.route("/reservations/<int:reservation_id>/mark-refunded", methods=["POST"])
def mark_reservation_refunded(reservation_id):
    """Manually mark a Reservation as refunded (no Stripe call).

    Used by the admin to record refunds done by hand directly in Stripe
    Dashboard. Optional `send_email=1` query/body param emails the buyer
    a confirmation that their deposit is on its way.
    """
    from flask import jsonify
    r = Reservation.query.get_or_404(reservation_id)

    send_email_raw = (
        request.form.get("send_email")
        or request.args.get("send_email")
        or ""
    )
    if not send_email_raw and request.is_json:
        send_email_raw = str((request.get_json(silent=True) or {}).get("send_email", ""))
    send_email = send_email_raw.strip().lower() in ("1", "true", "yes", "on")

    now = datetime.now(timezone.utc)
    r.refunded_at = now
    r.refund_status = "success"
    r.refund_attempted_at = r.refund_attempted_at or now
    if not r.refund_id:
        r.refund_id = "MANUAL"
    if not r.refund_amount_cents:
        r.refund_amount_cents = r.amount_cents or 10000
    db.session.commit()

    email_sent = False
    if send_email:
        email_sent = _send_refund_email_and_stamp(r)

    logger.info(
        "manually marked reservation %s refunded (email_sent=%s)", r.id, email_sent,
    )
    return jsonify(
        ok=True,
        reservation_id=r.id,
        refund_status=r.refund_status,
        refunded_at=r.refunded_at.isoformat(),
        email_sent=email_sent,
    )


@admin_bp.route("/reservations/<int:reservation_id>/unmark-refunded", methods=["POST"])
def unmark_reservation_refunded(reservation_id):
    """Roll back a manual refund mark. Does NOT touch Stripe."""
    from flask import jsonify
    r = Reservation.query.get_or_404(reservation_id)
    r.refunded_at = None
    r.refund_status = None
    r.refund_id = None
    r.refund_amount_cents = None
    r.refund_attempted_at = None
    r.refund_error = None
    r.refund_email_sent_at = None
    db.session.commit()
    logger.info("unmarked refund on reservation %s", r.id)
    return jsonify(ok=True, reservation_id=r.id)


@admin_bp.route("/invoices")
def invoices():
    """Listing of all sent invoices, filterable by search + month.

    Pulls every CirclePayment with invoice_sent_at set. Pure read-only.
    """
    q_search = (request.args.get("q") or "").strip().lower()
    q_month = (request.args.get("month") or "").strip()  # "YYYY-MM"

    query = (
        CirclePayment.query
        .filter(CirclePayment.invoice_sent_at.isnot(None))
        .order_by(CirclePayment.invoice_sent_at.desc())
    )
    rows = query.all()

    # In-Python filter (small dataset, simple).
    if q_search:
        rows = [
            cp for cp in rows
            if (cp.invoice_id and q_search in cp.invoice_id.lower())
            or (cp.email and q_search in cp.email.lower())
            or (cp.customer_name and q_search in cp.customer_name.lower())
            or (cp.description and q_search in cp.description.lower())
        ]
    if q_month and len(q_month) == 7:
        rows = [
            cp for cp in rows
            if cp.invoice_sent_at and cp.invoice_sent_at.strftime("%Y-%m") == q_month
        ]

    # Aggregate KPIs across the FULL set (unfiltered) so the admin sees
    # totals for the whole archive regardless of current filter.
    all_invoiced = (
        CirclePayment.query
        .filter(CirclePayment.invoice_sent_at.isnot(None))
        .all()
    )
    total_count = len(all_invoiced)
    total_amount_cents = sum(cp.amount_cents or 0 for cp in all_invoiced)

    # Build a list of YYYY-MM options from existing invoices for the filter.
    months = sorted({
        cp.invoice_sent_at.strftime("%Y-%m") for cp in all_invoiced if cp.invoice_sent_at
    }, reverse=True)

    return render_template(
        "admin_invoices.html",
        rows=rows,
        q_search=q_search,
        q_month=q_month,
        months=months,
        total_count=total_count,
        total_amount_cents=total_amount_cents,
        filtered_count=len(rows),
        filtered_amount_cents=sum(cp.amount_cents or 0 for cp in rows),
        active_section="invoices",
        **_admin_layout_context(),
    )


@admin_bp.route("/circle-payments/<int:cp_id>/preview-invoice")
def preview_invoice_pdf(cp_id):
    """Serve the invoice PDF for a CirclePayment.

    If the invoice has already been sent AND the bytes were archived,
    serve the exact copy the customer got. Otherwise regenerate from
    current data so the link always works (older invoices sent before
    archive-to-DB shipped don't have bytes on file).
    """
    from flask import Response
    from app.services.invoice_pdf import generate_invoice_pdf
    cp = CirclePayment.query.get_or_404(cp_id)

    # Already-sent invoice with archived bytes → serve the immutable copy.
    if cp.invoice_pdf_bytes and cp.invoice_id:
        filename = cp.invoice_id.replace('"', '')
        return Response(
            bytes(cp.invoice_pdf_bytes),
            mimetype="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}.pdf"'},
        )

    # Live regen: either invoice was never sent (preview) or it was sent
    # before we started archiving the bytes. Either way the customer gets
    # a working PDF when they click.
    invoice_number = cp.invoice_id or "INV-PREVIEW"
    line_description = cp.description or "Digital services — MetaKizz Project"
    try:
        pdf = generate_invoice_pdf(
            invoice_number=invoice_number,
            customer_email=cp.email,
            customer_name=cp.customer_name,
            line_items=[{
                "description": line_description,
                "qty": 1,
                "unit_price_cents": cp.amount_cents or 0,
            }],
            currency=(cp.currency or "usd").upper(),
            stripe_charge_id=cp.stripe_charge_id,
            issue_date=cp.paid_at or datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("preview_invoice_pdf: regen failed for cp=%s", cp.id)
        return ("Failed to render invoice. Check server logs.", 500)
    filename = invoice_number.replace('"', '')
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}_preview.pdf"'},
    )


@admin_bp.route("/circle-payments/<int:cp_id>/send-invoice", methods=["POST"])
def send_invoice_for_circle_payment(cp_id):
    """Generate + send the invoice for one CirclePayment. Idempotent
    unless force=1 (regenerates and resends, keeping the same number)."""
    from flask import jsonify
    cp = CirclePayment.query.get_or_404(cp_id)
    force = (request.form.get("force") or request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
    if cp.invoice_sent_at and not force:
        return jsonify(ok=True, sent=False, reason="already_sent",
                       sent_at=cp.invoice_sent_at.isoformat(), invoice_id=cp.invoice_id)
    if force:
        cp.invoice_sent_at = None
        db.session.commit()
    sent = _generate_and_send_invoice(cp, force=force)
    return jsonify(
        ok=True,
        sent=sent,
        invoice_id=cp.invoice_id,
        sent_at=cp.invoice_sent_at.isoformat() if cp.invoice_sent_at else None,
    )


@admin_bp.route("/circle-payments/send-pending-invoices", methods=["POST"])
def send_pending_invoices():
    """Bulk-generate + send invoices for every CirclePayment in the
    current edition that has not been invoiced yet."""
    from flask import jsonify
    candidates = [
        cp for cp in
        CirclePayment.query
            .filter(CirclePayment.invoice_sent_at.is_(None))
            .all()
        if _is_current_edition(cp)
    ]
    if _is_dry_run():
        return jsonify(
            ok=True, dry_run=True,
            candidates=[_summarize_circle_payment(cp) for cp in candidates],
            to_send=len(candidates), to_skip=0,
        )
    sent = 0
    failed = 0
    for cp in candidates:
        if _generate_and_send_invoice(cp):
            sent += 1
        else:
            failed += 1
    logger.info(
        "send_pending_invoices: candidates=%d sent=%d failed=%d",
        len(candidates), sent, failed,
    )
    return jsonify(ok=True, candidates=len(candidates), sent=sent, failed=failed)


@admin_bp.route("/preview-refund-email")
def preview_refund_email():
    """Render the refund confirmation email for visual review.

    Optional ?reservation_id=<id> — preview with real data from a specific
    reservation. Default: dummy data ("Maria López", €100).
    """
    rid_raw = request.args.get("reservation_id")
    reservation = None
    if rid_raw:
        try:
            reservation = Reservation.query.get(int(rid_raw))
        except Exception:
            reservation = None

    if reservation is None:
        # Build a lightweight dummy that quacks like a Reservation enough
        # for the email builder.
        class _Dummy:
            email = "preview@example.com"
            name = "Maria"
            ambassador = None
            amount_cents = 10000
            refund_amount_cents = 10000
        reservation = _Dummy()

    html, _amount = build_refund_confirmation_html(reservation)
    return html


@admin_bp.route("/reservations/<int:reservation_id>/send-refund-email", methods=["POST"])
def send_refund_email_for_reservation(reservation_id):
    """Send (or re-send) the refund confirmation email for a single
    reservation. Honors ?force=1 to override the refund_email_sent_at
    guard, otherwise it skips if already sent.
    """
    from flask import jsonify
    r = Reservation.query.get_or_404(reservation_id)
    if r.refund_status != "success":
        return jsonify(ok=False, error="reservation not refunded"), 400

    force = (request.form.get("force") or request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
    if r.refund_email_sent_at and not force:
        return jsonify(ok=True, sent=False, reason="already_sent",
                       sent_at=r.refund_email_sent_at.isoformat())

    if force:
        r.refund_email_sent_at = None  # so the helper sends again
        db.session.commit()

    sent = _send_refund_email_and_stamp(r)
    return jsonify(
        ok=True,
        sent=sent,
        sent_at=r.refund_email_sent_at.isoformat() if r.refund_email_sent_at else None,
    )


@admin_bp.route("/reservations/send-pending-refund-emails", methods=["POST"])
def send_pending_refund_emails():
    """Bulk-send the refund confirmation email to every reservation that
    has refund_status='success' but no refund_email_sent_at yet. Useful
    after a backfill of manual refunds. Returns count sent + skipped.
    """
    from flask import jsonify
    targets = (
        Reservation.query
        .filter(Reservation.refund_status == "success")
        .filter(Reservation.refund_email_sent_at.is_(None))
        .all()
    )
    if _is_dry_run():
        return jsonify(
            ok=True, dry_run=True,
            candidates=[_summarize_reservation(r) for r in targets],
            to_send=len(targets), to_skip=0,
        )
    sent = 0
    failed = 0
    for r in targets:
        if _send_refund_email_and_stamp(r):
            sent += 1
        else:
            failed += 1
    logger.info(
        "send_pending_refund_emails: candidates=%d sent=%d failed=%d",
        len(targets), sent, failed,
    )
    return jsonify(ok=True, candidates=len(targets), sent=sent, failed=failed)


@admin_bp.route("/reservations/<int:reservation_id>/send-no-phone-email", methods=["POST"])
def send_no_phone_email_for_reservation(reservation_id):
    """Send (or re-send) the "tried to reach you on WhatsApp but couldn't"
    email for a single reservation. Use ?force=1 to re-send.
    """
    from flask import jsonify
    r = Reservation.query.get_or_404(reservation_id)
    force = (request.form.get("force") or request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
    if r.no_phone_email_sent_at and not force:
        return jsonify(ok=True, sent=False, reason="already_sent",
                       sent_at=r.no_phone_email_sent_at.isoformat())
    if force:
        r.no_phone_email_sent_at = None
        db.session.commit()
    sent = _send_no_phone_email_and_stamp(r)
    return jsonify(
        ok=True,
        sent=sent,
        sent_at=r.no_phone_email_sent_at.isoformat() if r.no_phone_email_sent_at else None,
    )


@admin_bp.route("/reservations/send-pending-no-phone-emails", methods=["POST"])
def send_pending_no_phone_emails():
    """Bulk-send the "tried to reach you on WhatsApp" email to every paid
    reservation that has no phone on file (via the matched Ambassador) and
    hasn't been emailed yet.
    """
    from flask import jsonify
    targets = (
        Reservation.query
        .filter(Reservation.paid_at.isnot(None))
        .filter(Reservation.no_phone_email_sent_at.is_(None))
        .all()
    )
    targets = [r for r in targets if not _reservation_has_phone(r)]
    if _is_dry_run():
        return jsonify(
            ok=True, dry_run=True,
            candidates=[_summarize_reservation(r) for r in targets],
            to_send=len(targets), to_skip=0,
        )
    sent = 0
    failed = 0
    for r in targets:
        if _send_no_phone_email_and_stamp(r):
            sent += 1
        else:
            failed += 1
    logger.info(
        "send_pending_no_phone_emails: candidates=%d sent=%d failed=%d",
        len(targets), sent, failed,
    )
    return jsonify(ok=True, candidates=len(targets), sent=sent, failed=failed)


@admin_bp.route("/preview-no-phone-email")
def preview_no_phone_email():
    """Render the no-phone outreach email body for preview (HTML in browser).
    Picks the most recent paid reservation without a phone number. Falls
    back to a synthesized stub so the page renders even with an empty DB.
    """
    sample = (
        Reservation.query
        .filter(Reservation.paid_at.isnot(None))
        .order_by(Reservation.paid_at.desc())
        .first()
    )
    if sample is None:
        # Render with a stub so the admin can still preview the layout.
        class _Stub:
            email = "you@example.com"
            name = "Sample"
            ambassador = None
            amount_cents = 10000
        sample = _Stub()
    html = build_no_phone_outreach_html(sample)
    return html


@admin_bp.route("/preview-carrots-landing-email")
def preview_carrots_landing_email():
    """Render the "Carrots & onions → landing page" email for visual
    review in the browser. Query params:
      ?name=<str>   first name (default "Carla")
      ?hero=1       force-include the hero rabbit image (uses placeholder)
      ?hole=1       force-include the rabbit-hole image
      ?onion=1      force-include the onion image
    """
    import os as _os
    from flask import render_template

    first_name = (request.args.get("name") or "Carla").strip() or "Carla"
    app_url = (current_app.config.get("APP_URL") or request.host_url or "https://example.com").rstrip("/")

    # Image URLs come from env (so production uses uploaded assets). Allow
    # ?hero=1 / ?hole=1 / ?onion=1 to force a placeholder so the admin
    # can preview the layout WITH the rabbit slots filled even before
    # the artwork ships.
    placeholder = f"{app_url}/static/brand/rabbit/placeholder.png"
    hero = _os.getenv("RABBIT_HERO_URL", "").strip() or (
        placeholder if request.args.get("hero") else None
    )
    hole = _os.getenv("RABBIT_HOLE_URL", "").strip() or (
        placeholder if request.args.get("hole") else None
    )
    onion = _os.getenv("RABBIT_ONION_URL", "").strip() or (
        placeholder if request.args.get("onion") else None
    )

    metadancers_url = _os.getenv("METADANCERS_URL", "").strip() or (
        "https://inevitable.metakizzproject.com/mkot3"
    )
    metainstructors_url = _os.getenv("METAINSTRUCTORS_URL", "").strip() or (
        "https://inevitable.metakizzproject.com/mkot3-instructors"
    )

    return render_template(
        "emails/carrots_landing.html",
        first_name=first_name,
        community=True,
        metadancers_url=metadancers_url,
        metainstructors_url=metainstructors_url,
        hero_image_url=hero,
        rabbithole_image_url=hole,
        onion_image_url=onion,
        dashboard_url=f"{app_url}/dashboard/preview",
        unsubscribe_url=f"{app_url}/unsubscribe/preview",
        app_url=app_url,
    )


@admin_bp.route("/preview-masterclass-email")
def preview_masterclass_email():
    """Render the "here's your prize" masterclass invitation email for
    visual review in the browser.

    Query params (all optional):
      ?count=<int>   simulate referral_count (drives the personalization
                     branch — 0 / 1-4 / 5+ each show different copy)
      ?name=<str>    first name to address (default "Carla")
    """
    import os as _os
    from flask import render_template
    from app.mailer import _masterclass_calendar_urls

    try:
        referral_count = max(0, int(request.args.get("count", "5") or 5))
    except ValueError:
        referral_count = 5
    first_name = (request.args.get("name") or "Carla").strip() or "Carla"

    app_url = (current_app.config.get("APP_URL") or request.host_url or "https://example.com").rstrip("/")
    ics_url, gcal_url, outlook_url = _masterclass_calendar_urls(app_url)

    join_url = _os.getenv("MASTERCLASS_JOIN_URL", "").strip() or (
        "https://us06web.zoom.us/j/87205814207?pwd=k0ZugO56KMvaKLMdyjbDn7YH2mCzJw.1"
    )
    topic = _os.getenv("MASTERCLASS_TOPIC", "").strip() or (
        "Musicality Masterclass · Hacking the Urbankiz Code"
    )
    date_label = _os.getenv("MASTERCLASS_DATE_LABEL", "").strip() or "May 15 · 18:00 Madrid"
    meeting_id = _os.getenv("MASTERCLASS_MEETING_ID", "").strip() or "872 0581 4207"
    passcode = _os.getenv("MASTERCLASS_PASSCODE", "").strip() or "488349"

    return render_template(
        "emails/masterclass_invitation.html",
        first_name=first_name,
        community=True,
        referral_count=referral_count,
        join_url=join_url,
        topic=topic,
        date_label=date_label,
        meeting_id=meeting_id,
        passcode=passcode,
        ics_url=ics_url,
        google_calendar_url=gcal_url,
        outlook_calendar_url=outlook_url,
        dashboard_url=f"{app_url}/dashboard/preview",
        unsubscribe_url=f"{app_url}/unsubscribe/preview",
        app_url=app_url,
    )


# ─── BULK ACTIONS ON SELECTED RESERVATIONS ─────────────────────────
#
# The /admin/reservations page lets the admin tick checkboxes on multiple
# rows and apply one operation to all of them at once. Each endpoint
# accepts JSON `{"ids": [1,2,3]}` or form-encoded `ids=1&ids=2&ids=3`.
# Returns counts of processed/sent/failed for the toast in the UI.

def _parse_id_list(field="ids"):
    """Pull an integer list of IDs out of the current request. Accepts
    JSON body, form data (repeated keys), or comma-separated string.
    Returns a list of ints (de-duplicated, order preserved).
    """
    raw_ids = []
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw = body.get(field) or body.get(f"{field}[]")
        if isinstance(raw, list):
            raw_ids = raw
        elif isinstance(raw, str):
            raw_ids = raw.split(",")
    if not raw_ids:
        raw_ids = request.form.getlist(field) or request.form.getlist(f"{field}[]")
    if not raw_ids:
        s = request.form.get(field) or request.args.get(field) or ""
        if s:
            raw_ids = s.split(",")

    out, seen = [], set()
    for v in raw_ids:
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _is_dry_run():
    """True if the current request asked for a dry-run preview instead of
    an actual send. Reads `dry_run` from JSON body, form data, or query
    string. Used by the "preview before send" modal to fetch the candidate
    list without firing emails.
    """
    raw = ""
    if request.is_json:
        raw = (request.get_json(silent=True) or {}).get("dry_run") or ""
    if not raw:
        raw = request.form.get("dry_run") or request.args.get("dry_run") or ""
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _summarize_reservation(r, skip_reason=None):
    """Compact dict for the bulk-email preview modal."""
    name = ((r.name or "") + " " + (r.surname or "")).strip() or "(no name)"
    return {
        "id": r.id,
        "name": name,
        "email": r.email or "",
        "amount_eur": round((r.amount_cents or 0) / 100, 2),
        "skip_reason": skip_reason,
    }


def _summarize_circle_payment(cp, skip_reason=None):
    """Compact dict for the bulk-invoice preview modal."""
    return {
        "id": cp.id,
        "name": cp.customer_name or "(no name)",
        "email": cp.email or "",
        "amount_eur": round((cp.amount_cents or 0) / 100, 2),
        "skip_reason": skip_reason,
    }


@admin_bp.route("/reservations/bulk-send-no-phone-email", methods=["POST"])
def bulk_send_no_phone_email():
    """Send the no-phone outreach email to a specific list of reservations.
    Skips rows that already have it sent. Idempotent.
    """
    from flask import jsonify
    ids = _parse_id_list("ids")
    if not ids:
        return jsonify(ok=False, error="no_ids"), 400
    rows = Reservation.query.filter(Reservation.id.in_(ids)).all()
    if _is_dry_run():
        candidates = []
        to_send = to_skip = 0
        for r in rows:
            if r.no_phone_email_sent_at:
                stamp = r.no_phone_email_sent_at.strftime("%Y-%m-%d %H:%M")
                candidates.append(_summarize_reservation(r, skip_reason=f"already sent {stamp}"))
                to_skip += 1
            else:
                candidates.append(_summarize_reservation(r))
                to_send += 1
        return jsonify(
            ok=True, dry_run=True, candidates=candidates,
            to_send=to_send, to_skip=to_skip,
        )
    sent = failed = skipped = 0
    for r in rows:
        if r.no_phone_email_sent_at:
            skipped += 1
            continue
        if _send_no_phone_email_and_stamp(r):
            sent += 1
        else:
            failed += 1
    logger.info(
        "bulk_send_no_phone_email: ids=%d found=%d sent=%d failed=%d skipped=%d",
        len(ids), len(rows), sent, failed, skipped,
    )
    return jsonify(ok=True, candidates=len(rows), sent=sent, failed=failed, skipped=skipped)


@admin_bp.route("/reservations/bulk-send-refund-email", methods=["POST"])
def bulk_send_refund_email():
    """Send the refund-on-the-way email to a list of refunded reservations.
    Skips rows already notified or not yet marked as refunded.
    """
    from flask import jsonify
    ids = _parse_id_list("ids")
    if not ids:
        return jsonify(ok=False, error="no_ids"), 400
    rows = Reservation.query.filter(Reservation.id.in_(ids)).all()
    if _is_dry_run():
        candidates = []
        to_send = to_skip = 0
        for r in rows:
            if r.refund_status != "success":
                candidates.append(_summarize_reservation(r, skip_reason="not refunded yet"))
                to_skip += 1
            elif r.refund_email_sent_at:
                stamp = r.refund_email_sent_at.strftime("%Y-%m-%d %H:%M")
                candidates.append(_summarize_reservation(r, skip_reason=f"already sent {stamp}"))
                to_skip += 1
            else:
                candidates.append(_summarize_reservation(r))
                to_send += 1
        return jsonify(
            ok=True, dry_run=True, candidates=candidates,
            to_send=to_send, to_skip=to_skip,
        )
    sent = failed = skipped = 0
    for r in rows:
        if r.refund_status != "success" or r.refund_email_sent_at:
            skipped += 1
            continue
        if _send_refund_email_and_stamp(r):
            sent += 1
        else:
            failed += 1
    logger.info(
        "bulk_send_refund_email: ids=%d found=%d sent=%d failed=%d skipped=%d",
        len(ids), len(rows), sent, failed, skipped,
    )
    return jsonify(ok=True, candidates=len(rows), sent=sent, failed=failed, skipped=skipped)


@admin_bp.route("/reservations/bulk-mark-refunded", methods=["POST"])
def bulk_mark_refunded():
    """Mark every selected reservation as refunded (manual, no Stripe call).
    Optionally send the confirmation email at the same time (send_email=1).
    """
    from flask import jsonify
    ids = _parse_id_list("ids")
    if not ids:
        return jsonify(ok=False, error="no_ids"), 400

    send_email_raw = (
        request.form.get("send_email")
        or request.args.get("send_email")
        or (request.get_json(silent=True) or {}).get("send_email", "") if request.is_json else ""
    )
    if isinstance(send_email_raw, bool):
        send_email = send_email_raw
    else:
        send_email = str(send_email_raw or "").strip().lower() in ("1", "true", "yes", "on")

    rows = Reservation.query.filter(Reservation.id.in_(ids)).all()
    now = datetime.now(timezone.utc)
    marked = email_sent = email_failed = skipped = 0
    for r in rows:
        if r.refund_status == "success":
            skipped += 1
        else:
            r.refunded_at = now
            r.refund_status = "success"
            r.refund_attempted_at = r.refund_attempted_at or now
            if not r.refund_id:
                r.refund_id = "MANUAL"
            if not r.refund_amount_cents:
                r.refund_amount_cents = r.amount_cents or 10000
            marked += 1
        if send_email and not r.refund_email_sent_at:
            if _send_refund_email_and_stamp(r):
                email_sent += 1
            else:
                email_failed += 1
    db.session.commit()
    logger.info(
        "bulk_mark_refunded: ids=%d found=%d marked=%d skipped=%d email_sent=%d email_failed=%d",
        len(ids), len(rows), marked, skipped, email_sent, email_failed,
    )
    return jsonify(
        ok=True, candidates=len(rows), marked=marked, skipped=skipped,
        email_sent=email_sent, email_failed=email_failed,
    )


@admin_bp.route("/reservations/bulk-delete", methods=["POST"])
def bulk_delete_reservations():
    """Hard-delete a list of reservations + any CirclePayments for the
    same emails. Returns counts. Same blast radius as the per-row delete.
    """
    from flask import jsonify
    ids = _parse_id_list("ids")
    if not ids:
        return jsonify(ok=False, error="no_ids"), 400

    rows = Reservation.query.filter(Reservation.id.in_(ids)).all()
    state = _get_raffle_state()
    deleted_reservations = 0
    deleted_cps = 0
    for r in rows:
        if state.winner_reservation_id == r.id:
            state.winner_reservation_id = None
            state.spun_at = None
        email = (r.email or "").lower()
        if email:
            cps = CirclePayment.query.filter(CirclePayment.email.ilike(email)).all()
            for cp in cps:
                db.session.delete(cp)
                deleted_cps += 1
        db.session.delete(r)
        deleted_reservations += 1
    db.session.commit()
    logger.info(
        "bulk_delete_reservations: ids=%d deleted_reservations=%d deleted_cps=%d",
        len(ids), deleted_reservations, deleted_cps,
    )
    return jsonify(
        ok=True,
        deleted_reservations=deleted_reservations,
        deleted_circle_payments=deleted_cps,
    )


@admin_bp.route("/circle-payments/bulk-send-invoice", methods=["POST"])
def bulk_send_invoice():
    """Generate + email the PDF invoice for a list of CirclePayments.
    Skips rows already invoiced.
    """
    from flask import jsonify
    ids = _parse_id_list("ids")
    if not ids:
        return jsonify(ok=False, error="no_ids"), 400
    cps = CirclePayment.query.filter(CirclePayment.id.in_(ids)).all()
    if _is_dry_run():
        candidates = []
        to_send = to_skip = 0
        for cp in cps:
            if cp.invoice_sent_at:
                stamp = cp.invoice_sent_at.strftime("%Y-%m-%d %H:%M")
                candidates.append(_summarize_circle_payment(cp, skip_reason=f"already invoiced {stamp}"))
                to_skip += 1
            else:
                candidates.append(_summarize_circle_payment(cp))
                to_send += 1
        return jsonify(
            ok=True, dry_run=True, candidates=candidates,
            to_send=to_send, to_skip=to_skip,
        )
    sent = failed = skipped = 0
    for cp in cps:
        if cp.invoice_sent_at:
            skipped += 1
            continue
        try:
            if _generate_and_send_invoice(cp):
                sent += 1
            else:
                failed += 1
        except Exception:
            logger.exception("bulk_send_invoice: failed for cp=%s", cp.id)
            failed += 1
    logger.info(
        "bulk_send_invoice: ids=%d found=%d sent=%d failed=%d skipped=%d",
        len(ids), len(cps), sent, failed, skipped,
    )
    return jsonify(ok=True, candidates=len(cps), sent=sent, failed=failed, skipped=skipped)


@admin_bp.route("/email-tests/send", methods=["POST"])
def send_test_bulk_email():
    """Send a single test copy of one of the bulk emails to an arbitrary
    address. Does NOT stamp any *_sent_at fields and does NOT assign a
    real invoice number — it's purely for "what will this look like in
    my inbox?" verification before launching the real send.

    Body (JSON or form): {kind, to, ids?}
        kind: "no_phone" | "refund" | "invoice"
        to: destination address for the test
        ids: optional — if provided, use the first row as the body source
             (otherwise we pick a representative sample row).
    """
    from flask import jsonify
    from app.mailer import _send, _send_with_attachment

    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or request.form.get("kind") or "").strip().lower()
    to = (body.get("to") or request.form.get("to") or "").strip()
    ids_raw = body.get("ids") or []
    if isinstance(ids_raw, str):
        ids_raw = [x for x in ids_raw.split(",") if x]

    if not to or "@" not in to:
        return jsonify(ok=False, error="invalid_email"), 400
    if kind not in ("no_phone", "refund", "invoice"):
        return jsonify(ok=False, error="invalid_kind"), 400

    sample_id = None
    try:
        sample_id = int(ids_raw[0]) if ids_raw else None
    except (TypeError, ValueError):
        sample_id = None

    if kind == "no_phone":
        sample = None
        if sample_id:
            sample = Reservation.query.get(sample_id)
        if sample is None:
            sample = (
                Reservation.query
                .filter(Reservation.paid_at.isnot(None))
                .order_by(Reservation.paid_at.desc())
                .first()
            )
        if sample is None:
            return jsonify(ok=False, error="no_sample_row"), 400
        html = build_no_phone_outreach_html(sample)
        ok = bool(_send(to, "[TEST] Trying to reach you about your MKOT 3.0 plan", html))
        return jsonify(ok=True, sent=ok, kind=kind, to=to, sample_id=sample.id)

    if kind == "refund":
        sample = None
        if sample_id:
            sample = Reservation.query.get(sample_id)
        if sample is None:
            sample = (
                Reservation.query
                .filter(Reservation.refund_status == "success")
                .order_by(Reservation.id.desc())
                .first()
            )
        if sample is None:
            sample = Reservation.query.order_by(Reservation.id.desc()).first()
        if sample is None:
            return jsonify(ok=False, error="no_sample_row"), 400
        html, _amount = build_refund_confirmation_html(sample)
        ok = bool(_send(to, "[TEST] Your €100 deposit is on its way back", html))
        return jsonify(ok=True, sent=ok, kind=kind, to=to, sample_id=sample.id)

    # kind == "invoice"
    from app.services.invoice_pdf import generate_invoice_pdf, safe_pdf_filename
    sample_cp = None
    if sample_id:
        sample_cp = CirclePayment.query.get(sample_id)
    if sample_cp is None:
        sample_cp = (
            CirclePayment.query
            .order_by(CirclePayment.id.desc())
            .first()
        )
    if sample_cp is None:
        return jsonify(ok=False, error="no_sample_row"), 400

    biz_name = os.getenv("INVOICE_BUSINESS_NAME", "Virtual Flow LLC").strip()
    test_invoice_number = "INV-TEST"
    line_description = sample_cp.description or "Digital services — MetaKizz Project"
    amount = sample_cp.amount_cents or 0

    try:
        pdf_bytes = generate_invoice_pdf(
            invoice_number=test_invoice_number,
            customer_email=sample_cp.email,
            customer_name=sample_cp.customer_name,
            line_items=[{
                "description": line_description,
                "qty": 1,
                "unit_price_cents": amount,
            }],
            currency=(sample_cp.currency or "usd").upper(),
            stripe_charge_id=sample_cp.stripe_charge_id,
            issue_date=sample_cp.paid_at or datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("invoice PDF test generation failed for cp=%s", sample_cp.id)
        return jsonify(ok=False, error="pdf_generation_failed"), 500

    from app.mailer import build_invoice_email_html
    html = build_invoice_email_html(sample_cp, test_invoice_number)
    filename = safe_pdf_filename(test_invoice_number, sample_cp.customer_name, sample_cp.email)
    ok = bool(_send_with_attachment(
        to=to,
        subject=f"[TEST] Your invoice from {biz_name} — {test_invoice_number}",
        html=html,
        attachment_bytes=pdf_bytes,
        attachment_filename=filename,
        from_name=biz_name,
    ))
    return jsonify(ok=True, sent=ok, kind=kind, to=to, sample_id=sample_cp.id)


@admin_bp.route("/buyer/<path:email>")
def buyer_detail(email):
    """Per-buyer profile aggregating everything we know about this email:
    Reservation (if any), CirclePayments, Ambassador (Circle member),
    PartnerInvite (sent or received), LeadEvents (timeline), LeadNotes.
    """
    email = (email or "").strip().lower()
    if not email:
        return redirect(url_for("admin.reservations"))

    reservation = (
        Reservation.query
        .filter(Reservation.email.ilike(email))
        .order_by(Reservation.paid_at.desc().nullslast())
        .first()
    )
    circle_payments = [
        cp for cp in
        CirclePayment.query
            .filter(CirclePayment.email.ilike(email))
            .order_by(CirclePayment.paid_at.desc().nullslast())
            .all()
        if _is_current_edition(cp)
    ]
    ambassador = Ambassador.query.filter(Ambassador.email.ilike(email)).first()
    partner_invite_as_buyer = (
        PartnerInvite.query
        .filter(PartnerInvite.buyer_email.ilike(email))
        .order_by(PartnerInvite.created_at.desc())
        .first()
    )
    partner_invite_as_partner = (
        PartnerInvite.query
        .filter(PartnerInvite.partner_email.ilike(email))
        .order_by(PartnerInvite.created_at.desc())
        .first()
    )
    lead_events = (
        LeadEvent.query
        .filter(LeadEvent.email.ilike(email))
        .order_by(LeadEvent.created_at.desc())
        .limit(500)
        .all()
    )
    # LeadNotes: we keep them per-ambassador. If the buyer is an ambassador, fetch theirs.
    lead_notes = []
    if ambassador:
        from app.models import LeadNote
        lead_notes = (
            LeadNote.query
            .filter_by(ambassador_id=ambassador.id)
            .order_by(LeadNote.created_at.desc())
            .all()
        )

    # Order index (for the "Top 50" badge) — current-edition only.
    # Counted by UNIQUE EMAIL so subscription renewals don't shift the rank.
    order_index = None
    is_top_50 = False
    TOP_N_VIDEO = 50
    if circle_payments and circle_payments[0].paid_at:
        ordered = [
            cp for cp in
            CirclePayment.query.order_by(CirclePayment.paid_at.asc().nullslast()).all()
            if _is_current_edition(cp)
        ]
        seen_emails = set()
        rank = 0
        for cp in ordered:
            if not cp.email:
                continue
            key = cp.email.lower()
            if key in seen_emails:
                continue
            seen_emails.add(key)
            rank += 1
            if key == email:
                order_index = rank
                break
        is_top_50 = (order_index is not None and order_index <= TOP_N_VIDEO)

    # Unified, chronological timeline (most recent first).
    timeline = []
    if reservation:
        if reservation.paid_at:
            timeline.append({
                "ts": reservation.paid_at,
                "type": "deposit_paid",
                "icon": "💰",
                "label": f"€{(reservation.amount_cents or 10000)/100:.0f} deposit paid",
            })
        if reservation.form_completed_at:
            timeline.append({
                "ts": reservation.form_completed_at,
                "type": "form_completed",
                "icon": "📝",
                "label": "Reservation form completed",
            })
        if reservation.refunded_at:
            timeline.append({
                "ts": reservation.refunded_at,
                "type": "refunded",
                "icon": "↩️",
                "label": f"€{(reservation.refund_amount_cents or 10000)/100:.0f} deposit refunded",
                "extra": reservation.refund_id or "",
            })
        if reservation.last_contacted_at:
            timeline.append({
                "ts": reservation.last_contacted_at,
                "type": "contacted",
                "icon": "📞",
                "label": f"Contacted via {reservation.last_contacted_channel or '—'}",
            })
    for cp in circle_payments:
        if cp.paid_at:
            timeline.append({
                "ts": cp.paid_at,
                "type": "full_paid",
                "icon": "🎉",
                "label": f"€{(cp.amount_cents or 0)/100:.0f} full plan paid (Circle)",
                "extra": cp.description or "",
            })
        if cp.invoice_sent_at:
            timeline.append({
                "ts": cp.invoice_sent_at,
                "type": "invoice_sent",
                "icon": "📄",
                "label": "Invoice sent",
                "extra": cp.invoice_id or "",
            })
    if partner_invite_as_buyer:
        timeline.append({
            "ts": partner_invite_as_buyer.created_at,
            "type": "partner_invited",
            "icon": "🫶🏼",
            "label": f"Invited partner: {partner_invite_as_buyer.partner_name} ({partner_invite_as_buyer.partner_email})",
        })
    if partner_invite_as_partner:
        timeline.append({
            "ts": partner_invite_as_partner.created_at,
            "type": "invited_by",
            "icon": "🫶🏼",
            "label": f"Invited by: {partner_invite_as_partner.buyer_name} ({partner_invite_as_partner.buyer_email})",
        })
    for ev in lead_events:
        timeline.append({
            "ts": ev.created_at,
            "type": "activity",
            "icon": _activity_icon(ev.event_type),
            "label": _activity_label(ev),
            "extra": ev.event_type,
        })
    for n in lead_notes:
        timeline.append({
            "ts": n.created_at,
            "type": "admin_note",
            "icon": "✎",
            "label": n.type or "note",
            "content": n.content or "",
        })

    timeline.sort(
        key=lambda e: e["ts"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    # Status header pills
    has_deposit = bool(reservation and reservation.paid_at)
    has_full_paid = bool(circle_payments)
    has_refund = bool(reservation and reservation.refund_status == "success")
    track = None  # 'dancers' | 'instructors' from PartnerInvite.target_group
    if partner_invite_as_buyer and partner_invite_as_buyer.target_group:
        track = partner_invite_as_buyer.target_group

    return render_template(
        "admin_buyer_detail.html",
        email=email,
        reservation=reservation,
        circle_payments=circle_payments,
        ambassador=ambassador,
        partner_invite_as_buyer=partner_invite_as_buyer,
        partner_invite_as_partner=partner_invite_as_partner,
        timeline=timeline,
        order_index=order_index,
        is_top_50=is_top_50,
        has_deposit=has_deposit,
        has_full_paid=has_full_paid,
        has_refund=has_refund,
        track=track,
        active_section="reservations",
        **_admin_layout_context(),
    )


def _activity_icon(event_type):
    et = (event_type or "").lower()
    if "webinar" in et:
        return "🎥"
    if "completed" in et:
        return "✓"
    if "viewed" in et or "started" in et:
        return "▶"
    if "progress" in et:
        return "⏳"
    if "resource" in et or "download" in et:
        return "📎"
    if "purchase" in et:
        return "🛒"
    return "•"


def _activity_label(ev):
    et = (ev.event_type or "").replace("_", " ")
    if ev.class_number:
        et = f"Class {ev.class_number} · {et}"
    if ev.pct is not None:
        et += f" ({ev.pct}%)"
    if ev.webinar_duration_min:
        et += f" — {ev.webinar_duration_min} min"
    return et


@admin_bp.route("/circle-payments/sync", methods=["POST"])
def sync_circle_payments():
    """Pull recent payments from the Circle Stripe account and upsert
    CirclePayment rows. Catches up on payments that arrived before the
    webhook was wired (or any webhook misfire in the future).

    Reads STRIPE_CIRCLE_API_KEY. Idempotent on stripe_charge_id —
    re-running is safe.
    """
    from flask import jsonify
    api_key = os.getenv("STRIPE_CIRCLE_API_KEY", "").strip()
    if not api_key:
        return jsonify(ok=False, error="STRIPE_CIRCLE_API_KEY not configured"), 400
    try:
        import stripe
    except ImportError:
        return jsonify(ok=False, error="stripe package missing"), 500

    limit = int(request.args.get("limit") or 100)
    limit = max(1, min(100, limit))

    created = 0
    backfilled = 0
    skipped = 0
    errors = []

    def _extract_session_description(s):
        """Pull the product/line item name from an expanded checkout session."""
        items = (s.get("line_items") or {}).get("data") or []
        if not items:
            return None
        first = items[0]
        desc = first.get("description") or None
        if not desc:
            price = first.get("price") or {}
            product = price.get("product") or {}
            if isinstance(product, dict):
                desc = product.get("name") or None
        return desc

    try:
        # Checkout sessions with full expansion so we capture the product name.
        sessions = stripe.checkout.Session.list(
            api_key=api_key,
            limit=limit,
            status="complete",
            expand=[
                "data.payment_intent",
                "data.customer_details",
                "data.line_items",
                "data.line_items.data.price.product",
            ],
        )
        for s in sessions.get("data") or []:
            try:
                charge_id = (
                    (s.get("payment_intent") or {}).get("id")
                    if isinstance(s.get("payment_intent"), dict)
                    else (s.get("payment_intent") or s.get("id"))
                )
                if not charge_id:
                    skipped += 1
                    continue
                customer_details = s.get("customer_details") or {}
                email = (customer_details.get("email") or s.get("customer_email") or "").strip().lower()
                if not email:
                    skipped += 1
                    continue
                description = _extract_session_description(s)

                existing = CirclePayment.query.filter_by(stripe_charge_id=charge_id).first()
                if existing is not None:
                    # Backfill any missing fields (description, customer_name) but
                    # never overwrite existing data.
                    updated = False
                    if not existing.description and description:
                        existing.description = description
                        updated = True
                    if not existing.customer_name and customer_details.get("name"):
                        existing.customer_name = customer_details.get("name")
                        updated = True
                    if updated:
                        backfilled += 1
                    else:
                        skipped += 1
                    continue

                created_ts = s.get("created")
                paid_at = (
                    datetime.fromtimestamp(created_ts, tz=timezone.utc)
                    if created_ts else datetime.now(timezone.utc)
                )
                pi = s.get("payment_intent")
                pi_id = pi.get("id") if isinstance(pi, dict) else pi
                db.session.add(CirclePayment(
                    stripe_charge_id=charge_id,
                    stripe_payment_intent_id=pi_id,
                    email=email,
                    customer_name=customer_details.get("name"),
                    amount_cents=s.get("amount_total"),
                    currency=(s.get("currency") or "eur").lower(),
                    paid_at=paid_at,
                    description=description,
                    raw_event_type="manual_sync_session",
                ))
                created += 1
            except Exception as e:
                errors.append(f"session {s.get('id')}: {e}")

        # Also pull recent succeeded charges for safety.
        charges = stripe.Charge.list(api_key=api_key, limit=limit)
        for c in charges.get("data") or []:
            try:
                if c.get("status") != "succeeded":
                    continue
                charge_id = c.get("id")
                if not charge_id:
                    continue
                billing = c.get("billing_details") or {}
                email = (billing.get("email") or c.get("receipt_email") or "").strip().lower()
                if not email:
                    skipped += 1
                    continue
                description = c.get("description") or None

                existing = CirclePayment.query.filter_by(stripe_charge_id=charge_id).first()
                if existing is not None:
                    updated = False
                    if not existing.description and description:
                        existing.description = description
                        updated = True
                    if not existing.customer_name and billing.get("name"):
                        existing.customer_name = billing.get("name")
                        updated = True
                    if updated:
                        backfilled += 1
                    else:
                        skipped += 1
                    continue

                created_ts = c.get("created")
                paid_at = (
                    datetime.fromtimestamp(created_ts, tz=timezone.utc)
                    if created_ts else datetime.now(timezone.utc)
                )
                db.session.add(CirclePayment(
                    stripe_charge_id=charge_id,
                    stripe_payment_intent_id=c.get("payment_intent"),
                    email=email,
                    customer_name=billing.get("name"),
                    amount_cents=c.get("amount"),
                    currency=(c.get("currency") or "eur").lower(),
                    paid_at=paid_at,
                    description=description,
                    raw_event_type="manual_sync_charge",
                ))
                created += 1
            except Exception as e:
                errors.append(f"charge {c.get('id')}: {e}")

        db.session.commit()
    except Exception as e:
        logger.exception("sync_circle_payments failed")
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500

    logger.info(
        "sync_circle_payments: created=%d backfilled=%d skipped=%d errors=%d",
        created, backfilled, skipped, len(errors),
    )
    return jsonify(
        ok=True,
        created=created,
        backfilled=backfilled,
        skipped=skipped,
        errors=errors[:5],
    )


@admin_bp.route("/circle-payments/cleanup-non-mkot3", methods=["POST"])
def cleanup_non_mkot3_payments():
    """Permanently delete CirclePayments that don't match the current MKOT
    edition keywords (set via MKOT_EDITION_KEYWORDS env var, defaults to
    "MKOT 3.0,MKOT3,MKOT 3"). Useful after a sync brought in historical
    payments from previous editions. Returns the deleted count plus a
    sample of affected emails so the admin can sanity-check.
    """
    from flask import jsonify
    all_cps = CirclePayment.query.all()
    to_delete = [cp for cp in all_cps if not _is_current_edition(cp)]
    count = len(to_delete)
    samples = [
        {
            "email": cp.email,
            "amount_cents": cp.amount_cents or 0,
            "description": cp.description or "(none)",
            "paid_at": cp.paid_at.isoformat() if cp.paid_at else None,
        }
        for cp in to_delete[:5]
    ]
    for cp in to_delete:
        db.session.delete(cp)
    db.session.commit()
    logger.info("cleanup_non_mkot3: deleted %d CirclePayments", count)
    return jsonify(ok=True, deleted=count, samples=samples)


@admin_bp.route("/raffle")
def raffle():
    state = _get_raffle_state()
    eligible = _eligible_reservations(state)
    winner = state.winner if state.winner_reservation_id else None
    return render_template(
        "admin_raffle.html",
        state=state,
        eligible=eligible,
        eligible_count=len(eligible),
        winner=winner,
    )


@admin_bp.route("/raffle/state.json")
def raffle_state_json():
    """Polled by the raffle page to refresh the entrant list and live ticker."""
    from flask import jsonify
    state = _get_raffle_state()
    eligible = _eligible_reservations(state)

    # Live activity signals for the stage ticker.
    last_payment = (
        Reservation.query
        .filter(Reservation.paid_at.isnot(None))
        .order_by(Reservation.paid_at.desc())
        .first()
    )
    last_completion = (
        Reservation.query
        .filter(Reservation.form_completed_at.isnot(None))
        .order_by(Reservation.form_completed_at.desc())
        .first()
    )
    # "Filling out the form right now" = paid but no form yet, in the last 30 min.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    in_progress = Reservation.query.filter(
        Reservation.paid_at.isnot(None),
        Reservation.form_completed_at.is_(None),
        Reservation.paid_at >= cutoff,
    ).count()

    return jsonify({
        "window_closed_at": state.window_closed_at.isoformat() if state.window_closed_at else None,
        "winner_id": state.winner_reservation_id,
        "eligible_count": len(eligible),
        "eligible": [
            {
                "id": r.id,
                "name": r.name or "",
                "surname": r.surname or "",
                "form_completed_at": r.form_completed_at.isoformat() if r.form_completed_at else None,
            }
            for r in eligible
        ],
        "winner": (
            {
                "id": state.winner.id,
                "name": state.winner.name or "",
                "surname": state.winner.surname or "",
            }
            if state.winner_reservation_id and state.winner else None
        ),
        "activity": {
            "last_payment_at": last_payment.paid_at.isoformat() if last_payment and last_payment.paid_at else None,
            "last_completion_at": last_completion.form_completed_at.isoformat() if last_completion and last_completion.form_completed_at else None,
            "in_progress_count": in_progress,
        },
    })


@admin_bp.route("/raffle/close", methods=["POST"])
def raffle_close():
    state = _get_raffle_state()
    if state.window_closed_at is None:
        state.window_closed_at = datetime.now(timezone.utc)
        state.closed_by_admin = "admin"
        db.session.commit()
    return redirect(url_for("admin.raffle"))


@admin_bp.route("/raffle/spin", methods=["POST"])
def raffle_spin():
    """Pick a random winner from current eligibles. Idempotent: once a
    winner is set, returns the same one."""
    from flask import jsonify
    import random
    state = _get_raffle_state()
    if state.winner_reservation_id:
        # Already spun — return the existing winner.
        w = state.winner
        return jsonify({
            "ok": True, "already_spun": True,
            "winner": {"id": w.id, "name": w.name or "", "surname": w.surname or ""},
        })

    eligible = _eligible_reservations(state)
    if not eligible:
        return jsonify({"ok": False, "error": "no eligible reservations"}), 400

    winner = random.choice(eligible)
    state.winner_reservation_id = winner.id
    state.spun_at = datetime.now(timezone.utc)
    # If the window wasn't closed yet, close it now (spinning implies cutoff).
    if state.window_closed_at is None:
        state.window_closed_at = state.spun_at
        state.closed_by_admin = "admin (spin)"
    db.session.commit()
    return jsonify({
        "ok": True, "already_spun": False,
        "winner": {"id": winner.id, "name": winner.name or "", "surname": winner.surname or ""},
    })


@admin_bp.route("/raffle/reset", methods=["POST"])
def raffle_reset():
    """Reset the raffle window AND winner. Use with care — for testing or
    for running multiple raffles in the same session."""
    state = _get_raffle_state()
    state.window_closed_at = None
    state.winner_reservation_id = None
    state.spun_at = None
    state.closed_by_admin = None
    db.session.commit()
    return redirect(url_for("admin.raffle"))


# ─── Zoom webinar attendance import ──────────────────────────────────────
# Two paths to the same goal: insert one LeadEvent(event_type="webinar_joined")
# per participant email so future reminder emails (live_imminent,
# webinar_reminder) correctly skip those who already attended via
# `exclude_if_event_in`.
#
#   1. Paste-CSV path: admin pastes the Zoom export (CSV or any text — we
#      regex-extract emails). Works tonight without a Zoom app.
#   2. API path: pulls participants directly from the Zoom Reports API
#      using Server-to-Server OAuth credentials (ZOOM_* env vars).
#      One-click, idempotent.

@admin_bp.route("/zoom/attendees")
def zoom_attendees():
    """Engagement breakdown for the most recent webinar import.

    Buckets:
      - Top fans:    duration_min >= 45  (engaged the whole way through)
      - Tibios:      10 <= duration_min < 45
      - Pasaron:     duration_min < 10   (joined to peek)
      - Unknown:     duration_min IS NULL (paste path or no data)

    Also splits matched ambassadors vs ghost leads, and shows the top
    countries / devices observed.
    """
    rows = (
        LeadEvent.query
        .filter(LeadEvent.event_type == "webinar_joined")
        .order_by(LeadEvent.webinar_duration_min.desc().nullslast(),
                  LeadEvent.created_at.desc())
        .all()
    )

    # Bucket boundaries match the heat-scoring tiers in temperature.py
    # (see TEMP_WEIGHTS: webinar_attended_full=60+, _long=30-60, _short=10-30,
    # _brief=<10). Aligning here so /admin/zoom/attendees and /admin/leads
    # never disagree on who's a "top fan" vs a "long-stayer" etc.
    top_fans = [r for r in rows if (r.webinar_duration_min or 0) >= 60]   # full sit-through
    long_stayers = [r for r in rows if 30 <= (r.webinar_duration_min or 0) < 60]
    short_stayers = [r for r in rows if 10 <= (r.webinar_duration_min or 0) < 30]
    brief = [r for r in rows if 0 < (r.webinar_duration_min or 0) < 10]
    unknown = [r for r in rows if not r.webinar_duration_min]
    # Legacy `tibios` / `pasaron` aliases preserved for the template
    # transition so existing references don't 500. Will be removed when
    # the template is updated.
    tibios = long_stayers + short_stayers
    pasaron = brief

    matched = sum(1 for r in rows if r.ambassador_id)
    ghosts = sum(1 for r in rows if not r.ambassador_id)

    # Country histogram (top 10).
    from collections import Counter
    country_count = Counter(r.webinar_country for r in rows if r.webinar_country)
    device_count = Counter(r.webinar_device for r in rows if r.webinar_device)

    # Resolve ambassador info for matched rows so the table can show name.
    amb_ids = [r.ambassador_id for r in rows if r.ambassador_id]
    amb_lookup = {}
    if amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(amb_ids)).all():
            amb_lookup[a.id] = a

    return render_template(
        "admin_zoom_attendees.html",
        page_title="Zoom Attendees",
        active_section="emails",
        rows=rows,
        top_fans=top_fans,
        long_stayers=long_stayers,
        short_stayers=short_stayers,
        brief=brief,
        tibios=tibios,
        pasaron=pasaron,
        unknown=unknown,
        matched=matched,
        ghosts=ghosts,
        country_top=country_count.most_common(10),
        device_top=device_count.most_common(),
        total=len(rows),
        amb_lookup=amb_lookup,
        **_admin_layout_context(),
    )


@admin_bp.route("/class-views")
def class_views():
    """Per-class engagement dashboard with first-view vs rewatch buckets.

    Pulls every class{N}_viewed/completed/progress_* LeadEvent, segments
    each ambassador as: never-viewed | first-view-only | returner |
    rewatch-only (rare). Surfaces the "didn't return" set (first-view but no
    rewatch yet) — that's the audience for the rewatch reminder email.

    Filters via query string:
      ?class=1|2|3   — restrict the unified table to one class only
      ?bucket=returner|sleeper|rewatch_only|first_only  — restrict by bucket
      ?min_pct=25|50|75|95   — only rows with at least this max progress
    """
    cutoff = _rewatch_cutoff()  # global default for top of page
    cutoff_iso = current_app.config.get("REWATCH_WINDOW_OPENS_AT")
    # Per-class cutoffs — class 3 may have a different window than 1/2.
    cutoffs_per_class = {n: _rewatch_cutoff(n) for n in (1, 2, 3)}

    # Filter inputs
    f_class = (request.args.get("class") or "").strip()
    try:
        f_class = int(f_class) if f_class else None
        if f_class not in (1, 2, 3):
            f_class = None
    except (TypeError, ValueError):
        f_class = None
    f_bucket = (request.args.get("bucket") or "").strip().lower()
    if f_bucket not in ("returner", "sleeper", "rewatch_only", "first_only"):
        f_bucket = ""
    try:
        f_min_pct = int(request.args.get("min_pct") or 0)
    except (TypeError, ValueError):
        f_min_pct = 0
    if f_min_pct not in (0, 25, 50, 75, 95):
        f_min_pct = 0

    # All class engagement events in one scan (≤10k rows in practice).
    class_event_types = [
        "class1_viewed", "class2_viewed", "class3_viewed",
        "class1_completed", "class2_completed", "class3_completed",
        "class1_progress_25", "class1_progress_50", "class1_progress_75", "class1_progress_95",
        "class2_progress_25", "class2_progress_50", "class2_progress_75", "class2_progress_95",
        "class3_progress_25", "class3_progress_50", "class3_progress_75", "class3_progress_95",
    ]
    events = (
        db.session.query(
            LeadEvent.email, LeadEvent.event_type, LeadEvent.created_at,
            LeadEvent.pct, LeadEvent.ambassador_id,
        )
        .filter(LeadEvent.event_type.in_(class_event_types))
        .filter(LeadEvent.email.isnot(None))
        .order_by(LeadEvent.created_at.asc())
        .all()
    )

    # by_pair = { (email_lower, class_n): {
    #     "first_view_at", "last_view_at", "before_count", "after_count",
    #     "max_pct", "completed", "ambassador_id"
    # }}
    by_pair = {}
    for em, ev_type, ts, pct, amb_id in events:
        em_norm = (em or "").lower()
        if not em_norm:
            continue
        try:
            n = int(ev_type[5])
        except (IndexError, ValueError):
            continue
        if n not in (1, 2, 3):
            continue
        ts_aware = ts if (ts and ts.tzinfo) else (ts.replace(tzinfo=timezone.utc) if ts else None)
        if ts_aware is None:
            continue
        rec = by_pair.setdefault((em_norm, n), {
            "first_view_at": ts_aware,
            "last_view_at": ts_aware,
            "before_count": 0,
            "after_count": 0,
            "max_pct": 0,
            "completed": False,
            "ambassador_id": amb_id,
        })
        if ts_aware < rec["first_view_at"]:
            rec["first_view_at"] = ts_aware
        if ts_aware > rec["last_view_at"]:
            rec["last_view_at"] = ts_aware
        # Use per-class cutoff so class 3 (live-replay) can have a different
        # rewatch window than 1/2.
        cls_cutoff = cutoffs_per_class.get(n)
        if cls_cutoff is not None:
            if ts_aware < cls_cutoff:
                rec["before_count"] += 1
            else:
                rec["after_count"] += 1
        if pct and pct > rec["max_pct"]:
            rec["max_pct"] = pct
        if ev_type.endswith("_completed"):
            rec["completed"] = True
        if amb_id and not rec["ambassador_id"]:
            rec["ambassador_id"] = amb_id

    # Resolve ambassadors for all matched ids in one shot.
    amb_ids = {rec["ambassador_id"] for rec in by_pair.values() if rec["ambassador_id"]}
    amb_lookup = {}
    if amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(amb_ids)).all():
            amb_lookup[a.id] = a

    # Build per-class summary + table rows.
    classes = []
    for n in (1, 2, 3):
        rows = []
        first_views_set = set()  # emails who watched at any point
        returners_set = set()
        sleepers_set = set()
        rewatch_only_set = set()
        cls_cutoff = cutoffs_per_class.get(n)
        for (em_norm, cn), rec in by_pair.items():
            if cn != n:
                continue
            first_views_set.add(em_norm)
            if cls_cutoff is None:
                # Without a cutoff, every viewer is a "first view"; no rewatch concept.
                bucket = "first_only"
            elif rec["before_count"] > 0 and rec["after_count"] > 0:
                bucket = "returner"
                returners_set.add(em_norm)
            elif rec["before_count"] > 0 and rec["after_count"] == 0:
                bucket = "sleeper"
                sleepers_set.add(em_norm)
            elif rec["before_count"] == 0 and rec["after_count"] > 0:
                bucket = "rewatch_only"
                rewatch_only_set.add(em_norm)
            else:
                bucket = "first_only"
            rows.append({
                "email": em_norm,
                "ambassador": amb_lookup.get(rec["ambassador_id"]),
                "first_view_at": rec["first_view_at"],
                "last_view_at": rec["last_view_at"],
                "before_count": rec["before_count"],
                "after_count": rec["after_count"],
                "max_pct": rec["max_pct"],
                "completed": rec["completed"],
                "bucket": bucket,
            })
        # Sort: returners first, then sleepers, then first-views; recent at top
        bucket_order = {"returner": 0, "sleeper": 1, "rewatch_only": 2, "first_only": 3}
        rows.sort(key=lambda r: (bucket_order.get(r["bucket"], 9), -(r["last_view_at"].timestamp() if r["last_view_at"] else 0)))
        classes.append({
            "n": n,
            "label": f"Class 0{n}",
            "total_unique": len(first_views_set),
            "returners": len(returners_set),
            "sleepers": len(sleepers_set),
            "rewatch_only": len(rewatch_only_set),
            "rows": rows,
        })

    # Apply filters to the unified row list (only used for the table at
    # the bottom — the per-class panel KPIs always show unfiltered totals).
    filtered_rows = []
    for cls in classes:
        if f_class is not None and cls["n"] != f_class:
            continue
        for r in cls["rows"]:
            if f_bucket and r["bucket"] != f_bucket:
                continue
            if f_min_pct and r["max_pct"] < f_min_pct:
                continue
            filtered_rows.append((cls, r))

    return render_template(
        "admin_class_views.html",
        page_title="Class Views",
        active_section="class_views",
        classes=classes,
        filtered_rows=filtered_rows,
        f_class=f_class,
        f_bucket=f_bucket,
        f_min_pct=f_min_pct,
        cutoff_iso=cutoff_iso,
        cutoff_dt=cutoff,
        cutoff_set=cutoff is not None,
        **_admin_layout_context(),
    )


@admin_bp.route("/zoom/rematch-ghosts", methods=["POST"])
def zoom_rematch_ghosts():
    """Second-pass fuzzy match: link Zoom ghost attendees (LeadEvent rows
    with event_type='webinar_joined' and ambassador_id IS NULL) to engaged
    ambassadors via UNIQUE name-token matching.

    Engaged = has any referral, any LeadEvent, or dashboard_visit_count>0.
    A "unique token" is one (>=4 chars, not in STOP_TOKENS) that appears in
    exactly one engaged ambassador's name. If the ghost's webinar_name
    contains any unique token, we link to that single ambassador.

    Conservative on purpose: ambiguous tokens (e.g. "Maria") never link.
    Idempotent: only processes rows where ambassador_id IS NULL.
    """
    import re

    # Common Spanish/Portuguese surnames + dance-scene first names that
    # are too frequent to reliably disambiguate with a single token.
    STOP_TOKENS = {
        "silva", "santos", "rodriguez", "garcia", "lopez", "martinez",
        "fernandez", "gonzalez", "perez", "sanchez", "ramirez", "torres",
        "ruiz", "diaz", "morales", "ortiz", "gomez", "hernandez", "alvarez",
        "moreno", "jimenez", "romero", "munoz", "alonso", "delgado",
        "castro", "martin", "navarro", "ortega", "iglesias", "medina",
        "garrido", "marquez", "molina", "pena", "vega", "soto", "calvo",
        "vargas", "blanco", "suarez", "carrasco", "guerrero", "caballero",
        "nieto", "pascual", "herrera", "duran",
        "maria", "anna", "anna_", "carlos", "david", "jose", "juan",
        "pedro", "anna", "ana", "laura", "sofia", "lucia", "marta",
        "nuria", "patricia", "diana", "elena", "irene", "iphone",
        "android", "user", "guest", "anonymous",
    }

    def tokenize(name):
        if not name:
            return []
        # Split on non-letter chars; lowercase; strip accents-loose.
        tokens = re.findall(r"[a-zA-ZÀ-ÿ]+", name.lower())
        return [t for t in tokens if len(t) >= 4 and t not in STOP_TOKENS]

    # Engaged ambassadors pool. Use a broad definition — the uniqueness
    # check is what protects against false positives.
    engaged = (
        Ambassador.query
        .outerjoin(Referral, Referral.ambassador_id == Ambassador.id)
        .outerjoin(LeadEvent, func.lower(LeadEvent.email) == func.lower(Ambassador.email))
        .filter(or_(
            Ambassador.dashboard_visit_count > 0,
            Referral.id.isnot(None),
            LeadEvent.id.isnot(None),
        ))
        .filter(Ambassador.unsubscribed_at.is_(None))
        .distinct()
        .all()
    )

    # Build {token: [amb_id, ...]} then keep only unique-token mappings.
    token_groups = {}
    for amb in engaged:
        seen_tokens = set()  # one ambassador contributes each token once
        for tok in tokenize(amb.name):
            if tok in seen_tokens:
                continue
            seen_tokens.add(tok)
            token_groups.setdefault(tok, set()).add(amb.id)
    unique_tokens = {t: next(iter(ids)) for t, ids in token_groups.items() if len(ids) == 1}

    # Iterate ghost rows.
    ghosts = (
        LeadEvent.query
        .filter(LeadEvent.event_type == "webinar_joined")
        .filter(LeadEvent.ambassador_id.is_(None))
        .filter(LeadEvent.webinar_name.isnot(None))
        .all()
    )

    matched_count = 0
    for ghost in ghosts:
        for tok in tokenize(ghost.webinar_name):
            amb_id = unique_tokens.get(tok)
            if amb_id:
                ghost.ambassador_id = amb_id
                matched_count += 1
                break

    db.session.commit()

    remaining = len(ghosts) - matched_count
    flash(
        f"Re-matched {matched_count} of {len(ghosts)} Zoom ghost attendees to engaged ambassadors via unique-token. "
        f"{remaining} still ghost (no matching unique token in any engaged ambassador's name).",
        "success",
    )
    return redirect(url_for("admin.zoom_attendees"))


@admin_bp.route("/zoom/debug")
def zoom_debug():
    """Diagnostic: shows what Zoom returns for past_meetings + each instance.
    Use when the import returns the wrong participant count to figure out
    if the meeting has multiple instances and which UUID has the real data.
    """
    from app.services import zoom as zoom_svc
    from flask import jsonify

    meeting_id = (request.args.get("meeting_id") or "82504511534").strip()
    if not meeting_id:
        return jsonify({"error": "pass ?meeting_id=..."})

    try:
        instances = zoom_svc.list_past_instances(meeting_id)
    except Exception as e:
        return jsonify({"error": f"list_past_instances failed: {e}"})

    instance_summaries = []
    if instances:
        token = zoom_svc._get_access_token()
        for inst in instances:
            uuid = inst.get("uuid")
            start = inst.get("start_time")
            if not uuid:
                continue
            encoded = zoom_svc._double_url_encode(uuid)
            participants, err = zoom_svc._fetch_participants_endpoint(token, "meetings", encoded)
            instance_summaries.append({
                "uuid": uuid,
                "start_time": start,
                "participant_count": len(participants),
                "first_3_emails": [p.get("user_email") for p in participants[:3]],
                "error": err,
            })

    # Also try the single-shot numeric ID for comparison
    token = zoom_svc._get_access_token()
    direct, direct_err = zoom_svc._fetch_participants_endpoint(token, "meetings", meeting_id)

    return jsonify({
        "meeting_id_queried": meeting_id,
        "instances_found": len(instances),
        "instances": instance_summaries,
        "direct_call_count": len(direct),
        "direct_call_first_3_emails": [p.get("user_email") for p in direct[:3]],
        "direct_call_error": direct_err,
    })


@admin_bp.route("/zoom/import-participants", methods=["POST"])
def import_zoom_participants():
    """Import webinar attendees as `webinar_joined` LeadEvents.

    Two source paths:
      • API (meeting_id) — pulls full participant records from Zoom Reports.
        Captures duration + country + device + join/leave timestamps.
      • Paste — regex-extracts emails from any pasted text (CSV exports etc.).
        Only fills email + match-to-ambassador; rich fields stay NULL.

    For the API path, multiple rows per email are coalesced into one event:
      - duration_min = sum of all session durations (rejoins counted)
      - webinar_joined_at = earliest join_time
      - webinar_left_at = latest leave_time
      - country/device = first non-empty value seen

    Idempotent: emails that already have a webinar_joined event are skipped
    (so re-running the import after fixing a missing email doesn't duplicate).
    """
    import re
    import json
    from app.services import zoom as zoom_svc

    raw = (request.form.get("emails", "") or "").strip()
    meeting_id = (request.form.get("meeting_id", "") or "").strip()

    # Per-email aggregated record (only the API path fills the rich fields).
    # email -> { duration_min, country, device, joined_at, left_at, raw }
    records = {}
    source_label = ""

    def _parse_iso(s):
        if not s:
            return None
        try:
            # Zoom returns "2026-05-07T19:01:23Z"
            from datetime import datetime as _dt
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    if meeting_id:
        try:
            participants = zoom_svc.fetch_meeting_participants(meeting_id)
        except Exception as e:
            logger.exception("zoom api fetch failed")
            flash(f"Zoom API error: {e}", "error")
            return redirect(url_for("admin.emails"))

        # KEY DESIGN: Zoom Meetings (vs Webinars) often return user_email=""
        # for guest joiners (people who clicked the link without being logged
        # in to a Zoom account). We MUST keep those rows — name is still a
        # useful identifier and we capture full engagement metrics for them.
        # The dedup key is email-when-present, else "name:" + normalized name.
        for p in participants:
            em = (p.get("user_email") or "").strip().lower()
            name = (p.get("name") or "").strip()
            # Skip rows that have neither — these are noise (Zoom occasionally
            # emits sentinel rows for waiting-room timeouts etc.).
            if not em and not name:
                continue
            if em and "@" not in em:
                em = ""  # treat malformed emails as missing
            key = em if em else "name:" + name.lower()

            duration_sec = int(p.get("duration") or 0)
            join_t = _parse_iso(p.get("join_time"))
            leave_t = _parse_iso(p.get("leave_time"))
            country = (p.get("location") or "").strip() or None
            device = (p.get("device") or "").strip() or None

            rec = records.get(key)
            if rec is None:
                rec = {
                    "email": em or None,
                    "name": name or None,
                    "duration_sec": 0,
                    "country": None,
                    "device": None,
                    "joined_at": None,
                    "left_at": None,
                    "raw_sessions": [],
                }
                records[key] = rec
            # Keep first non-empty name/email if multiple sessions disagree.
            if not rec["name"] and name:
                rec["name"] = name
            if not rec["email"] and em:
                rec["email"] = em
            rec["duration_sec"] += duration_sec
            if country and not rec["country"]:
                rec["country"] = country[:80]
            if device and not rec["device"]:
                rec["device"] = device[:40]
            if join_t and (rec["joined_at"] is None or join_t < rec["joined_at"]):
                rec["joined_at"] = join_t
            if leave_t and (rec["left_at"] is None or leave_t > rec["left_at"]):
                rec["left_at"] = leave_t
            rec["raw_sessions"].append({
                "name": name,
                "user_email": em,
                "join_time": p.get("join_time"),
                "leave_time": p.get("leave_time"),
                "duration": duration_sec,
                "device": device,
                "location": country,
            })
        source_label = f"API (meeting {meeting_id})"

    elif raw:
        # Tolerant parser: extract anything that looks like an email.
        pattern = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
        for em in pattern.findall(raw.lower()):
            records.setdefault(em, {
                "email": em, "name": None,
                "duration_sec": 0, "country": None, "device": None,
                "joined_at": None, "left_at": None, "raw_sessions": [],
            })
        source_label = "pasted list"
    else:
        flash("Either paste participant emails or enter the meeting ID.", "error")
        return redirect(url_for("admin.emails"))

    if not records:
        flash("No valid emails found in the input.", "error")
        return redirect(url_for("admin.emails"))

    now = datetime.now(timezone.utc)

    # Idempotency keys: emails that already have webinar_joined, AND
    # name-only events (we stored those without email last time around).
    existing_emails = {
        em.lower() for (em,) in
        db.session.query(LeadEvent.email)
        .filter(LeadEvent.event_type == "webinar_joined")
        .filter(LeadEvent.email.isnot(None))
        .all() if em
    }
    existing_names = {
        (n or "").strip().lower() for (n,) in
        db.session.query(LeadEvent.webinar_name)
        .filter(LeadEvent.event_type == "webinar_joined")
        .filter(LeadEvent.email.is_(None))
        .filter(LeadEvent.webinar_name.isnot(None))
        .all() if n
    }

    # Email-keyed and name-keyed ambassador lookups for matching.
    amb_by_email = {
        (em or "").lower(): aid for (aid, em) in
        db.session.query(Ambassador.id, Ambassador.email).all()
        if em
    }
    # Group ambassadors by lowercased name; only use the lookup when a name
    # has exactly ONE ambassador (avoids false positives on common names).
    name_groups = {}
    for aid, n in db.session.query(Ambassador.id, Ambassador.name).all():
        if not n:
            continue
        k = n.strip().lower()
        name_groups.setdefault(k, []).append(aid)
    amb_by_name_unique = {k: ids[0] for k, ids in name_groups.items() if len(ids) == 1}

    new_events = 0
    matched_by_email = 0
    matched_by_name = 0
    skipped = 0
    for key, rec in records.items():
        em = (rec.get("email") or "").lower()
        name = (rec.get("name") or "").strip()
        name_key = name.lower()

        if em and em in existing_emails:
            skipped += 1
            continue
        if not em and name_key and name_key in existing_names:
            skipped += 1
            continue

        amb_id = None
        if em and em in amb_by_email:
            amb_id = amb_by_email[em]
            matched_by_email += 1
        elif name_key and name_key in amb_by_name_unique:
            amb_id = amb_by_name_unique[name_key]
            matched_by_name += 1

        duration_min = (rec["duration_sec"] // 60) if rec["duration_sec"] else None
        # Truncate the raw audit JSON to ~4KB to stay safely under the 5KB
        # comment in the column. Most webinars produce <1KB per attendee.
        extra_json = None
        if rec["raw_sessions"]:
            try:
                extra_json = json.dumps(rec["raw_sessions"])[:4000]
            except Exception:
                extra_json = None
        db.session.add(LeadEvent(
            ambassador_id=amb_id,
            email=em or None,
            event_type="webinar_joined",
            webinar_duration_min=duration_min,
            webinar_country=rec["country"],
            webinar_device=rec["device"],
            webinar_joined_at=rec["joined_at"],
            webinar_left_at=rec["left_at"],
            webinar_name=name[:120] if name else None,
            extra=extra_json,
            created_at=now,
        ))
        new_events += 1
    db.session.commit()

    matched = matched_by_email + matched_by_name
    no_email_count = sum(1 for r in records.values() if not r.get("email"))
    flash(
        f"Imported {new_events} unique attendees from {source_label}. "
        f"{matched_by_email} matched by email, "
        f"{matched_by_name} matched by name, "
        f"{new_events - matched} unmatched (ghost). "
        f"{no_email_count} had no email captured (Zoom guest joiners). "
        f"{skipped} already imported (skipped).",
        "success",
    )
    return redirect(url_for("admin.emails"))


@admin_bp.route("/stripe-health")
def stripe_health():
    """Quick validation that both Stripe API keys are configured + working.

    Calls stripe.Account.retrieve() with each key (lightweight, free) and
    pulls the most recent charge as a sanity check. Returns a small HTML
    page with the result for each account.
    """
    try:
        import stripe
    except ImportError:
        return ("stripe package not installed", 500)

    keys_to_check = [
        ("CIRCLE", "STRIPE_CIRCLE_API_KEY"),
        ("DEPOSIT", "STRIPE_DEPOSIT_API_KEY"),
        ("LEGACY (unknown origin)", "STRIPE_API_KEY"),
    ]

    results = []
    for label, env_name in keys_to_check:
        key = os.getenv(env_name, "").strip()
        entry = {"label": label, "env_name": env_name}
        if not key:
            entry["status"] = "missing"
            entry["detail"] = f"{env_name} is not set in env"
            results.append(entry)
            continue

        # Mask key for display (show first 7 + last 4 chars).
        entry["masked_key"] = (key[:7] + "..." + key[-4:]) if len(key) > 12 else "***"

        try:
            account = stripe.Account.retrieve(api_key=key)
            entry["account_id"] = account.get("id", "")
            entry["account_name"] = (
                account.get("settings", {}).get("dashboard", {}).get("display_name")
                or account.get("business_profile", {}).get("name")
                or account.get("email")
                or "(no name)"
            )
            entry["country"] = account.get("country", "")

            # Try to pull the most recent charge to confirm read permission.
            charges = stripe.Charge.list(api_key=key, limit=1)
            if charges.get("data"):
                last = charges["data"][0]
                amt = last.get("amount", 0) / 100
                cur = (last.get("currency") or "").upper()
                created = last.get("created")
                from datetime import datetime
                created_str = (
                    datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M")
                    if created else "?"
                )
                entry["last_charge"] = f"{amt:.2f} {cur} on {created_str}"
            else:
                entry["last_charge"] = "(no charges yet in this account)"

            entry["status"] = "ok"
        except stripe.error.AuthenticationError as e:
            entry["status"] = "auth_error"
            entry["detail"] = str(e)
        except stripe.error.PermissionError as e:
            entry["status"] = "permission_error"
            entry["detail"] = str(e)
        except Exception as e:
            entry["status"] = "error"
            entry["detail"] = f"{type(e).__name__}: {e}"

        results.append(entry)

    refund_enabled = os.getenv("STRIPE_REFUND_ENABLED", "").strip() in ("1", "true", "True", "yes")
    circle_webhook_secret_set = bool(os.getenv("STRIPE_CIRCLE_WEBHOOK_SECRET", "").strip())

    return render_template(
        "admin_stripe_health.html",
        results=results,
        refund_enabled=refund_enabled,
        circle_webhook_secret_set=circle_webhook_secret_set,
        active_section="stripe_health",
        **_admin_layout_context(),
    )


# ═══════════════════════════════════════════════════════════════════
# EMAIL HUB — saved audiences + custom HTML drafts + filtered send
# ═══════════════════════════════════════════════════════════════════
# Sits behind /admin/emails (the page itself is unchanged URL). Lets the
# admin (a) build a saved Audience by combining filter dimensions over
# Ambassador / Reservation / CirclePayment, (b) compose a one-off HTML
# email draft, (c) preview the recipient list, (d) canary to a test
# address, (e) fire the real send. Designed for sales-week workflows
# where the same audience (e.g. "public_unpaid") is reused across many
# different emails.


def resolve_audience(criteria):
    """Apply a criteria dict to Ambassador, return a list of Ambassador rows.

    Supported keys (all optional, omitted = no filter on that dimension):
      source:         "public" | "community"
      has_paid_full:  True (joined CirclePayment.paid_at) / False (not paid)
      has_reservation: True (joined Reservation row exists) / False
      program_choice: "dancers" | "instructors" | "not_sure"  (from Reservation)
      dance_level:    string (exact match on Ambassador.dance_level form answer)
      never_contacted: True (last_outreach_at IS NULL) / False (IS NOT NULL)
      include_unsubscribed: True (rare; default False excludes unsubscribed)
    """
    q = Ambassador.query

    if not criteria.get("include_unsubscribed", False):
        q = q.filter(Ambassador.unsubscribed_at.is_(None))

    src = criteria.get("source")
    if src in ("public", "community"):
        q = q.filter(Ambassador.source == src)

    lvl = criteria.get("dance_level")
    if lvl:
        q = q.filter(Ambassador.dance_level == lvl)

    nc = criteria.get("never_contacted")
    if nc is True:
        q = q.filter(Ambassador.last_outreach_at.is_(None))
    elif nc is False:
        q = q.filter(Ambassador.last_outreach_at.isnot(None))

    base_rows = q.all()

    # Email-keyed filters: Reservation and CirclePayment are joined to
    # Ambassador via email string (no FK column). Do those in Python
    # with single bulk queries up-front so we stay O(N) instead of
    # firing 2 queries per ambassador.
    needs_rsv = (
        criteria.get("has_reservation") is not None
        or criteria.get("program_choice") is not None
    )
    needs_paid = criteria.get("has_paid_full") is not None

    rsv_by_email = {}
    if needs_rsv:
        for r in Reservation.query.all():
            if r.email:
                rsv_by_email.setdefault(r.email.lower(), []).append(r)

    paid_emails = set()
    if needs_paid:
        paid_emails = {
            (cp.email or "").lower()
            for cp in CirclePayment.query
                .filter(CirclePayment.paid_at.isnot(None)).all()
            if cp.email
        }

    has_rsv_flag = criteria.get("has_reservation")
    has_paid_flag = criteria.get("has_paid_full")
    program_choice = criteria.get("program_choice")

    out = []
    for a in base_rows:
        email_l = (a.email or "").lower()

        if has_rsv_flag is not None:
            has = email_l in rsv_by_email if email_l else False
            if has_rsv_flag and not has:
                continue
            if not has_rsv_flag and has:
                continue

        if has_paid_flag is not None:
            has = email_l in paid_emails if email_l else False
            if has_paid_flag and not has:
                continue
            if not has_paid_flag and has:
                continue

        if program_choice:
            rsvs = rsv_by_email.get(email_l, []) if email_l else []
            if not any(r.program_choice == program_choice for r in rsvs):
                continue

        out.append(a)

    return out


def _serialize_audience(a):
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description or "",
        "criteria": a.criteria(),
        "is_preset": bool(a.is_preset),
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _serialize_draft(d):
    return {
        "id": d.id,
        "name": d.name,
        "subject": d.subject or "",
        "body_html": d.body_html or "",
        "last_sent_at": d.last_sent_at.isoformat() if d.last_sent_at else None,
        "last_sent_audience_id": d.last_sent_audience_id,
        "last_sent_count": d.last_sent_count,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _serialize_ambassador_for_preview(a):
    return {
        "id": a.id,
        "name": a.name or "(no name)",
        "email": a.email or "",
        "source": a.source or "",
        "country_code": a.country_code or "",
        "dance_level": a.dance_level or "",
    }


# ── Saved audiences ────────────────────────────────────────────────

@admin_bp.route("/emails/audiences", methods=["GET"])
def email_hub_list_audiences():
    from flask import jsonify
    audiences = SavedAudience.query.order_by(SavedAudience.is_preset.desc(), SavedAudience.name).all()
    return jsonify(audiences=[_serialize_audience(a) for a in audiences])


@admin_bp.route("/emails/audiences", methods=["POST"])
def email_hub_create_audience():
    from flask import jsonify
    import json as _json
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="name_required"), 400
    if SavedAudience.query.filter_by(name=name).first():
        return jsonify(ok=False, error="name_taken"), 409
    criteria = body.get("criteria") or {}
    a = SavedAudience(
        name=name,
        description=(body.get("description") or "").strip() or None,
        criteria_json=_json.dumps(criteria),
        is_preset=False,
    )
    db.session.add(a)
    db.session.commit()
    return jsonify(ok=True, audience=_serialize_audience(a))


@admin_bp.route("/emails/audiences/<int:audience_id>", methods=["PUT", "PATCH"])
def email_hub_update_audience(audience_id):
    from flask import jsonify
    import json as _json
    a = SavedAudience.query.get_or_404(audience_id)
    body = request.get_json(silent=True) or {}
    if "name" in body:
        new_name = (body["name"] or "").strip()
        if not new_name:
            return jsonify(ok=False, error="name_required"), 400
        if new_name != a.name and SavedAudience.query.filter_by(name=new_name).first():
            return jsonify(ok=False, error="name_taken"), 409
        a.name = new_name
    if "description" in body:
        a.description = (body["description"] or "").strip() or None
    if "criteria" in body:
        a.criteria_json = _json.dumps(body["criteria"] or {})
    db.session.commit()
    return jsonify(ok=True, audience=_serialize_audience(a))


@admin_bp.route("/emails/audiences/<int:audience_id>", methods=["DELETE"])
def email_hub_delete_audience(audience_id):
    from flask import jsonify
    a = SavedAudience.query.get_or_404(audience_id)
    if a.is_preset:
        return jsonify(ok=False, error="preset_protected"), 400
    db.session.delete(a)
    db.session.commit()
    return jsonify(ok=True)


@admin_bp.route("/emails/audiences/<int:audience_id>/preview", methods=["POST", "GET"])
def email_hub_preview_audience(audience_id):
    """Resolve a saved audience to a recipient list (id, name, email)."""
    from flask import jsonify
    a = SavedAudience.query.get_or_404(audience_id)
    rows = resolve_audience(a.criteria())
    return jsonify(
        ok=True,
        audience_id=a.id,
        audience_name=a.name,
        total=len(rows),
        recipients=[_serialize_ambassador_for_preview(r) for r in rows],
    )


@admin_bp.route("/emails/audiences/preview-ad-hoc", methods=["POST"])
def email_hub_preview_ad_hoc():
    """Resolve a criteria dict on the fly (without saving). Used by the
    audience builder UI to live-update the recipient count as the admin
    toggles filter checkboxes."""
    from flask import jsonify
    body = request.get_json(silent=True) or {}
    criteria = body.get("criteria") or {}
    rows = resolve_audience(criteria)
    return jsonify(
        ok=True,
        total=len(rows),
        recipients=[_serialize_ambassador_for_preview(r) for r in rows[:500]],  # cap for payload size
        truncated=len(rows) > 500,
    )


# ── Email drafts ───────────────────────────────────────────────────

@admin_bp.route("/emails/drafts", methods=["GET"])
def email_hub_list_drafts():
    from flask import jsonify
    drafts = EmailDraft.query.order_by(EmailDraft.updated_at.desc()).all()
    return jsonify(drafts=[_serialize_draft(d) for d in drafts])


@admin_bp.route("/emails/drafts", methods=["POST"])
def email_hub_create_draft():
    from flask import jsonify
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip() or "Untitled draft"
    d = EmailDraft(
        name=name,
        subject=(body.get("subject") or "").strip(),
        body_html=body.get("body_html") or "",
    )
    db.session.add(d)
    db.session.commit()
    return jsonify(ok=True, draft=_serialize_draft(d))


@admin_bp.route("/emails/drafts/<int:draft_id>", methods=["PUT", "PATCH"])
def email_hub_update_draft(draft_id):
    from flask import jsonify
    d = EmailDraft.query.get_or_404(draft_id)
    body = request.get_json(silent=True) or {}
    if "name" in body:
        d.name = (body["name"] or "").strip() or "Untitled draft"
    if "subject" in body:
        d.subject = (body["subject"] or "").strip()
    if "body_html" in body:
        d.body_html = body["body_html"] or ""
    db.session.commit()
    return jsonify(ok=True, draft=_serialize_draft(d))


@admin_bp.route("/emails/drafts/<int:draft_id>", methods=["DELETE"])
def email_hub_delete_draft(draft_id):
    from flask import jsonify
    d = EmailDraft.query.get_or_404(draft_id)
    db.session.delete(d)
    db.session.commit()
    return jsonify(ok=True)


@admin_bp.route("/emails/drafts/<int:draft_id>/render-preview", methods=["GET"])
def email_hub_render_draft_preview(draft_id):
    """Return the shell-wrapped HTML of a draft for visual preview in a
    new tab (no send). Useful before firing canary or real send."""
    d = EmailDraft.query.get_or_404(draft_id)
    return render_custom_html_preview(d.body_html or "")


@admin_bp.route("/emails/drafts/<int:draft_id>/test-send", methods=["POST"])
def email_hub_send_test(draft_id):
    """Canary send a draft to a specific email address. Does NOT touch
    last_sent_at on the draft (it's a test). Subject is prefixed [TEST].
    """
    from flask import jsonify
    d = EmailDraft.query.get_or_404(draft_id)
    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    if not to or "@" not in to:
        return jsonify(ok=False, error="invalid_email"), 400
    # Use a fake Ambassador-like object so unsubscribe block can still render
    # (or none — _wrap accepts unsubscribe_url=None for tests).
    wrapped = render_custom_html_preview(d.body_html or "")
    sent = bool(_mailer_send(to, f"[TEST] {d.subject or d.name}", wrapped))
    return jsonify(ok=True, sent=sent, to=to)


@admin_bp.route("/emails/drafts/<int:draft_id>/send", methods=["POST"])
def email_hub_send_draft(draft_id):
    """Real send: dispatch the draft to every Ambassador in the audience.

    Body: { audience_id: int, dry_run: bool? }

    dry_run=True returns the recipient list without sending. Real send
    iterates synchronously (per-ambassador unsub-link injection + Resend
    HTTP call) and returns counts.
    """
    from flask import jsonify
    d = EmailDraft.query.get_or_404(draft_id)
    body = request.get_json(silent=True) or {}
    audience_id = body.get("audience_id")
    if not audience_id:
        return jsonify(ok=False, error="audience_required"), 400
    aud = SavedAudience.query.get_or_404(int(audience_id))

    recipients = resolve_audience(aud.criteria())

    if str(body.get("dry_run") or "").strip().lower() in ("1", "true", "yes", "on") or body.get("dry_run") is True:
        return jsonify(
            ok=True, dry_run=True,
            audience_id=aud.id, audience_name=aud.name,
            total=len(recipients),
            recipients=[_serialize_ambassador_for_preview(r) for r in recipients[:500]],
            truncated=len(recipients) > 500,
        )

    if not recipients:
        return jsonify(ok=False, error="empty_audience"), 400

    if not d.subject:
        return jsonify(ok=False, error="subject_required"), 400
    if not (d.body_html or "").strip():
        return jsonify(ok=False, error="body_required"), 400

    sent = 0
    failed = 0
    app_url = current_app.config.get("APP_URL", "") if current_app else ""
    for amb in recipients:
        try:
            ok = send_custom_html_email(
                amb,
                subject=d.subject,
                body_html=d.body_html,
                app_url=app_url,
                template_key=f"draft_{d.id}",
            )
            if ok:
                sent += 1
            else:
                failed += 1
        except Exception:
            logger.exception("email_hub_send_draft: failed for ambassador %s", amb.id)
            failed += 1

    d.last_sent_at = datetime.now(timezone.utc)
    d.last_sent_audience_id = aud.id
    d.last_sent_count = sent
    db.session.commit()

    logger.info(
        "email_hub_send_draft: draft=%s audience=%s sent=%d failed=%d",
        d.id, aud.id, sent, failed,
    )
    return jsonify(
        ok=True, dry_run=False,
        sent=sent, failed=failed, total=len(recipients),
        audience_name=aud.name, draft_name=d.name,
    )


def _seed_default_audiences():
    """Idempotent: ensure the baseline preset audiences exist. Called from
    the /admin/emails view so they materialize on first page load instead
    of requiring a migration step.
    """
    import json as _json
    presets = [
        {
            "name": "public_unpaid",
            "description": "Public-source ambassadors who have not made a reservation AND have not paid the full plan. The natural audience for sales-week outreach.",
            "criteria": {
                "source": "public",
                "has_reservation": False,
                "has_paid_full": False,
                "exclude_unsubscribed": True,
            },
        },
        {
            "name": "public_paid",
            "description": "Public-source ambassadors who already paid the full plan. Use for post-purchase comms.",
            "criteria": {
                "source": "public",
                "has_paid_full": True,
            },
        },
        {
            "name": "public_reserved_not_paid",
            "description": "Public-source ambassadors who put down the €100 deposit but haven't completed the full plan. Closing audience.",
            "criteria": {
                "source": "public",
                "has_reservation": True,
                "has_paid_full": False,
            },
        },
    ]
    created = 0
    for p in presets:
        if SavedAudience.query.filter_by(name=p["name"]).first():
            continue
        a = SavedAudience(
            name=p["name"],
            description=p["description"],
            criteria_json=_json.dumps(p["criteria"]),
            is_preset=True,
        )
        db.session.add(a)
        created += 1
    if created:
        db.session.commit()
        logger.info("seeded %d default audience preset(s)", created)
