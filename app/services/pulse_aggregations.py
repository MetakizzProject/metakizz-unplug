"""Pulse data aggregations.

Centralizes the SQL/computation for every Pulse dashboard so the route
handlers stay thin (HTTP → call helper → render). Each function returns
a JSON-serializable dict shaped exactly for the template that consumes
it. Add a new dashboard panel = add a function here.

Caching: a tiny in-process TTL memo (`_cached(seconds)`) decorates the
helpers whose underlying queries are expensive enough that re-running
them on every pageload would matter. Activity is intentionally NOT
cached — that page promises "live" data. Per-worker, no Redis.
"""
from __future__ import annotations

import time
from functools import wraps


def _cached(ttl_seconds: int):
    """In-process TTL memo for parameterless aggregation functions.

    The cache is per Python process (per Gunicorn worker on Render),
    NOT cross-worker. Acceptable here: Pulse is admin-only, traffic is
    tiny, eventual consistency within `ttl_seconds` is fine. If the
    user opens a Pulse page and immediately reloads, the second load
    is instant; new signups will appear after `ttl_seconds`.
    """
    def decorator(fn):
        state = {"value": None, "expires": 0.0}
        @wraps(fn)
        def wrapped():
            now = time.time()
            if state["value"] is not None and now < state["expires"]:
                return state["value"]
            value = fn()
            state["value"] = value
            state["expires"] = now + ttl_seconds
            return value
        wrapped.cache_clear = lambda: state.update({"value": None, "expires": 0.0})
        return wrapped
    return decorator


# Stubs for each page — filled in during the page-specific iterations.
# Keeping them here so route handlers can already import them, and the
# acquisition/conversion/revenue/activity templates have a single place
# to look for "where does this number come from".


