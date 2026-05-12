"""Lead temperature scoring.

Combines signals from across our DB to produce a 0-N score per lead,
plus a human-readable bucket / color. Higher score = more engaged.

Signal weights are tunable from one place (TEMP_WEIGHTS). After the
launch we'll calibrate these against actual conversion data.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Any


TEMP_WEIGHTS = {
    # Passive signals — capped low. Opening an email is barely intent.
    "email_opened":          3,
    "email_opened_cap":      15,
    "email_clicked":         8,    # click = active interest
    "email_clicked_cap":     32,
    "dashboard_visit":       4,    # came back to check status
    "dashboard_visit_cap":   24,
    # Active brand-building — they brought us a new person.
    "referral_brought":      25,
    # Class viewing — the strongest behavioural intent during the launch.
    # Each class fully watched = 45 pts. All 3 fully = 135 pts.
    "class_25":              10,
    "class_50":              18,
    "class_75":              30,
    "class_95_or_complete":  45,
    # Past content — they already invested attention with us once.
    "past_masterclass":      20,
    # Live webinar attendance — tiered by duration_min when available.
    # Someone who stayed 60+ min is far hotter than someone who joined
    # for 2 min; previous binary +80 didn't discriminate.
    "webinar_attended_brief":  15,   # < 10 min · clicked join, bounced
    "webinar_attended_short":  40,   # 10-30 min · gave us part of an evening
    "webinar_attended_long":   70,   # 30-60 min · most of the live
    "webinar_attended_full":  100,   # 60+ min · sat through the whole thing
    "webinar_attended_unknown": 60,  # joined but no duration captured (CSV path)
    # Reservation paid — proof of purchase intent for MKOT 3.0.
    # Sits between "burning intent" and "customer" — these people put
    # €100 down. Auto-promotes their bucket to at least burning.
    "reservation_paid":      120,
    # Purchase — auto-bucket → Customer regardless of score.
    "purchase_completed":    150,
}


# Bucket thresholds. Kept for post-launch calibration when we have real
# conversion data — currently NOT used to assign buckets (the launch-day
# event-presence classifier `bucket_from_event_set` drives both the temp
# filter and the per-row badge so they always agree). Score is still
# computed and used for sorting WITHIN a bucket.
TEMP_THRESHOLDS = {
    "cold":    (0, 14),
    "cool":    (15, 39),
    "warm":    (40, 79),
    "hot":     (80, 159),
    "burning": (160, 10_000),
}


# Display labels + colors for each bucket key. Single source of truth so
# filter cards, distribution counters, and per-row badges all render the
# same emoji + color for the same key.
BUCKET_LABELS = {
    "cold":     ("🧊 COLD",     "#6B7280"),
    "cool":     ("❄ COOL",      "#60A5FA"),
    "warm":     ("🌡 WARM",     "#FFC857"),
    "hot":      ("🚀 HOT",      "#F97316"),
    "burning":  ("🔥 BURNING",  "#DC2626"),
    "customer": ("💎 CUSTOMER", "#A78BFA"),
}


# ────────────────────────────────────────────────────────────────────
# Canonical event-type predicates per class.
#
# Single source of truth for "what counts as Started / Completed /
# Visited a class". Used by /admin/leads funnel, /admin/leads PLF
# counters, /admin/leads/insights funnel, and any future caller.
# Don't compute these definitions inline anywhere else — call these.
#
# Intentional choices:
# - STARTED requires ≥25% watched, NOT just `class{n}_viewed` (page-load).
#   Page-loaders are tracked separately as "Visited" so they can be
#   reported but don't pollute the engagement funnel.
# - COMPLETED includes both `progress_95` and `completed`, tolerating
#   the occasional missed `completed` event at video end (browser tab
#   closed, network blip, etc.).
# ────────────────────────────────────────────────────────────────────

def class_started_event_types(class_n: int):
    """Event types that count as 'started Class N' (≥25% watched).
    Excludes class{n}_viewed which is page-load only."""
    return [
        f"class{class_n}_progress_25",
        f"class{class_n}_progress_50",
        f"class{class_n}_progress_75",
        f"class{class_n}_progress_95",
        f"class{class_n}_completed",
    ]


def class_completed_event_types(class_n: int):
    """Event types that count as 'completed Class N' (≥95% watched).
    Tolerates the explicit `completed` event not firing at video end."""
    return [
        f"class{class_n}_progress_95",
        f"class{class_n}_completed",
    ]


def class_visited_event_types(class_n: int):
    """Event types that mean 'opened the Class N page but didn't engage'.
    Just the page-load fire — separate metric from Started so we can
    distinguish curious page-loaders from real watchers."""
    return [f"class{class_n}_viewed"]


def bucket_from_event_set(event_types, has_paid_reservation: bool = False) -> str:
    """Classify a lead's temperature bucket from the SET of event_types
    they have. Launch-day-friendly: any class_completed promotes to
    burning (used to require 2+). Used by:
      - The temperature filter on /admin/leads (`?temp=burning`)
      - Distribution counters on /admin/leads + /admin/leads/insights
      - Per-row badge in compute_temperature()

    Returns one of: cold | cool | warm | hot | burning | customer.

    `has_paid_reservation` is an out-of-band signal (joined from the
    Reservation table by email). When True, bumps the bucket to at
    least "burning" — putting €100 down is a stronger commitment than
    any class progress.
    """
    evts = event_types if isinstance(event_types, set) else set(event_types or [])
    if "purchase_completed" in evts:
        return "customer"
    if has_paid_reservation:
        return "burning"
    if "webinar_joined" in evts:
        return "burning"
    if any(f"class{n}_completed" in evts for n in (1, 2, 3)):
        return "burning"
    if any(f"class{n}_progress_{p}" in evts for n in (1, 2, 3) for p in (75, 95)):
        return "hot"
    if any(f"class{n}_progress_50" in evts for n in (1, 2, 3)):
        return "warm"
    if any(f"class{n}_progress_25" in evts or f"class{n}_viewed" in evts
           for n in (1, 2, 3)):
        return "cool"
    return "cold"


def _pct_from_event(event_type: str, pct_field: Optional[int]) -> int:
    """Best-known progress % implied by a single LeadEvent."""
    if pct_field is not None:
        try:
            return int(pct_field)
        except (TypeError, ValueError):
            pass
    if not event_type:
        return 0
    et = event_type
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


def compute_max_pct_per_class(lead_events) -> Dict[int, int]:
    """Walk an iterable of LeadEvent rows for one ambassador and return
    {1: 0..100, 2: 0..100, 3: 0..100} with the max % achieved per class.

    Class 3 is the live-masterclass replay (uploaded to Bunny Stream
    after the live). Same event taxonomy as 1 and 2.
    """
    out = {1: 0, 2: 0, 3: 0}
    for e in lead_events:
        cn = e.class_number
        if cn not in (1, 2, 3):
            # class_number column may be NULL on legacy rows — fall back
            # to parsing the event_type prefix.
            ev = e.event_type or ""
            if ev.startswith("class") and len(ev) >= 6:
                try:
                    cn = int(ev[5])
                except ValueError:
                    continue
            else:
                continue
            if cn not in (1, 2, 3):
                continue
        pct = _pct_from_event(e.event_type or "", e.pct)
        if pct > out[cn]:
            out[cn] = pct
    return out


def _compute_recency_bonus(lead_events, ambassador, email_events):
    """Returns (bonus_pts, reason_str, last_activity_at) for the recency
    boost on a lead's heat score.

    Looks at the most recent timestamp across:
    - Any LeadEvent (class views, webinar_joined, etc.)
    - Ambassador.last_dashboard_visit_at
    - Most recent EmailEvent of type 'opened' or 'clicked' (passive but
      still a recency signal — they at least looked at the inbox)

    Bonus tiers:
      < 24h → +30 pts, "active in last 24h"
      < 72h → +15 pts, "active in last 3 days"
      < 7d  →  +5 pts, "active this week"
      else  → 0

    The bonus is ADDITIVE, not multiplicative — a stale burning lead
    (score 150) still ranks above a recent cold lead (30 + 30 = 60).
    But two leads with similar base scores get reordered by recency,
    which is what the action queue needs.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    def _aware(dt):
        if dt is None:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    candidates = []
    for e in lead_events or []:
        ts = _aware(getattr(e, "created_at", None))
        if ts:
            candidates.append(ts)
    dash = _aware(getattr(ambassador, "last_dashboard_visit_at", None))
    if dash:
        candidates.append(dash)
    for ev in email_events or []:
        if getattr(ev, "event_type", None) in ("opened", "clicked"):
            ts = _aware(getattr(ev, "created_at", None))
            if ts:
                candidates.append(ts)

    if not candidates:
        return (0, None, None)

    last_activity = max(candidates)
    delta_hours = (now - last_activity).total_seconds() / 3600.0
    if delta_hours < 24:
        return (30, "active in last 24h", last_activity)
    if delta_hours < 72:
        return (15, "active in last 3 days", last_activity)
    if delta_hours < 24 * 7:
        return (5, "active this week", last_activity)
    return (0, None, last_activity)


