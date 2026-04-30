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
from sqlalchemy import func
from app.models import db, Ambassador, Referral, RewardTier, MilestoneNotification, EmailEvent, PendingReferral
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
    champions = [a for a in reachable if a.referral_count >= 5]
    top10 = sorted(reachable, key=lambda a: -a.referral_count)[:10]
    inactive_7d = [a for a in reachable if days_since_last_referral(a) >= 7]
    never_visited = [a for a in reachable if a.last_dashboard_visit_at is None]

    return {
        "cold": cold,                    # 0 unplugs (need a kick)
        "sleeping": sleeping,            # 1-4 unplugs (need momentum)
        "champions": champions,          # 5+ unplugs (lock the prize)
        "top10": top10,                  # current top performers
        "inactive_7d": inactive_7d,      # no activity in 7 days
        "never_visited": never_visited,  # never opened their dashboard
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

    # Geo data for the choropleth — keyed by ISO numeric (matches world-atlas TopoJSON)
    geo = {}
    for code, c in counts:
        numeric = iso_to_numeric(code)
        if numeric:
            name, flag = lookup_country(code)
            geo[numeric] = {
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

    return render_template(
        "admin.html",
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
        pending_review_count=pending_review_count,
        turnstile_stats=turnstile_stats,
        country_dist=country_dist,
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


@admin_bp.route("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("home.index"))
