"""Pulse data aggregations.

Centralizes the SQL/computation for every Pulse dashboard so the route
handlers stay thin (HTTP → call helper → render). Each function returns
a JSON-serializable dict shaped exactly for the template that consumes
it. Add a new dashboard panel = add a function here.

Caching strategy: in-process memo with short TTL via functools.lru_cache
patterns is intentionally NOT used yet — Pulse pages will be hit by one
operator (Álvaro) so even uncached queries are fine. Add caching only
when a specific aggregation gets slow.
"""
from __future__ import annotations

# Stubs for each page — filled in during the page-specific iterations.
# Keeping them here so route handlers can already import them, and the
# acquisition/conversion/revenue/activity templates have a single place
# to look for "where does this number come from".


def acquisition_summary() -> dict:
    """KPIs for /admin/pulse/acquisition. Returns:
      {
        "total_leads": int,
        "new_7d": int,
        "new_prev_7d": int,
        "delta_7d": int,           # new_7d - new_prev_7d
        "delta_7d_pct": float,     # vs prev 7d, signed
        "source_breakdown": [{key, label, emoji, count, share_pct}, ...],
      }

    Reuses `classify_source()` from temperature.py so the bucketing is
    identical to /admin/leads `?origin=` filter and source distribution
    counters. No extra DB schema needed.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from app.models import db, Ambassador
    from app.services.temperature import classify_source, SOURCE_BUCKETS

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_14d = now - timedelta(days=14)

    total = Ambassador.query.count()
    new_7d = (
        Ambassador.query
        .filter(Ambassador.created_at >= cutoff_7d)
        .count()
    )
    new_prev_7d = (
        Ambassador.query
        .filter(Ambassador.created_at >= cutoff_14d)
        .filter(Ambassador.created_at < cutoff_7d)
        .count()
    )
    delta = new_7d - new_prev_7d
    delta_pct = (delta / new_prev_7d * 100) if new_prev_7d else (100.0 if new_7d else 0.0)

    # Per-source count via Python classify_source (~2800 rows is sub-second).
    # If/when this gets slow, push it down to SQL by precomputing the
    # source key as a generated column on the ambassadors table.
    by_key = {key: 0 for key, _ in SOURCE_BUCKETS}
    meta = {key: {"label": label, "emoji": ""} for key, label in SOURCE_BUCKETS}
    rows = Ambassador.query.with_entities(
        Ambassador.utm_source, Ambassador.utm_medium, Ambassador.utm_campaign,
        Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid,
    ).all()

    # classify_source expects the ambassador-like object — adapt via shim.
    class _Shim:
        __slots__ = ("utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid", "ttclid")
        def __init__(self, r):
            self.utm_source, self.utm_medium, self.utm_campaign = r[0], r[1], r[2]
            self.fbclid, self.gclid, self.ttclid = r[3], r[4], r[5]

    for r in rows:
        info = classify_source(_Shim(r))
        key = info["key"]
        by_key[key] = by_key.get(key, 0) + 1
        if key not in meta:
            meta[key] = {"label": info["label"], "emoji": info["emoji"]}
        elif not meta[key]["emoji"]:
            meta[key]["emoji"] = info["emoji"]

    # Materialize sorted, with share % out of total. Show only non-zero.
    total_for_share = sum(by_key.values()) or 1
    source_breakdown = []
    for key, count in sorted(by_key.items(), key=lambda kv: -kv[1]):
        if count == 0:
            continue
        # Strip emoji prefix from canonical label like "📸 Instagram"
        # if present, since meta carries the emoji separately.
        raw_label = meta[key]["label"]
        emoji = meta[key]["emoji"] or ""
        if raw_label.startswith(emoji + " ") and emoji:
            label = raw_label[len(emoji) + 1:]
        else:
            label = raw_label
        source_breakdown.append({
            "key": key,
            "label": label,
            "emoji": emoji,
            "count": count,
            "share_pct": round(count * 100.0 / total_for_share, 1),
        })

    # ─── Timeline 30d stacked by source ────────────────────────
    # Last 30 calendar days, count signups/day, broken out by source.
    cutoff_30d = now - timedelta(days=30)
    recent_rows = (
        Ambassador.query.with_entities(
            Ambassador.created_at,
            Ambassador.utm_source, Ambassador.utm_medium, Ambassador.utm_campaign,
            Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid,
        )
        .filter(Ambassador.created_at >= cutoff_30d)
        .all()
    )
    # Build a contiguous date axis (last 30 days inclusive of today).
    today = now.date()
    days = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    day_index = {d: i for i, d in enumerate(days)}

    # Bucket sources to a smaller set for chart readability.
    # Merge variants ("instagram" + "instagram_ad" → "Instagram", etc).
    def _bucket_for_chart(key):
        if key.startswith("instagram"):
            return ("instagram", "Instagram", "#E1306C")
        if key.startswith("facebook"):
            return ("facebook", "Facebook", "#1877F2")
        if key.startswith("google"):
            return ("google", "Google", "#FBBC04")
        if key.startswith("tiktok"):
            return ("tiktok", "TikTok", "#FE2C55")
        if key == "referral":
            return ("referral", "Referral", "#A78BFA")
        if key == "email":
            return ("email", "Email", "#60A5FA")
        if key == "direct":
            return ("direct", "Direct", "#9CA3AF")
        return ("other", "Other", "#6B7280")

    series_buckets = {}  # bucket_key → {label, color, values:[30]}
    for r in recent_rows:
        ts = r[0]
        if ts is None:
            continue
        d = ts.date() if hasattr(ts, "date") else ts
        idx = day_index.get(d)
        if idx is None:
            continue
        info = classify_source(_Shim(r[1:]))
        bk, blabel, bcolor = _bucket_for_chart(info["key"])
        if bk not in series_buckets:
            series_buckets[bk] = {"label": blabel, "color": bcolor, "values": [0] * 30}
        series_buckets[bk]["values"][idx] += 1

    # Sort series by total descending so the biggest sits at the bottom
    # of the stack (Chart.js renders datasets bottom-up).
    series_sorted = sorted(
        series_buckets.values(),
        key=lambda s: -sum(s["values"]),
    )
    timeline_30d = {
        "labels": [d.strftime("%b %-d") for d in days],
        "series": series_sorted,
    }

    # ─── Top referrers ─────────────────────────────────────────
    # Group referrals by ambassador, top 10 with at least 1 referral.
    from app.models import Referral
    referrer_rows = (
        db.session.query(
            Referral.ambassador_id,
            func.count(Referral.id).label("cnt"),
        )
        .group_by(Referral.ambassador_id)
        .order_by(func.count(Referral.id).desc())
        .limit(10)
        .all()
    )
    top_amb_ids = [r[0] for r in referrer_rows]
    amb_lookup = {}
    if top_amb_ids:
        for a in Ambassador.query.filter(Ambassador.id.in_(top_amb_ids)).all():
            amb_lookup[a.id] = a
    top_referrers = []
    for amb_id, cnt in referrer_rows:
        a = amb_lookup.get(amb_id)
        if not a:
            continue
        top_referrers.append({
            "id": a.id,
            "name": a.name or "(no name)",
            "email": a.email or "",
            "count": cnt,
        })

    return {
        "total_leads": total,
        "new_7d": new_7d,
        "new_prev_7d": new_prev_7d,
        "delta_7d": delta,
        "delta_7d_pct": round(delta_pct, 1),
        "source_breakdown": source_breakdown,
        "timeline_30d_by_source": timeline_30d,
        "top_referrers": top_referrers,
    }


def conversion_summary() -> dict:
    """KPIs for /admin/pulse/conversion. Returns:
      {
        "funnel": [{label, count, drop_pct}, ...],
        "temperature_dist": [{bucket, count, pct, color}, ...],
        "avg_time_to_deposit_days": float | None,
        "avg_time_to_full_days":    float | None,
        "queue": {burning_uncontacted: int, hot_uncontacted: int, contacted_today: int},
        "cohorts": {weeks: [...], rows: [{week, signups, day7, day14, day30}]},
      }
    """
    return {}


def revenue_summary() -> dict:
    """KPIs for /admin/pulse/revenue. Returns:
      {
        "cash_collected_net_cents": int,
        "total_billed_cents":       int,
        "deposits_in_cents":        int,
        "full_in_cents":            int,
        "refunds_out_cents":        int,
        "deposit_to_full_pct":      float,
        "revenue_by_program":   [{label, cents}, ...],
        "revenue_by_plan":      [{label, cents}, ...],
        "timeline_30d":         {labels: [...], values: [...]},
      }
    """
    return {}


def activity_summary() -> dict:
    """KPIs for /admin/pulse/activity. Returns:
      {
        "last_24h": {signups: int, deposits: int, full_purchases: int,
                     emails_sent: int, opens: int, clicks: int},
        "outreach_today": {contacted: int, in_queue: int},
        "latest_webinar": {name: str, attendees: int, avg_duration_min: float} | None,
      }
    """
    return {}


def activity_feed(limit: int = 30) -> list:
    """Returns a chronologically-ordered list of recent events for the
    real-time feed on /admin/pulse/activity. Each event:
      {"ts": iso, "type": "signup|deposit|full_purchase|email_open|email_click",
       "actor": "name", "detail": "human-readable"}
    """
    return []
