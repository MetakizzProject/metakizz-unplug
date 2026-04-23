"""
Cron-driven email dispatch logic.

All functions are idempotent: a send sets a per-email timestamp column on the
Ambassador (e.g. activation_nudge_sent_at), and the audience query excludes
anyone who already has that timestamp set. Safe to re-run any endpoint N times.

The 6 cron-driven emails:
    #2 Activation Nudge   — 48h after signup if count=0                (run daily)
    #5 Midway Reminder    — 7 days after signup if >=5 days to close   (run daily)
    #6 Final 48h          — one-shot at 2026-05-05 19:00 Europe/Madrid
    #7 Last 6 Hours       — one-shot at 2026-05-07 13:00 Madrid, count IN (3,4)
    #8 Results            — one-shot at 2026-05-08 10:00 Madrid, all active
    #9 You Won            — one-shot at 2026-05-08 10:30 Madrid, prize winners

The "real-time" emails (#1 Welcome, #3 First Unplug, #4 Guaranteed Prize) do
NOT live here — they fire from app.services.signup.
"""

import logging
from datetime import datetime, timedelta, timezone
from flask import current_app
from app.models import db, Ambassador
from app.mailer import (
    send_activation_nudge_email,
    send_midway_reminder_email,
    send_final_48h_email,
    send_last_6h_email,
    send_results_announcement_email,
    send_you_won_email,
)
from app.services.signup import _rank_in_bucket

logger = logging.getLogger(__name__)


def _close_datetime():
    """Parse the campaign close datetime from config (timezone-aware)."""
    return datetime.fromisoformat(current_app.config["CAMPAIGN_CLOSE_ISO"])


def _now_utc():
    return datetime.now(timezone.utc)


def _is_past_close():
    """True once the campaign close timestamp has passed."""
    return _now_utc() >= _close_datetime()


def _days_until_close():
    """Whole days between now and close (can be negative if past close)."""
    delta = _close_datetime() - _now_utc()
    return delta.days  # integer days, rounds toward zero for negative


def _eligible_for_email(amb):
    """Base eligibility gate: not unsubscribed."""
    return amb.unsubscribed_at is None


# ─── JOB: Daily check (runs activation nudge + midway) ─────────────

def dispatch_daily():
    """Run the two per-user time-based jobs.

    - Activation Nudge: created_at >= 48h ago AND count=0 AND not yet sent.
      Skipped entirely if less than 24h remain until close (would be noise).
    - Midway Reminder: created_at >= 7d ago AND >=5 days to close AND not yet sent.
    """
    app_url = current_app.config["APP_URL"]
    now = _now_utc()
    close = _close_datetime()
    hours_to_close = (close - now).total_seconds() / 3600.0

    stats = {"activation_sent": 0, "activation_failed": 0,
             "midway_sent": 0, "midway_failed": 0}

    # --- Activation Nudge ---
    if hours_to_close < 24:
        logger.info("skipping activation nudge: <24h to close")
    else:
        cutoff_48h = now - timedelta(hours=48)
        candidates = Ambassador.query.filter(
            Ambassador.created_at <= cutoff_48h,
            Ambassador.activation_nudge_sent_at.is_(None),
            Ambassador.unsubscribed_at.is_(None),
        ).all()
        # Filter count=0 in Python (referral_count is a property, not a column)
        candidates = [a for a in candidates if a.referral_count == 0]
        for amb in candidates:
            try:
                if send_activation_nudge_email(amb, app_url):
                    amb.activation_nudge_sent_at = now
                    db.session.commit()
                    stats["activation_sent"] += 1
                else:
                    stats["activation_failed"] += 1
            except Exception:
                logger.exception("activation nudge failed for %s", amb.email)
                stats["activation_failed"] += 1

    # --- Midway Reminder ---
    if (close - now).days < 5:
        logger.info("skipping midway reminder: <5 days to close")
    else:
        cutoff_7d = now - timedelta(days=7)
        candidates = Ambassador.query.filter(
            Ambassador.created_at <= cutoff_7d,
            Ambassador.midway_sent_at.is_(None),
            Ambassador.unsubscribed_at.is_(None),
        ).all()
        days_left = max(0, (close - now).days)
        for amb in candidates:
            try:
                rank = _rank_in_bucket(amb)
                if send_midway_reminder_email(amb, rank, days_left, app_url):
                    amb.midway_sent_at = now
                    db.session.commit()
                    stats["midway_sent"] += 1
                else:
                    stats["midway_failed"] += 1
            except Exception:
                logger.exception("midway reminder failed for %s", amb.email)
                stats["midway_failed"] += 1

    return stats


# ─── JOB: Final 48h (one-shot, 5 may 19:00 Madrid) ─────────────────

