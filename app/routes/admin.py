import csv
import io
import logging
import threading
from collections import defaultdict
from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, session, current_app, Response,
)
from datetime import datetime, timezone, timedelta
from sqlalchemy import func, or_
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification, EmailEvent, PendingReferral, PrizeDelivery, LeadEvent
from app.mailer import (
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
    _send as _mailer_send,  # low-level Resend POST, used by /admin/broadcast
    # legacy:
    send_first_referral_email,
    send_referral_notification_email,
    send_milestone_email,
    send_almost_there_email,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Marketing helpers — segments + chart data
# ════════════════════════════════════════════════════════════════════

def _compute_segments(ambassadors):
    """Group reachable ambassadors into marketing-relevant buckets.

    Only includes opted-in (unsubscribed_at IS NULL) ambassadors. Each
    segment is a list of Ambassador instances.
    """
    now = datetime.now(timezone.utc)
    reachable = [a for a in ambassadors if a.unsubscribed_at is None]

    def days_since_last_referral(amb):
        if amb.referrals:
            last = max(r.registered_at for r in amb.referrals)
            # SQLite returns naive datetimes; coerce to UTC for math
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return (now - last).days
        created = amb.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).days

    cold = [a for a in reachable if a.referral_count == 0]
    sleeping = [a for a in reachable if 1 <= a.referral_count < 5]
    needs_activation = [a for a in reachable if a.referral_count < 5]  # cold ∪ sleeping
    champions = [a for a in reachable if a.referral_count >= 5]
    top10 = sorted(reachable, key=lambda a: -a.referral_count)[:10]
    inactive_7d = [a for a in reachable if days_since_last_referral(a) >= 7]
    never_visited = [a for a in reachable if a.last_dashboard_visit_at is None]

    return {
        "cold": cold,                          # 0 unplugs (need a kick)
        "sleeping": sleeping,                  # 1-4 unplugs (need momentum)
        "needs_activation": needs_activation,  # 0-4 unplugs (haven't unlocked yet)
        "champions": champions,                # 5+ unplugs (lock the prize)
        "top10": top10,                        # current top performers
        "inactive_7d": inactive_7d,            # no activity in 7 days
        "never_visited": never_visited,        # never opened their dashboard
    }


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