@_cached(ttl_seconds=60)
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

    # ─── Country distribution ──────────────────────────────────
    # Group by Ambassador.country_code (ISO 3166-1 alpha-2). Top 15 +
    # bucket the rest into "other".
    from app.services.phone import lookup_country
    country_rows = (
        db.session.query(
            Ambassador.country_code, func.count(Ambassador.id).label("cnt"),
        )
        .filter(Ambassador.country_code.isnot(None))
        .filter(Ambassador.country_code != "")
        .group_by(Ambassador.country_code)
        .order_by(func.count(Ambassador.id).desc())
        .all()
    )
    country_dist = []
    total_with_country = sum(c[1] for c in country_rows)
    for iso, cnt in country_rows[:15]:
        name, flag = lookup_country(iso)
        country_dist.append({
            "iso": iso,
            "name": name,
            "flag": flag,
            "count": cnt,
            "share_pct": round(cnt * 100.0 / total_with_country, 1) if total_with_country else 0.0,
        })
    if len(country_rows) > 15:
        other_cnt = sum(c[1] for c in country_rows[15:])
        country_dist.append({
            "iso": "—",
            "name": f"Other ({len(country_rows) - 15} countries)",
            "flag": "🌐",
            "count": other_cnt,
            "share_pct": round(other_cnt * 100.0 / total_with_country, 1) if total_with_country else 0.0,
        })

    # ─── Per-source funnel ─────────────────────────────────────
    # For each top-5 source bucket, compute step counts:
    #   signups  → watched ≥1 class → attended live → paid €100 → paid full
    #
    # We re-walk ambassadors in Python because the source bucket key is
    # derived (classify_source). For ~2.9k rows this is sub-second.
    # Watched-class and live-attended come from LeadEvents; paid-deposit
    # from Reservation; paid-full from CirclePayment.
    from app.models import LeadEvent, Reservation, CirclePayment

    # Bulk fetch the signal sets (one query each, indexed by lowercased
    # email so we never iterate inside the per-ambassador loop).
    watched_emails = {
        r[0].lower() for r in (
            db.session.query(LeadEvent.email)
            .filter(LeadEvent.email.isnot(None))
            .filter(LeadEvent.event_type.like("class%_progress_%"))
            .distinct().all()
        ) if r[0]
    }
    live_emails = {
        r[0].lower() for r in (
            db.session.query(LeadEvent.email)
            .filter(LeadEvent.email.isnot(None))
            .filter(LeadEvent.event_type == "webinar_joined")
            .distinct().all()
        ) if r[0]
    }
    paid_emails = {
        r[0].lower() for r in (
            db.session.query(Reservation.email)
            .filter(Reservation.paid_at.isnot(None))
            .filter(Reservation.email.isnot(None))
            .distinct().all()
        ) if r[0]
    }
    full_emails = {
        r[0].lower() for r in (
            db.session.query(CirclePayment.email)
            .filter(CirclePayment.email.isnot(None))
            .distinct().all()
        ) if r[0]
    }

    # Walk all ambassadors (not just last-30d) to compute the full-history
    # funnel by source. Use a separate query that includes email.
    funnel_rows = Ambassador.query.with_entities(
        Ambassador.email,
        Ambassador.utm_source, Ambassador.utm_medium, Ambassador.utm_campaign,
        Ambassador.fbclid, Ambassador.gclid, Ambassador.ttclid,
    ).all()

    bucket_stats = {}  # bk → {signups, watched, live, paid, full}
    for r in funnel_rows:
        email = (r[0] or "").lower()
        info = classify_source(_Shim(r[1:]))
        bk, blabel, bcolor = _bucket_for_chart(info["key"])
        if bk not in bucket_stats:
            bucket_stats[bk] = {
                "label": blabel, "color": bcolor,
                "signups": 0, "watched": 0, "live": 0, "paid": 0, "full": 0,
            }
        s = bucket_stats[bk]
        s["signups"] += 1
        if email and email in watched_emails: s["watched"] += 1
        if email and email in live_emails:    s["live"] += 1
        if email and email in paid_emails:    s["paid"] += 1
        if email and email in full_emails:    s["full"] += 1

    funnel_by_source = sorted(
        bucket_stats.values(),
        key=lambda b: -b["signups"],
    )[:5]
    # Add conversion % per step (relative to signups so all bars share a base).
    for b in funnel_by_source:
        base = b["signups"] or 1
        b["watched_pct"] = round(b["watched"] * 100 / base, 1)
        b["live_pct"]    = round(b["live"]    * 100 / base, 1)
        b["paid_pct"]    = round(b["paid"]    * 100 / base, 1)
        b["full_pct"]    = round(b["full"]    * 100 / base, 1)

    return {
        "total_leads": total,
        "new_7d": new_7d,
        "new_prev_7d": new_prev_7d,
        "delta_7d": delta,
        "delta_7d_pct": round(delta_pct, 1),
        "source_breakdown": source_breakdown,
        "timeline_30d_by_source": timeline_30d,
        "top_referrers": top_referrers,
        "country_distribution": country_dist,
        "funnel_by_source": funnel_by_source,
    }