def compute_views_per_class(lead_events) -> Dict[int, int]:
    """Counts distinct `class{N}_viewed` LeadEvent rows per class.
    Each row is one play session — rewatches naturally bump the count
    because the importer doesn't dedup on (email, event_type).
    """
    out = {1: 0, 2: 0, 3: 0}
    for e in lead_events:
        ev = e.event_type or ""
        if not ev.endswith("_viewed"):
            continue
        if not ev.startswith("class"):
            continue
        try:
            cn = int(ev[5])
        except (IndexError, ValueError):
            continue
        if cn in out:
            out[cn] += 1
    return out


def compute_temperature(
    ambassador,
    lead_events: Optional[List[Any]] = None,
    email_events: Optional[List[Any]] = None,
    referral_count: Optional[int] = None,
    webinar_duration_min: Optional[int] = None,
    has_paid_reservation: bool = False,
) -> Dict[str, Any]:
    """Score a single ambassador using all available signals.

    Pass pre-fetched lead_events and email_events for that ambassador to
    avoid N+1 queries (recommended when scoring many leads at once).

    `referral_count` lets the caller pre-resolve the count via a single
    SQL aggregation (see admin._get_referral_counts) so this function
    never touches the lazy `Ambassador.referral_count` property. With
    ~2500 leads on the insights page that prevents 2500 extra queries.

    Returns a dict:
      {
        "score":      int total points (used for sorting within a bucket),
        "bucket":     label "🧊 COLD" | "❄ COOL" | "🌡 WARM" | "🚀 HOT" | "🔥 BURNING" | "💎 CUSTOMER",
        "bucket_key": "cold" | "cool" | "warm" | "hot" | "burning" | "customer",
        "color":      hex color for the badge,
        "signals":    list of human-readable contributing signals,
        "max_pct":    {1: int, 2: int} per-class progress
      }

    Bucket assignment uses bucket_from_event_set() — the same classifier
    that drives the temperature filter on /admin/leads — so the filter
    and the displayed badge always agree.
    """
    lead_events = lead_events or []
    email_events = email_events or []

    score = 0
    signals = []

    # ── Email engagement ──
    opens = sum(1 for e in email_events if e.event_type == "opened")
    clicks = sum(1 for e in email_events if e.event_type == "clicked")
    if opens:
        pts = min(opens * TEMP_WEIGHTS["email_opened"], TEMP_WEIGHTS["email_opened_cap"])
        score += pts
        signals.append(f"opened {opens} email{'s' if opens != 1 else ''}")
    if clicks:
        pts = min(clicks * TEMP_WEIGHTS["email_clicked"], TEMP_WEIGHTS["email_clicked_cap"])
        score += pts
        signals.append(f"clicked {clicks} link{'s' if clicks != 1 else ''}")

    # ── Dashboard visits ──
    visits = ambassador.dashboard_visit_count or 0
    if visits:
        pts = min(visits * TEMP_WEIGHTS["dashboard_visit"], TEMP_WEIGHTS["dashboard_visit_cap"])
        score += pts
        signals.append(f"{visits} dashboard visit{'s' if visits != 1 else ''}")

    # ── Referrals brought (real signups attributed) ──
    # Prefer the explicit `referral_count` arg when caller pre-resolved
    # it (bulk scoring); fall back to the lazy property only for the
    # single-row use case where the N+1 doesn't matter.
    if referral_count is not None:
        refs = referral_count
    else:
        refs = ambassador.referral_count or 0
    if refs:
        pts = refs * TEMP_WEIGHTS["referral_brought"]
        score += pts
        signals.append(f"brought {refs} referral{'s' if refs != 1 else ''}")

    # ── Class video progress ──
    max_pct = compute_max_pct_per_class(lead_events)
    for cn, pct in max_pct.items():
        if pct >= 95:
            score += TEMP_WEIGHTS["class_95_or_complete"]
            signals.append(f"finished class {cn}")
        elif pct >= 75:
            score += TEMP_WEIGHTS["class_75"]
            signals.append(f"watched {pct}% of class {cn}")
        elif pct >= 50:
            score += TEMP_WEIGHTS["class_50"]
            signals.append(f"watched {pct}% of class {cn}")
        elif pct >= 25:
            score += TEMP_WEIGHTS["class_25"]
            signals.append(f"watched {pct}% of class {cn}")

    # ── Webinar attendance (tiered by duration_min) ──
    # Caller can pre-resolve duration via bulk_webinar_durations() to
    # avoid an extra query per row. If left None, fall back to the
    # webinar_duration_min on the latest webinar_joined LeadEvent in
    # `lead_events` (which is what /admin/leads already pre-fetches).
    joined_webinar = any(e.event_type == "webinar_joined" for e in lead_events)
    if webinar_duration_min is None and joined_webinar:
        # Try to read it from the lead_events we already have.
        for e in lead_events:
            if e.event_type == "webinar_joined" and getattr(e, "webinar_duration_min", None):
                webinar_duration_min = e.webinar_duration_min
                break
    if webinar_duration_min is not None and webinar_duration_min > 0:
        if webinar_duration_min >= 60:
            score += TEMP_WEIGHTS["webinar_attended_full"]
            signals.append(f"attended live ({webinar_duration_min}m)")
        elif webinar_duration_min >= 30:
            score += TEMP_WEIGHTS["webinar_attended_long"]
            signals.append(f"attended live ({webinar_duration_min}m)")
        elif webinar_duration_min >= 10:
            score += TEMP_WEIGHTS["webinar_attended_short"]
            signals.append(f"attended live ({webinar_duration_min}m)")
        else:
            score += TEMP_WEIGHTS["webinar_attended_brief"]
            signals.append(f"attended live ({webinar_duration_min}m, brief)")
    elif joined_webinar:
        # No duration captured — keep the binary signal.
        score += TEMP_WEIGHTS["webinar_attended_unknown"]
        signals.append("attended live")

    # ── Reservation paid (€100 deposit for MKOT 3.0) ──
    if has_paid_reservation:
        score += TEMP_WEIGHTS["reservation_paid"]
        signals.append("paid €100 reservation")

    # ── Purchase ──
    if any(e.event_type == "purchase_completed" for e in lead_events):
        score += TEMP_WEIGHTS["purchase_completed"]
        signals.append("PURCHASED")

    # ── Past masterclass (warm signal from tags) ──
    tags_csv = (ambassador.ghl_tags or "").lower()
    if "masterclass march17th" in tags_csv:
        score += TEMP_WEIGHTS["past_masterclass"]
        signals.append("attended past masterclass")

    # ── Recency bonus ── (additive, surfaces fresh activity at top of queue)
    # A lead who watched class 2 today is more actionable than one who
    # finished classes 3 weeks ago. Bonus boosts to-do queue ordering
    # without disturbing the bucket assignment (event-presence based).
    recency_pts, recency_reason, last_activity_at = _compute_recency_bonus(
        lead_events, ambassador, email_events,
    )
    if recency_pts:
        score += recency_pts
        if recency_reason:
            signals.append(recency_reason)

    # ── Bucket ── (event-presence classification, see bucket_from_event_set)
    # Score is preserved for sorting within a bucket, but bucket assignment
    # uses the same classifier as the temperature filter so the filter and
    # the per-row badge always agree. Reservation paid promotes to burning.
    bucket_key = bucket_from_event_set(
        {e.event_type for e in lead_events},
        has_paid_reservation=has_paid_reservation,
    )
    bucket, color = BUCKET_LABELS[bucket_key]

    return {
        "score": score,
        "bucket": bucket,
        "bucket_key": bucket_key,
        "color": color,
        "signals": signals,
        "max_pct": max_pct,
        "views_per_class": compute_views_per_class(lead_events),
        "webinar_duration_min": webinar_duration_min,
        "has_paid_reservation": has_paid_reservation,
        "recency_bonus": recency_pts,
        "last_activity_at": last_activity_at,
    }