def dispatch_final_48h():
    """Send #6 Final 48h to all active ambassadors who haven't received it."""
    app_url = current_app.config["APP_URL"]
    now = _now_utc()
    stats = {"sent": 0, "failed": 0}

    candidates = Ambassador.query.filter(
        Ambassador.final_48h_sent_at.is_(None),
        Ambassador.unsubscribed_at.is_(None),
    ).all()

    # Need per-ambassador rank + gap_to_top3
    for amb in candidates:
        try:
            rank = _rank_in_bucket(amb)
            # Compute gap_to_top3: how many more unplugs to enter top 3
            bucket = sorted(
                Ambassador.query.filter_by(source=amb.source).all(),
                key=lambda a: (-a.referral_count, a.created_at),
            )
            third_count = bucket[2].referral_count if len(bucket) >= 3 else 0
            gap = max(0, third_count - amb.referral_count + 1)
            if send_final_48h_email(amb, rank, gap, app_url):
                amb.final_48h_sent_at = now
                db.session.commit()
                stats["sent"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            logger.exception("final_48h failed for %s", amb.email)
            stats["failed"] += 1
    return stats


# ─── JOB: Last 6 Hours (one-shot, 7 may 13:00 Madrid) ──────────────

def dispatch_last_6h():
    """Send #7 Last 6h ONLY to ambassadors with count IN (3, 4)."""
    app_url = current_app.config["APP_URL"]
    now = _now_utc()
    stats = {"sent": 0, "failed": 0}

    candidates = Ambassador.query.filter(
        Ambassador.last_6h_sent_at.is_(None),
        Ambassador.unsubscribed_at.is_(None),
    ).all()
    candidates = [a for a in candidates if a.referral_count in (3, 4)]

    for amb in candidates:
        try:
            if send_last_6h_email(amb, app_url):
                amb.last_6h_sent_at = now
                db.session.commit()
                stats["sent"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            logger.exception("last_6h failed for %s", amb.email)
            stats["failed"] += 1
    return stats


# ─── JOB: Results (one-shot, 8 may 10:00 Madrid) ───────────────────

def dispatch_results():
    """Send #8 Results to all active ambassadors."""
    app_url = current_app.config["APP_URL"]
    now = _now_utc()
    stats = {"sent": 0, "failed": 0}

    # Compute campaign totals + global top 3 (first names only)
    all_active = Ambassador.query.filter(Ambassador.unsubscribed_at.is_(None)).all()
    total_ambassadors = Ambassador.query.count()
    total_unplugs = sum(a.referral_count for a in all_active)
    total_countries = 27  # per memory; could be computed if profile has country
    sorted_all = sorted(all_active, key=lambda a: (-a.referral_count, a.created_at))
    top3 = [
        {
            "name": (a.name.strip().split()[0] if a.name and a.name.strip() else "?"),
            "count": a.referral_count,
        }
        for a in sorted_all[:3]
    ]

    candidates = [a for a in all_active if a.results_sent_at is None]

    for amb in candidates:
        try:
            if send_results_announcement_email(
                amb, total_ambassadors, total_unplugs, total_countries, top3, app_url
            ):
                amb.results_sent_at = now
                db.session.commit()
                stats["sent"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            logger.exception("results failed for %s", amb.email)
            stats["failed"] += 1
    return stats


# ─── JOB: You Won (one-shot, 8 may 10:30 Madrid) ───────────────────

def dispatch_you_won():
    """Send #9 You Won to any prize winner (5+ unplugs OR in top 3 of their bucket)."""
    app_url = current_app.config["APP_URL"]
    now = _now_utc()
    stats = {"sent": 0, "failed": 0}

    # Precompute top 3 per bucket
    top3_ids_by_source = {}
    for source in ("community", "public"):
        bucket = sorted(
            Ambassador.query.filter_by(source=source).all(),
            key=lambda a: (-a.referral_count, a.created_at),
        )
        top3_ids_by_source[source] = {a.id: i + 1 for i, a in enumerate(bucket[:3])}

    candidates = Ambassador.query.filter(
        Ambassador.you_won_sent_at.is_(None),
        Ambassador.unsubscribed_at.is_(None),
    ).all()

    for amb in candidates:
        try:
            position = top3_ids_by_source.get(amb.source, {}).get(amb.id)  # 1, 2, 3, or None
            has_guaranteed = amb.referral_count >= 5
            if not has_guaranteed and position is None:
                continue  # didn't win anything
            # send_you_won_email figures out the rama internally based on count + position
            if send_you_won_email(amb, position, app_url):
                amb.you_won_sent_at = now
                db.session.commit()
                stats["sent"] += 1
            else:
                stats["failed"] += 1
        except Exception:
            logger.exception("you_won failed for %s", amb.email)
            stats["failed"] += 1
    return stats