@_cached(ttl_seconds=60)
def conversion_summary() -> dict:
    """KPIs for /admin/pulse/conversion. Returns:
      {
        "funnel": {steps: [{label, count, pct_of_total, dropoff_pct, color, key}], visited: {1,2,3}},
        "temperature_dist": [{key, label, color, count, pct}, ...],
        "avg_time_to_deposit_days": float | None,
        "avg_time_to_full_days":    float | None,
        "queue": {burning_uncontacted, hot_uncontacted, contacted_today, in_queue_total},
      }

    Funnel reuses `_compute_launch_funnel()` from admin.py so the
    counts match /admin/leads_insights exactly. Temperature distribution
    uses `_build_email_buckets()` (same classifier as the temp filter
    on /admin/leads).
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from collections import defaultdict
    from app.models import db, Ambassador, Reservation, CirclePayment
    from app.services.temperature import BUCKET_LABELS
    from app.routes.admin import _compute_launch_funnel, _build_email_buckets

    now = datetime.now(timezone.utc)
    total = Ambassador.query.count()

    # ── Funnel ──
    funnel_data = _compute_launch_funnel(total)

    # ── Temperature distribution ──
    # _build_email_buckets relies on the `purchase_completed` LeadEvent
    # which isn't always written when a CirclePayment row lands. Overlay
    # CirclePayment.email → customer so the dashboard reflects reality.
    buckets = dict(_build_email_buckets())
    for (em,) in (
        db.session.query(CirclePayment.email)
        .filter(CirclePayment.email.isnot(None))
        .distinct().all()
    ):
        if em:
            buckets[em.lower()] = "customer"

    temp_counts = defaultdict(int)
    for em, b in buckets.items():
        temp_counts[b] += 1
    # cold = total - everyone-with-any-event (since cold ambassadors have
    # no events and aren't in the bucket dict). We surface cold separately
    # so the distribution adds up to total leads.
    untracked = total - len(buckets)
    if untracked > 0:
        temp_counts["cold"] = temp_counts.get("cold", 0) + untracked

    temp_total = sum(temp_counts.values()) or 1
    bucket_order = ["customer", "burning", "hot", "warm", "cool", "cold"]
    temperature_dist = []
    for key in bucket_order:
        cnt = temp_counts.get(key, 0)
        label, color = BUCKET_LABELS.get(key, (key, "#9CA3AF"))
        temperature_dist.append({
            "key": key,
            "label": label,
            "color": color,
            "count": cnt,
            "pct": round(cnt * 100.0 / temp_total, 1),
        })

    # ── Avg time-to-deposit (days from Ambassador.created_at → Reservation.paid_at) ──
    paid_rows = (
        db.session.query(Ambassador.created_at, Reservation.paid_at)
        .join(Reservation, func.lower(Reservation.email) == func.lower(Ambassador.email))
        .filter(Reservation.paid_at.isnot(None))
        .filter(Ambassador.created_at.isnot(None))
        .all()
    )
    def _aware(dt):
        return dt if (dt and dt.tzinfo) else (dt.replace(tzinfo=timezone.utc) if dt else None)
    deltas_deposit = []
    for created, paid in paid_rows:
        c = _aware(created); p = _aware(paid)
        if c and p and p >= c:
            deltas_deposit.append((p - c).total_seconds() / 86400.0)
    avg_to_deposit = round(sum(deltas_deposit) / len(deltas_deposit), 1) if deltas_deposit else None

    # ── Avg time-to-full-plan (created_at → CirclePayment.paid_at) ──
    full_rows = (
        db.session.query(Ambassador.created_at, CirclePayment.paid_at)
        .join(CirclePayment, func.lower(CirclePayment.email) == func.lower(Ambassador.email))
        .filter(CirclePayment.paid_at.isnot(None))
        .filter(Ambassador.created_at.isnot(None))
        .all()
    )
    deltas_full = []
    for created, paid in full_rows:
        c = _aware(created); p = _aware(paid)
        if c and p and p >= c:
            deltas_full.append((p - c).total_seconds() / 86400.0)
    avg_to_full = round(sum(deltas_full) / len(deltas_full), 1) if deltas_full else None

    # ── Outreach action queue ──
    one_day_ago = now - timedelta(hours=24)
    contacted_today = Ambassador.query.filter(
        Ambassador.last_outreach_at >= one_day_ago,
    ).count()

    burning_emails = {em for em, b in buckets.items() if b == "burning"}
    hot_emails = {em for em, b in buckets.items() if b == "hot"}
    def _uncontacted_count(email_set):
        if not email_set:
            return 0
        return (
            Ambassador.query
            .filter(func.lower(Ambassador.email).in_(email_set))
            .filter(Ambassador.last_outreach_at.is_(None))
            .filter(Ambassador.unsubscribed_at.is_(None))
            .count()
        )
    burning_uncontacted = _uncontacted_count(burning_emails)
    hot_uncontacted = _uncontacted_count(hot_emails)

    # ── Weekly cohort retention ────────────────────────────────
    # Per signup week (last 8 weeks), count signups + how many ever
    # converted to deposit / full. NOT a true day-7/14/30 active model
    # (that would need walking LeadEvents per row); this is a simpler
    # "did this cohort eventually pay?" view which is the question
    # Alvaro actually asks when looking at lead quality by week.
    cutoff_8w = now - timedelta(weeks=8)
    cohort_rows = (
        db.session.query(Ambassador.id, Ambassador.email, Ambassador.created_at)
        .filter(Ambassador.created_at >= cutoff_8w)
        .all()
    )
    # All deposit + full email sets, lowercased
    deposit_emails = {
        r[0].lower() for r in (
            db.session.query(Reservation.email)
            .filter(Reservation.paid_at.isnot(None))
            .filter(Reservation.email.isnot(None))
            .distinct().all()
        ) if r[0]
    }
    full_emails_cohort = {
        r[0].lower() for r in (
            db.session.query(CirclePayment.email)
            .filter(CirclePayment.email.isnot(None))
            .distinct().all()
        ) if r[0]
    }

    def _iso_week(dt):
        if dt is None: return None
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        # Anchor on Monday of that week
        d = dt.date()
        monday = d - timedelta(days=d.weekday())
        return monday

    cohort_map = {}  # week_monday → {signups, deposit, full}
    for amb_id, em, created in cohort_rows:
        wk = _iso_week(created)
        if wk is None:
            continue
        if wk not in cohort_map:
            cohort_map[wk] = {"signups": 0, "deposit": 0, "full": 0}
        cohort_map[wk]["signups"] += 1
        em_low = (em or "").lower()
        if em_low in deposit_emails: cohort_map[wk]["deposit"] += 1
        if em_low in full_emails_cohort: cohort_map[wk]["full"] += 1

    cohorts = []
    for wk in sorted(cohort_map.keys(), reverse=True):
        c = cohort_map[wk]
        base = c["signups"] or 1
        cohorts.append({
            "week": wk.isoformat(),
            "label": wk.strftime("%b %-d"),
            "signups": c["signups"],
            "deposit": c["deposit"],
            "deposit_pct": round(c["deposit"] * 100 / base, 1),
            "full": c["full"],
            "full_pct": round(c["full"] * 100 / base, 1),
        })

    return {
        "funnel": funnel_data,
        "temperature_dist": temperature_dist,
        "avg_time_to_deposit_days": avg_to_deposit,
        "avg_time_to_full_days": avg_to_full,
        "queue": {
            "burning_uncontacted": burning_uncontacted,
            "hot_uncontacted": hot_uncontacted,
            "contacted_today": contacted_today,
            "in_queue_total": burning_uncontacted + hot_uncontacted,
        },
        "cohorts": cohorts,
    }


@_cached(ttl_seconds=60)
def revenue_summary() -> dict:
    """KPIs for /admin/pulse/revenue. Returns:
      {
        "cash_collected_net_cents": int,
        "cash_gross_cents":         int,
        "total_billed_cents":       int,
        "deposits_in_cents":        int,
        "full_in_cents":            int,
        "refunds_out_cents":        int,
        "deposits_paid_count":      int,
        "full_paid_count":          int,
        "refund_count":             int,
        "deposit_to_full_pct":      float,
        "revenue_by_program":   [{label, cents, count}, ...],
        "revenue_by_plan":      [{label, cents, count}, ...],
        "timeline_30d":         {labels: [...], deposits: [...], full: [...]},
      }

    Reuses the exact same formula as /admin/reservations (NET cash =
    deposits + full − refunds) so numbers reconcile when comparing.
    """
    from datetime import datetime, timedelta, timezone
    from collections import defaultdict
    from app.models import db, Reservation, CirclePayment

    now = datetime.now(timezone.utc)

    # ── Headline ───────────────────────────────────────────────
    paid_res = Reservation.query.filter(Reservation.paid_at.isnot(None)).all()
    deposits_in_cents = sum(r.amount_cents or 0 for r in paid_res)
    deposits_count = len(paid_res)

    cps = CirclePayment.query.all()
    full_in_cents = sum(cp.amount_cents or 0 for cp in cps)
    full_count = len(cps)

    refunded = [r for r in paid_res if r.refund_status == "success"]
    refunds_out_cents = sum(r.refund_amount_cents or 0 for r in refunded)
    refund_count = len(refunded)

    cash_gross = deposits_in_cents + full_in_cents
    cash_net = cash_gross - refunds_out_cents

    # Total billed = sum of CirclePayment amounts that have an invoice
    # sent (matches the /admin/invoices billing card).
    total_billed_cents = sum(
        (cp.amount_cents or 0) for cp in cps if cp.invoice_sent_at
    )

    # Deposit-to-full conversion rate: of the unique-email deposit payers,
    # what % also have a CirclePayment row (full plan).
    deposit_emails = {(r.email or "").lower() for r in paid_res if r.email}
    full_emails = {(cp.email or "").lower() for cp in cps if cp.email}
    converted = deposit_emails & full_emails
    base = len(deposit_emails) or 1
    deposit_to_full_pct = round(len(converted) * 100.0 / base, 1)

    # ── Revenue by program (combines program_choice + modality_choice) ──
    program_map = {
        ("dancers", "solo"):       ("Solo Dancer", "#2EDB99"),
        ("dancers", "duo"):        ("Couple Dancer", "#A78BFA"),
        ("instructors", "solo"):   ("Solo Instructor", "#F97316"),
        ("instructors", "duo"):    ("Couple Instructor", "#DC2626"),
    }
    by_program = defaultdict(lambda: {"cents": 0, "count": 0})
    for r in paid_res:
        key = ((r.program_choice or "").lower(), (r.modality_choice or "").lower())
        label_color = program_map.get(key)
        if not label_color:
            continue
        by_program[key]["cents"] += (r.amount_cents or 0)
        by_program[key]["count"] += 1
        # Add the associated CirclePayment (if matched by email)
    # Now overlay CirclePayment amounts per program: pair CP to a
    # Reservation by email (most common case) and add to its program.
    res_by_email = {(r.email or "").lower(): r for r in paid_res if r.email}
    for cp in cps:
        em = (cp.email or "").lower()
        r = res_by_email.get(em)
        if not r:
            continue
        key = ((r.program_choice or "").lower(), (r.modality_choice or "").lower())
        if key not in program_map:
            continue
        by_program[key]["cents"] += (cp.amount_cents or 0)

    revenue_by_program = []
    for key, label_color in program_map.items():
        label, color = label_color
        d = by_program.get(key, {"cents": 0, "count": 0})
        revenue_by_program.append({
            "label": label,
            "color": color,
            "cents": d["cents"],
            "count": d["count"],
        })
    revenue_by_program.sort(key=lambda x: -x["cents"])

    # ── Revenue by payment plan (1× vs 6×) ──
    plan_map = {
        "one_payment":      ("Plan 1× · single payment", "#A78BFA"),
        "six_installments": ("Plan 6× · installments",   "#F97316"),
        "not_sure":         ("Undecided",                "#6B7280"),
    }
    by_plan = defaultdict(lambda: {"cents": 0, "count": 0})
    for r in paid_res:
        key = (r.payment_plan or "").lower()
        if key not in plan_map:
            continue
        by_plan[key]["cents"] += (r.amount_cents or 0)
        by_plan[key]["count"] += 1
    for cp in cps:
        em = (cp.email or "").lower()
        r = res_by_email.get(em)
        if not r:
            continue
        key = (r.payment_plan or "").lower()
        if key not in plan_map:
            continue
        by_plan[key]["cents"] += (cp.amount_cents or 0)

    revenue_by_plan = []
    for key, label_color in plan_map.items():
        label, color = label_color
        d = by_plan.get(key, {"cents": 0, "count": 0})
        revenue_by_plan.append({
            "label": label,
            "color": color,
            "cents": d["cents"],
            "count": d["count"],
        })

    # ── Timeline 30d ──
    cutoff = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
    today = now.date()
    days = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
    day_index = {d: i for i, d in enumerate(days)}
    deposits_series = [0] * 30
    full_series = [0] * 30
    for r in paid_res:
        d = r.paid_at.date() if r.paid_at else None
        idx = day_index.get(d) if d else None
        if idx is not None:
            deposits_series[idx] += (r.amount_cents or 0)
    for cp in cps:
        d = cp.paid_at.date() if cp.paid_at else None
        idx = day_index.get(d) if d else None
        if idx is not None:
            full_series[idx] += (cp.amount_cents or 0)
    timeline = {
        "labels": [d.strftime("%b %-d") for d in days],
        "deposits": deposits_series,
        "full": full_series,
    }

    return {
        "cash_collected_net_cents": cash_net,
        "cash_gross_cents": cash_gross,
        "total_billed_cents": total_billed_cents,
        "deposits_in_cents": deposits_in_cents,
        "full_in_cents": full_in_cents,
        "refunds_out_cents": refunds_out_cents,
        "deposits_paid_count": deposits_count,
        "full_paid_count": full_count,
        "refund_count": refund_count,
        "deposit_to_full_pct": deposit_to_full_pct,
        "revenue_by_program": revenue_by_program,
        "revenue_by_plan": revenue_by_plan,
        "timeline_30d": timeline,
    }


@_cached(ttl_seconds=15)
def activity_summary() -> dict:
    """KPIs for /admin/pulse/activity. Returns:
      {
        "last_24h": {signups, deposits, full_purchases, emails_sent, opens, clicks},
        "last_24h_prev": {...same shape...},   # day-before-yesterday baseline
        "outreach_today": {contacted, in_queue},
        "latest_webinar": {name, attendees, avg_duration_min} | None,
      }

    All counts are over rolling 24h windows, NOT midnight-aligned.
    Comparison uses the immediately-prior 24h block as baseline.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    from app.models import (
        db, Ambassador, Reservation, CirclePayment,
        EmailEvent, LeadEvent,
    )
    from app.routes.admin import _build_email_buckets

    now = datetime.now(timezone.utc)
    h24 = now - timedelta(hours=24)
    h48 = now - timedelta(hours=48)

    # Signups (Ambassador.created_at)
    signups_24h = Ambassador.query.filter(Ambassador.created_at >= h24).count()
    signups_prev = (
        Ambassador.query
        .filter(Ambassador.created_at >= h48)
        .filter(Ambassador.created_at < h24)
        .count()
    )

    # Deposit paid (Reservation.paid_at)
    deposits_24h = Reservation.query.filter(Reservation.paid_at >= h24).count()
    deposits_prev = (
        Reservation.query
        .filter(Reservation.paid_at >= h48)
        .filter(Reservation.paid_at < h24)
        .count()
    )

    # Full purchases (CirclePayment.paid_at OR created_at as fallback)
    full_24h = CirclePayment.query.filter(CirclePayment.paid_at >= h24).count()
    full_prev = (
        CirclePayment.query
        .filter(CirclePayment.paid_at >= h48)
        .filter(CirclePayment.paid_at < h24)
        .count()
    )

    # Email events
    def _email_count(et, since, until):
        q = EmailEvent.query.filter(EmailEvent.event_type == et).filter(EmailEvent.created_at >= since)
        if until is not None:
            q = q.filter(EmailEvent.created_at < until)
        return q.count()

    emails_sent_24h = _email_count("sent", h24, None)
    emails_sent_prev = _email_count("sent", h48, h24)
    opens_24h = _email_count("opened", h24, None)
    opens_prev = _email_count("opened", h48, h24)
    clicks_24h = _email_count("clicked", h24, None)
    clicks_prev = _email_count("clicked", h48, h24)

    # Outreach status (rolling 24h)
    contacted_24h = (
        Ambassador.query
        .filter(Ambassador.last_outreach_at >= h24)
        .count()
    )
    buckets = _build_email_buckets()
    burning_hot = {em for em, b in buckets.items() if b in ("burning", "hot")}
    in_queue = 0
    if burning_hot:
        in_queue = (
            Ambassador.query
            .filter(func.lower(Ambassador.email).in_(burning_hot))
            .filter(Ambassador.last_outreach_at.is_(None))
            .filter(Ambassador.unsubscribed_at.is_(None))
            .count()
        )

    # Latest webinar (any webinar_joined event in last 7 days)
    last_week = now - timedelta(days=7)
    recent_webinar = (
        db.session.query(
            func.count(LeadEvent.id).label("attendees"),
            func.avg(LeadEvent.webinar_duration_min).label("avg_dur"),
            func.max(LeadEvent.created_at).label("when"),
        )
        .filter(LeadEvent.event_type == "webinar_joined")
        .filter(LeadEvent.created_at >= last_week)
        .first()
    )
    latest_webinar = None
    if recent_webinar and recent_webinar.attendees and recent_webinar.attendees > 0:
        latest_webinar = {
            "attendees": int(recent_webinar.attendees),
            "avg_duration_min": round(float(recent_webinar.avg_dur or 0), 1),
            "when": recent_webinar.when.isoformat() if recent_webinar.when else None,
        }

    return {
        "last_24h": {
            "signups": signups_24h,
            "deposits": deposits_24h,
            "full_purchases": full_24h,
            "emails_sent": emails_sent_24h,
            "opens": opens_24h,
            "clicks": clicks_24h,
        },
        "last_24h_prev": {
            "signups": signups_prev,
            "deposits": deposits_prev,
            "full_purchases": full_prev,
            "emails_sent": emails_sent_prev,
            "opens": opens_prev,
            "clicks": clicks_prev,
        },
        "outreach_today": {
            "contacted": contacted_24h,
            "in_queue": in_queue,
        },
        "latest_webinar": latest_webinar,
    }