def bulk_webinar_durations(ambassadors):
    """One SQL: returns (by_amb_id, by_email_lower) tuple of dicts.

    Both dicts map to the MAX webinar_duration_min for that key.
    Caller resolves with: `by_amb_id.get(a.id) or by_email.get(em_lower)`.

    Why two paths: the Zoom rematch pass linked guest attendees to
    ambassadors via ambassador_id but left LeadEvent.email empty (Zoom
    Meetings don't capture guest emails). An email-only join would miss
    those ~169 attendees. Resolving by ambassador_id first AND email as
    fallback covers both pathways with one query.
    """
    from app.models import LeadEvent
    from sqlalchemy import func, or_
    if not ambassadors:
        return ({}, {})
    amb_ids = [a.id for a in ambassadors if a.id]
    emails_lower = [(a.email or "").lower() for a in ambassadors if a.email]
    if not amb_ids and not emails_lower:
        return ({}, {})
    conds = []
    if amb_ids:
        conds.append(LeadEvent.ambassador_id.in_(amb_ids))
    if emails_lower:
        conds.append(func.lower(LeadEvent.email).in_(emails_lower))
    rows = (
        LeadEvent.query
        .filter(LeadEvent.event_type == "webinar_joined")
        .filter(LeadEvent.webinar_duration_min.isnot(None))
        .filter(or_(*conds))
        .all()
    )
    by_amb, by_em = {}, {}
    for r in rows:
        dur = r.webinar_duration_min or 0
        if r.ambassador_id and (r.ambassador_id not in by_amb or dur > by_amb[r.ambassador_id]):
            by_amb[r.ambassador_id] = dur
        if r.email:
            em = r.email.lower()
            if em not in by_em or dur > by_em[em]:
                by_em[em] = dur
    return (by_amb, by_em)