def _compute_chart_data():
    """Return JSON-serialisable data for the admin charts."""
    now = datetime.now(timezone.utc)
    today = now.date()

    # ── Signups timeline (last 14 days, split by source) ──
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    day_keys = [d.isoformat() for d in days]
    counts_by_day = defaultdict(lambda: {"community": 0, "public": 0})
    cutoff = datetime.combine(days[0], datetime.min.time(), tzinfo=timezone.utc)
    for amb in Ambassador.query.filter(Ambassador.created_at >= cutoff).all():
        d = (amb.created_at.date() if amb.created_at.tzinfo else amb.created_at.date()).isoformat()
        counts_by_day[d][amb.source] += 1

    timeline = {
        "labels": [d.strftime("%b %d") for d in days],
        "community": [counts_by_day[k]["community"] for k in day_keys],
        "public": [counts_by_day[k]["public"] for k in day_keys],
    }

    # ── Activity distribution (unplug-count buckets) ──
    all_amb = Ambassador.query.all()
    buckets = {"0": 0, "1-2": 0, "3-4": 0, "5-9": 0, "10+": 0}
    for amb in all_amb:
        c = amb.referral_count
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
    total = len(all_amb)
    welcomed = sum(1 for a in all_amb if a.welcome_sent_at is not None)
    first_unplug = sum(1 for a in all_amb if a.referral_count >= 1)
    five_plus = sum(1 for a in all_amb if a.referral_count >= 5)

    funnel = {
        "labels": ["Registered", "Welcomed", "1+ unplug", "5+ (locked)"],
        "values": [total, welcomed, first_unplug, five_plus],
    }

    return {
        "timeline": timeline,
        "distribution": distribution,
        "funnel": funnel,
    }


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
        "admin_routes": ["overview", "live", "emails", "security", "reach"],
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
    all_amb = Ambassador.query.all()
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
    """
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

    if channel == "all":
        ambassadors = Ambassador.query.all()
    else:
        ambassadors = Ambassador.query.filter_by(source=channel).all()

    if q:
        ambassadors = [
            a for a in ambassadors
            if q in (a.name or "").lower() or q in (a.email or "").lower()
        ]

    sorted_ambassadors = sorted(ambassadors, key=lambda a: a.referral_count, reverse=True)

    # Top-line stats (computed across the FULL dataset, not the filtered view)
    all_amb_for_stats = Ambassador.query.all()
    total_referrals = Referral.query.count()
    community_count = Ambassador.query.filter_by(source="community").count()
    public_count = Ambassador.query.filter_by(source="public").count()
    unsubscribed = Ambassador.query.filter(Ambassador.unsubscribed_at.isnot(None)).count()
    prizes_earned = MilestoneNotification.query.count()
    prizes_pending = MilestoneNotification.query.filter_by(delivered=False).count()

    # Marketing segments + chart data + email stats
    segments = _compute_segments(all_amb_for_stats)
    segment_counts = {k: len(v) for k, v in segments.items()}
    charts = _compute_chart_data()
    email_stats = _compute_email_stats()
    turnstile_stats = _compute_turnstile_stats()
    country_dist = _compute_country_distribution()

    # Engagement: how many ambassadors have opened their dashboard at least once
    visited = sum(1 for a in all_amb_for_stats if a.last_dashboard_visit_at is not None)

    # Fraud risk per ambassador (only for the rows we'll actually render)
    risk_by_id = {a.id: _compute_suspicion(a) for a in sorted_ambassadors}
    high_risk_total = sum(1 for a in all_amb_for_stats if _compute_suspicion(a)["level"] == "high")

    # How many velocity-throttled signups are sitting in the review queue
    pending_review_count = PendingReferral.query.filter_by(status="pending").count()

    layout_ctx = _admin_layout_context()
    layout_ctx["pending_review_count"] = pending_review_count  # already computed below
    return render_template(
        "admin.html",
        page_title="Overview",
        active_section="overview",
        ambassadors=sorted_ambassadors,
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

    # ── TEST MODE: if only_email is provided, restrict the send to that
    # one ambassador. Mirrors the full route's logic exactly (lock,
    # idempotency flag, source-aware copy) so end-to-end is verified.
    only_email = (request.form.get("only_email", "") or "").strip().lower()
    if only_email:
        amb = Ambassador.query.filter(func.lower(Ambassador.email) == only_email).first()
        if amb is None:
            flash(f"Test mode: ambassador with email '{only_email}' not found.", "error")
            return redirect(url_for("admin.emails"))
        targets = [amb]
        # In test mode we deliberately ignore the idempotency flag so the
        # admin can re-trigger the same email on themselves repeatedly.
        # Skip min_age_days too — it's a test, those rules aren't relevant.
        flag = cfg["flag"]
        label = cfg["label"]
        fn = cfg["fn"]
        try:
            ok = fn(amb, current_app.config["APP_URL"])
            flash(
                f"{label} TEST · sent to {amb.email}: " +
                ("✓ delivered to Resend" if ok else "✗ Resend rejected — check logs"),
                "success" if ok else "error",
            )
        except Exception as e:
            logger.exception("test send failed for %s", amb.email)
            flash(f"{label} TEST · error: {e}", "error")
        return redirect(url_for("admin.emails"))

    all_amb = Ambassador.query.all()
    segments = _compute_segments(all_amb)
    targets = segments.get(segment_name, [])
    if not targets:
        flash(f"No ambassadors in segment '{segment_name}'.", "info")
        return redirect(url_for("admin.index"))

    flag = cfg["flag"]
    label = cfg["label"]
    fn = cfg["fn"]
    min_age_days = cfg.get("min_age_days", 0)

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

    if not eligible:
        flash(
            f"{label}: nothing to send. {skipped_already_sent} already received, "
            f"{skipped_too_new} too recent (<{min_age_days} days since signup).",
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

    all_amb = Ambassador.query.all()
    segments = _compute_segments(all_amb)
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
    all_amb = Ambassador.query.all()

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

        flask_app = current_app._get_current_object()

        def _run():
            with flask_app.app_context():
                _GHL_SYNC_STATE["running"] = True
                _GHL_SYNC_STATE["started_at"] = datetime.now(timezone.utc)
                _GHL_SYNC_STATE["finished_at"] = None
                _GHL_SYNC_STATE["stats"] = None
                _GHL_SYNC_STATE["error"] = None
                try:
                    stats = ghl_service.sync_all_contacts(create_missing=True)
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

        button_html = f'''
        <form method="post" style="margin-top:20px;">
          <button type="submit" style="background:#2EDB99; color:#000; border:0; padding:14px 28px; font-family:'Orbitron',sans-serif; font-weight:900; letter-spacing:2px; text-transform:uppercase; cursor:pointer; box-shadow:0 0 16px rgba(46,219,153,0.45); font-size:13px;">▶ Run full sync now</button>
          <p style="color:#6B7280; font-size:11px; margin-top:8px; line-height:1.6;">Pulls every contact from GHL (~1-2 min). Creates ghost leads only for contacts carrying any of: {relevant_tags_html}. Idempotent.</p>
        </form>

        <div style="margin-top:32px; padding:18px; background:rgba(220,38,38,0.08); border:1px solid rgba(220,38,38,0.4); border-radius:6px;">
          <p style="color:#FCA5A5; font-size:12px; letter-spacing:2px; text-transform:uppercase; margin:0 0 10px 0;">▌ Cleanup</p>
          <p style="color:#C9CFD4; font-size:13px; line-height:1.5; margin:0 0 14px 0;">
            Ghost leads from GHL: <strong style="color:#fff;">{ghost_total}</strong> total ·
            <strong style="color:#2EDB99;">{ghost_relevant}</strong> with at least one relevant tag ·
            <strong style="color:#FCA5A5;">{ghost_irrelevant}</strong> with NONE (these shouldn't be in the launch DB).
          </p>
          <form method="post" action="/admin/sync-ghl/cleanup" onsubmit="return confirm('Delete {ghost_irrelevant} ghost leads that don\\'t carry any relevant tag? This will not affect real signups (source=public/community).');">
            <button type="submit" style="background:#DC2626; color:#fff; border:0; padding:10px 20px; font-family:'Share Tech Mono',monospace; font-weight:bold; letter-spacing:1.5px; text-transform:uppercase; cursor:pointer; font-size:11px;">Delete {ghost_irrelevant} non-relevant ghost leads</button>
          </form>
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

@admin_bp.route("/leads")
def leads():
    """Filterable list of leads with temperature scoring + class progress.

    Filters via query params:
      q          — substring search on name/email/phone
      source     — public | community | ghl_import | (any)
      tag        — must contain this tag in ghl_tags
      temp       — cold | cool | warm | hot | burning | customer
      has_phone  — 1 to require phone
      class_min  — 25 | 50 | 75 | 95 (min % watched of any class)
      page       — pagination, 1-indexed
    """
    from app.services.temperature import (
        compute_temperature, fetch_signals_bulk, build_whatsapp_message
    )
    from app.services.ghl import RELEVANT_LEAD_TAGS
    from urllib.parse import quote

    q          = (request.args.get("q") or "").strip().lower()
    source     = (request.args.get("source") or "").strip()
    tag_filter = (request.args.get("tag") or "").strip()
    temp_bucket= (request.args.get("temp") or "").strip().lower()
    has_phone  = request.args.get("has_phone") == "1"
    class_min  = request.args.get("class_min", type=int)
    page       = max(1, request.args.get("page", default=1, type=int))
    per_page   = 50

    # ── Base query: all leads except community by default? ──
    # User wants to see EVERYONE relevant (launch + past + community
    # status as info). So include all sources; let them filter.
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

    # Order by most recent activity (last_dashboard_visit or created_at).
    base = base.order_by(
        Ambassador.last_dashboard_visit_at.desc().nullslast(),
        Ambassador.created_at.desc().nullslast(),
    )

    # Count BEFORE temperature filter (so totals reflect data, not derived
    # filters that would force a full scan).
    total_count = base.count()

    # Pull current page of ambassadors. If a temperature filter is set,
    # we over-fetch and re-page after scoring (acceptable for ~2k rows).
    if temp_bucket or class_min:
        # Bring everyone into memory for scoring + filter + paginate.
        all_rows = base.limit(5000).all()
        ids = [a.id for a in all_rows]
        lead_evts_by_id, email_evts_by_id = fetch_signals_bulk(ids)

        scored = []
        for a in all_rows:
            t = compute_temperature(
                a,
                lead_events=lead_evts_by_id.get(a.id, []),
                email_events=email_evts_by_id.get(a.id, []),
            )
            scored.append((a, t))

        if temp_bucket:
            bucket_map = {
                "cold": "🧊 COLD", "cool": "❄ COOL", "warm": "🌡 WARM",
                "hot": "🚀 HOT", "burning": "🔥 BURNING", "customer": "💎 CUSTOMER",
            }
            target = bucket_map.get(temp_bucket)
            if target:
                scored = [(a, t) for a, t in scored if t["bucket"] == target]
        if class_min:
            scored = [(a, t) for a, t in scored if max(t["max_pct"].values()) >= class_min]

        total_count = len(scored)
        page_rows = scored[(page - 1) * per_page : page * per_page]
        rows_with_temp = page_rows
    else:
        page_amb = base.offset((page - 1) * per_page).limit(per_page).all()
        ids = [a.id for a in page_amb]
        lead_evts_by_id, email_evts_by_id = fetch_signals_bulk(ids) if ids else ({}, {})
        rows_with_temp = [
            (a, compute_temperature(
                a,
                lead_events=lead_evts_by_id.get(a.id, []),
                email_events=email_evts_by_id.get(a.id, []),
            ))
            for a in page_amb
        ]

    # ── Top-of-page stats ──
    stats_overall = {
        "total":       Ambassador.query.count(),
        "with_phone":  Ambassador.query.filter(Ambassador.phone_number.isnot(None)).count(),
        "ghl_imported":Ambassador.query.filter(Ambassador.source == "ghl_import").count(),
        "community":   Ambassador.query.filter(Ambassador.source == "community").count(),
        "public":      Ambassador.query.filter(Ambassador.source == "public").count(),
    }

    pages = max(1, (total_count + per_page - 1) // per_page)

    # Attach pre-computed WhatsApp message URL per row (template-friendly).
    for amb, t in rows_with_temp:
        if amb.phone_number:
            msg = build_whatsapp_message(amb, t)
            t["wa_msg_url"] = quote(msg, safe="")
        else:
            t["wa_msg_url"] = None

    # Country flag helper
    from app.services.phone import lookup_country

    return render_template(
        "admin_leads.html",
        rows=rows_with_temp,
        total_count=total_count,
        stats=stats_overall,
        page=page,
        pages=pages,
        per_page=per_page,
        # Filter values to repopulate the UI
        f_q=q, f_source=source, f_tag=tag_filter,
        f_temp=temp_bucket, f_has_phone=has_phone, f_class_min=class_min,
        relevant_tags=sorted(RELEVANT_LEAD_TAGS),
        lookup_country=lookup_country,
        active_section="leads",
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
            "class_max": {1: 0, 2: 0, 3: 0},
        })
        s["event_count"] += 1
        if e.created_at and (s["first_seen"] is None or e.created_at < s["first_seen"]):
            s["first_seen"] = e.created_at
        if e.created_at and (s["last_seen"] is None or e.created_at > s["last_seen"]):
            s["last_seen"] = e.created_at
        cn = e.class_number
        if cn in (1, 2, 3):
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
          <td style="padding:6px 10px; text-align:center;">{_pct_cell(s["class_max"][3])}</td>
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
  <th style="text-align:center;">Class 3</th>
  <th style="text-align:center;">Events</th>
  <th>Last seen (UTC)</th>
 </tr></thead>
 <tbody>{''.join(summary_rows_html) if summary_rows_html else '<tr><td colspan="7" style="padding:20px; text-align:center; color:#9CA3AF;">No leads yet</td></tr>'}</tbody>
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