def activity_feed(limit: int = 30) -> list:
    """Recent activity events for the live feed on /admin/pulse/activity.
    Each event: {"ts": iso, "type": "signup|deposit|full|email", "actor", "detail"}
    Pulled from 4 sources (Ambassador.created_at, Reservation.paid_at,
    CirclePayment.paid_at, EmailEvent of type opened|clicked|sent) and
    merged + sorted desc.
    """
    from datetime import datetime, timedelta, timezone
    from app.models import Ambassador, Reservation, CirclePayment, EmailEvent

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    events = []

    for a in (
        Ambassador.query
        .filter(Ambassador.created_at >= cutoff)
        .order_by(Ambassador.created_at.desc())
        .limit(limit).all()
    ):
        if not a.created_at: continue
        events.append({
            "ts": a.created_at.isoformat(),
            "type": "signup",
            "actor": a.name or a.email or "(unknown)",
            "detail": f"signed up · {a.email or ''}",
        })

    for r in (
        Reservation.query
        .filter(Reservation.paid_at >= cutoff)
        .order_by(Reservation.paid_at.desc())
        .limit(limit).all()
    ):
        if not r.paid_at: continue
        events.append({
            "ts": r.paid_at.isoformat(),
            "type": "deposit",
            "actor": r.name or r.email or "(unknown)",
            "detail": f"paid €100 deposit · {r.program_choice or '?'} / {r.modality_choice or '?'}",
        })

    for cp in (
        CirclePayment.query
        .filter(CirclePayment.paid_at >= cutoff)
        .order_by(CirclePayment.paid_at.desc())
        .limit(limit).all()
    ):
        if not cp.paid_at: continue
        events.append({
            "ts": cp.paid_at.isoformat(),
            "type": "full",
            "actor": cp.customer_name or cp.email or "(unknown)",
            "detail": f"full plan · €{(cp.amount_cents or 0)/100:,.0f}",
        })

    for ev in (
        EmailEvent.query
        .filter(EmailEvent.created_at >= cutoff)
        .filter(EmailEvent.event_type.in_(("opened", "clicked")))
        .order_by(EmailEvent.created_at.desc())
        .limit(limit * 2).all()
    ):
        if not ev.created_at: continue
        events.append({
            "ts": ev.created_at.isoformat(),
            "type": f"email_{ev.event_type}",
            "actor": ev.to_email or "(unknown)",
            "detail": f"{ev.template_key or 'email'} · {ev.event_type}",
        })

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]