def bulk_paid_reservations(ambassadors):
    """One SQL: returns (paid_amb_ids_set, paid_email_set) tuple.

    Resolves by Reservation.ambassador_id first (typed link from Stripe
    webhook when set) AND by case-insensitive email match (fallback for
    typo'd emails or Stripe-only flows). Caller checks both:
        if a.id in paid_amb_ids or em_lower in paid_emails: ...
    """
    from app.models import Reservation
    from sqlalchemy import func, or_
    if not ambassadors:
        return (set(), set())
    amb_ids = [a.id for a in ambassadors if a.id]
    emails_lower = [(a.email or "").lower() for a in ambassadors if a.email]
    if not amb_ids and not emails_lower:
        return (set(), set())
    conds = []
    if amb_ids:
        conds.append(Reservation.ambassador_id.in_(amb_ids))
    if emails_lower:
        conds.append(func.lower(Reservation.email).in_(emails_lower))
    rows = (
        Reservation.query
        .filter(Reservation.paid_at.isnot(None))
        .filter(or_(*conds))
        .with_entities(Reservation.ambassador_id, Reservation.email)
        .all()
    )
    paid_amb_ids = set()
    paid_emails = set()
    for amb_id, em in rows:
        if amb_id:
            paid_amb_ids.add(amb_id)
        if em:
            paid_emails.add(em.lower())
    return (paid_amb_ids, paid_emails)


# ── Canonical class-event taxonomy ──────────────────────────────────
# Single source of truth for "every class event type we care about
# in funnel/temperature/segment/exclude calculations". Any place that
# previously hardcoded a list of `class1_viewed, class2_viewed, ...`
# strings should import this constant instead. Adding `class4` later
# means extending this once and propagating automatically.
ALL_CLASS_EVENT_TYPES = []
for _cn in (1, 2, 3):
    ALL_CLASS_EVENT_TYPES += class_visited_event_types(_cn)
    ALL_CLASS_EVENT_TYPES += class_started_event_types(_cn)
    # class_completed_event_types overlaps with progress_95/completed
    # already covered by class_started_event_types — explicit add is
    # idempotent because we wrap in set() at use sites.
del _cn


def fetch_signals_bulk(ambassador_ids, max_ids: int = 500):
    """Pre-fetch LeadEvents and EmailEvents for a list of ambassador IDs in
    two queries, then return:
        (lead_events_by_id, email_events_by_id)

    PERF: explicitly defer the heavy `extra` TEXT columns (raw JSON
    payloads up to ~5KB per row). With 30k+ events in prod that field
    alone could pile up to 150MB+ of data we never read, blowing past
    Render's worker memory and dragging the page into a 500.

    Soft cap (`max_ids`, default 500) prevents accidental "load events
    for all 2,500 ambassadors" callers from regressing. Callers that
    legitimately need everyone (e.g. /admin/leads/insights global stats)
    should pass max_ids=None to opt out of the cap.
    """
    from sqlalchemy.orm import defer
    from app.models import LeadEvent, EmailEvent

    if max_ids is not None and len(ambassador_ids) > max_ids:
        ambassador_ids = list(ambassador_ids)[:max_ids]

    lead_evts = (
        LeadEvent.query
        .options(defer(LeadEvent.extra))
        .filter(LeadEvent.ambassador_id.in_(ambassador_ids))
        .all()
    )
    email_evts = (
        EmailEvent.query
        .options(defer(EmailEvent.extra))
        .filter(EmailEvent.ambassador_id.in_(ambassador_ids))
        .all()
    )

    by_id_lead = defaultdict(list)
    by_id_email = defaultdict(list)
    for e in lead_evts:
        by_id_lead[e.ambassador_id].append(e)
    for e in email_evts:
        by_id_email[e.ambassador_id].append(e)
    return by_id_lead, by_id_email


def classify_source(ambassador) -> Dict[str, str]:
    """Bucket the lead's origin into a coarse category for filtering.

    Returns {key, label, emoji} where key is the filter value used in
    URLs and label/emoji are for display.

    Detection order matters — we check the most specific signals first.
    """
    src = (ambassador.utm_source or "").lower()
    med = (ambassador.utm_medium or "").lower()
    camp = (ambassador.utm_campaign or "").lower()
    fbclid = bool(ambassador.fbclid)
    gclid = bool(ambassador.gclid)
    ttclid = bool(ambassador.ttclid)

    is_paid = (
        any(k in med for k in ("cpc", "paid", "ads", "ad ")) or
        med in ("ad", "paid")
    )

    # Paid ad platforms first (most actionable category)
    if "tiktok" in src or ttclid:
        return {"key": "tiktok_ad" if is_paid else "tiktok",
                "label": "TikTok" + (" Ad" if is_paid else ""),
                "emoji": "🎵"}
    if "google" in src or gclid:
        return {"key": "google_ad" if is_paid or gclid else "google",
                "label": "Google" + (" Ad" if (is_paid or gclid) else ""),
                "emoji": "🔍"}
    # Meta family — Instagram is a sub-platform of Meta
    if any(k in src for k in ("instagram", "insta", "ig_")) or src == "ig":
        return {"key": "instagram_ad" if is_paid else "instagram",
                "label": "Instagram" + (" Ad" if is_paid else ""),
                "emoji": "📸"}
    if any(k in src for k in ("facebook", "fb_", "meta")) or src == "fb" or fbclid:
        return {"key": "facebook_ad" if (is_paid or fbclid) else "facebook",
                "label": "Facebook" + (" Ad" if (is_paid or fbclid) else ""),
                "emoji": "📘"}
    # Referrals: this lead arrived via someone else's referral link.
    # We can't tell from utm alone; check ghl_tags / utm_campaign for hints.
    if "referido" in camp or "referral" in src or "referral" in med:
        return {"key": "referral", "label": "Referral", "emoji": "👥"}
    # Email / newsletter
    if "email" in src or "newsletter" in src or med == "email":
        return {"key": "email", "label": "Email", "emoji": "📧"}
    # Catch-all "other" if any UTM at all
    if src or med or camp:
        return {"key": "other", "label": (src or med or camp)[:18], "emoji": "🔗"}
    # Truly nothing → direct
    return {"key": "direct", "label": "Direct", "emoji": "🌐"}


# Source-bucket order for stats display + filter dropdown.
SOURCE_BUCKETS = [
    ("instagram",     "📸 Instagram"),
    ("instagram_ad",  "📸 Instagram Ad"),
    ("facebook",      "📘 Facebook"),
    ("facebook_ad",   "📘 Facebook Ad"),
    ("google",        "🔍 Google"),
    ("google_ad",     "🔍 Google Ad"),
    ("tiktok",        "🎵 TikTok"),
    ("tiktok_ad",     "🎵 TikTok Ad"),
    ("referral",      "👥 Referral"),
    ("email",         "📧 Email"),
    ("other",         "🔗 Other"),
    ("direct",        "🌐 Direct"),
]


# Temperature buckets for stats display + clickable filter cards.
TEMP_BUCKETS = [
    ("burning",  "🔥 Burning",  "#DC2626"),
    ("hot",      "🚀 Hot",      "#F97316"),
    ("warm",     "🌡 Warm",     "#FFC857"),
    ("cool",     "❄ Cool",      "#60A5FA"),
    ("cold",     "🧊 Cold",     "#6B7280"),
    ("customer", "💎 Customer", "#A78BFA"),
]


def temp_label_to_key(label: str) -> str:
    """Map "🔥 BURNING" -> "burning"."""
    return label.split(" ")[-1].lower()


# ── WhatsApp opener templates — research-grounded ───────────────────
# Phrasings are adapted from established cold-outreach research:
#   - "Would you be open to..." softens the ask without lowering intent
#     (Voss, "Never Split the Difference"). Higher reply rate than direct
#     requests in published Gong/Yesware data.
#   - Explicit permission to decline ("Totally fine to say no") increases
#     compliance instead of reducing it (Cialdini, "Pre-Suasion").
#   - Single question per message. Gong studied 100k+ outreach threads:
#     1 question = ~30% reply rate, 2 questions = ~17%, 3+ collapses.
#   - Reciprocity reversal ("your take helps me make this better") shifts
#     the frame from "I want something from you" to "you'd be doing me
#     a small favor", which lowers the perceived cost of replying.
#   - Choice architecture for the deposit re-touch (3 options) preserves
#     autonomy — Cialdini found that offering controlled choices beats
#     a single CTA when the relationship already exists.
#   - Light, single-line personalization only. Heavier behavioral refs
#     read as surveillance ("creepy specifics" lowers reply rate per
#     LinkedIn outreach studies).
#   - Banned pattern-matched-as-spam phrases: "just checking in",
#     "hope this finds you well", "circle back" (overused, kept in the
#     re-touch as it's literal and contextually accurate there).

SEGMENT_LABELS = {
    "deposit_paid":       ("🛒", "Deposit paid · no full purchase",     "#A78BFA"),
    "hot_no_reserve":     ("🔥", "Hot leads · no deposit",              "#DC2626"),
    "watched_no_reserve": ("📺", "Watched classes · no deposit",        "#F97316"),
    "no_engagement":      ("🌑", "No engagement · signed up only",      "#6B7280"),
}


def _segment_message(seg: str, first_name: str) -> Optional[str]:
    """Return the WA message text for a forced segment, or None.

    Returning None means: don't override the auto-picker (use per-lead
    action-based templates). hot_no_reserve uses None deliberately so
    every hot lead gets an opener tailored to what they specifically did.

    deposit_paid is a RE-ENGAGEMENT template — every €100-deposit lead
    has already been spoken to once. Uses choice architecture (Cialdini)
    instead of a direct ask.
    """
    if seg == "deposit_paid":
        return (
            f"Hey {first_name}, Alvaro from MetaKizz. "
            f"Circling back from our last chat about the program. "
            f"No rush from my side, just wanted to leave the door open. "
            f"What would feel most useful right now: more info on the program, "
            f"a quick call, or just some space to think it over?"
        )
    if seg == "watched_no_reserve":
        return (
            f"Hey {first_name}, Alvaro from MetaKizz. "
            f"Saw you've been watching some of the launch content. "
            f"Would you be open to sharing what's on your mind about MKOT 3.0 right now? "
            f"Whether you end up joining or not, your take genuinely helps me make this better."
        )
    if seg == "no_engagement":
        return (
            f"Hey {first_name}, Alvaro from MetaKizz. "
            f"You signed up a bit ago and I figured the content might have slipped past you. "
            f"Want me to send you the direct link? Totally fine to say no."
        )
    # hot_no_reserve → fall through, auto-picker handles per-action messaging
    return None


# Universal opener used across the auto-picker. Same body for every hot
# lead — only the {signal} line changes based on what they actually did.
# Keeps tone consistent and avoids the "10 different messages" problem.
_UNIVERSAL_OPENER = (
    "Hey {first_name}, Alvaro from MetaKizz. {signal}. "
    "Would you be open to sharing what's on your mind about MKOT 3.0 right now? "
    "Whether you end up joining or not, your take genuinely helps me make this better."
)


def build_whatsapp_message(ambassador, temp_result, app_lang: str = "en",
                            force_segment: Optional[str] = None) -> str:
    """Build a contextual WhatsApp message based on what the lead has done.

    When `force_segment` is set, that segment's template wins regardless
    of per-lead signals — used by the segments UI on /admin/leads to
    keep messaging consistent across the chosen audience.

    Otherwise, picks by signal in order:
      1. Paid reservation → close-the-loop (no pitch, just service)
      2. Long live attendance (60+m / 30+m) → emotional anchor
      3. Class 3 (replay) completed/in-progress → distinct from 1/2
      4. Multiple classes completed → progress-recognition
      5. 2+ classes started → engagement check-in
      6. 1 class started → "what stopped you" prompt
      7. Past-masterclass tag → reactivation
      8. Generic check-in

    Returns the message text only (URL-encoded by the caller).
    """
    first_name = (ambassador.name or "there").split()[0]
    signals = temp_result.get("signals", [])
    max_pct = temp_result.get("max_pct", {})
    has_paid = temp_result.get("has_paid_reservation", False)
    webinar_dur = temp_result.get("webinar_duration_min")

    classes_watched = [cn for cn, pct in max_pct.items() if pct >= 25]
    completed = [cn for cn, pct in max_pct.items() if pct >= 95]

    # 0. Segment override — forced template wins over signal-based picker.
    if force_segment:
        seg_msg = _segment_message(force_segment, first_name)
        if seg_msg:
            return seg_msg

    # 1. Paid customer — re-engagement (every deposit-payer has been spoken
    # to once). Same choice-architecture frame as the deposit_paid segment.
    if has_paid:
        return (
            f"Hey {first_name}, Alvaro from MetaKizz. "
            f"Circling back from our last chat about the program. "
            f"No rush from my side, just wanted to leave the door open. "
            f"What would feel most useful right now: more info on the program, "
            f"a quick call, or just some space to think it over?"
        )

    # 2-8. Auto-picker for hot leads. Pick the single most relevant
    # signal phrase, drop it into the universal opener. Body stays
    # identical across branches so tone is consistent regardless of
    # which signal won — variation lives in just one line.
    if webinar_dur and webinar_dur >= 60:
        signal = "Saw you stuck around for the full live, that's rare"
    elif webinar_dur and webinar_dur >= 30:
        signal = "Saw you joined the live last week"
    elif max_pct.get(3, 0) >= 95:
        signal = "Saw you watched the masterclass replay end to end"
    elif max_pct.get(3, 0) >= 50:
        signal = "Saw you started watching the masterclass replay"
    elif completed:
        signal = "Saw you watched some of the launch classes"
    elif classes_watched:
        signal = "Saw you started one of the launch classes"
    elif "attended past masterclass" in signals:
        signal = "We met back at our masterclass in March"
    else:
        signal = "Reaching out because you signed up for the launch content"

    return _UNIVERSAL_OPENER.format(first_name=first_name, signal=signal)
