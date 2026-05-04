"""Lead temperature scoring.

Combines signals from across our DB to produce a 0-N score per lead,
plus a human-readable bucket / color. Higher score = more engaged.

Signal weights are tunable from one place (TEMP_WEIGHTS). After the
launch we'll calibrate these against actual conversion data.
"""

from collections import defaultdict
from typing import Dict, List, Optional, Any


TEMP_WEIGHTS = {
    "email_opened":          5,    # per open, capped
    "email_opened_cap":      30,
    "email_clicked":         10,   # per click, capped
    "email_clicked_cap":     40,
    "dashboard_visit":       3,    # per visit, capped
    "dashboard_visit_cap":   30,
    "referral_brought":      15,   # per real signup attributed
    "class_25":              7,
    "class_50":              12,
    "class_75":              18,
    "class_95_or_complete":  25,
    "past_masterclass":      15,   # masterclass march17th tag
    "webinar_attended":      40,   # future: webinar_joined event
    "purchase_completed":    100,
}


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
    """
    out = {1: 0, 2: 0, 3: 0}
    for e in lead_events:
        cn = e.class_number
        if cn not in (1, 2, 3):
            continue
        pct = _pct_from_event(e.event_type or "", e.pct)
        if pct > out[cn]:
            out[cn] = pct
    return out


def compute_temperature(
    ambassador,
    lead_events: Optional[List[Any]] = None,
    email_events: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Score a single ambassador using all available signals.

    Pass pre-fetched lead_events and email_events for that ambassador to
    avoid N+1 queries (recommended when scoring many leads at once).

    Returns a dict:
      {
        "score":     int total points,
        "bucket":    label "🧊 COLD" | "❄ COOL" | "🌡 WARM" | "🚀 HOT" | "🔥 BURNING",
        "color":     hex color for the badge,
        "signals":   list of human-readable contributing signals,
        "max_pct":   {1: int, 2: int, 3: int}  per-class progress
      }
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

    # ── Webinar attendance (future-proof) ──
    if any(e.event_type == "webinar_joined" for e in lead_events):
        score += TEMP_WEIGHTS["webinar_attended"]
        signals.append("attended webinar")

    # ── Purchase ──
    if any(e.event_type == "purchase_completed" for e in lead_events):
        score += TEMP_WEIGHTS["purchase_completed"]
        signals.append("PURCHASED")

    # ── Past masterclass (warm signal from tags) ──
    tags_csv = (ambassador.ghl_tags or "").lower()
    if "masterclass march17th" in tags_csv:
        score += TEMP_WEIGHTS["past_masterclass"]
        signals.append("attended past masterclass")

    # ── Bucket ──
    if any(e.event_type == "purchase_completed" for e in lead_events):
        bucket, color = "💎 CUSTOMER", "#A78BFA"
    elif score >= 100:
        bucket, color = "🔥 BURNING", "#DC2626"
    elif score >= 60:
        bucket, color = "🚀 HOT", "#F97316"
    elif score >= 30:
        bucket, color = "🌡 WARM", "#FFC857"
    elif score >= 10:
        bucket, color = "❄ COOL", "#60A5FA"
    else:
        bucket, color = "🧊 COLD", "#6B7280"

    return {
        "score": score,
        "bucket": bucket,
        "color": color,
        "signals": signals,
        "max_pct": max_pct,
    }


def fetch_signals_bulk(ambassador_ids):
    """Pre-fetch LeadEvents and EmailEvents for a list of ambassador IDs in
    two queries, then return:
        (lead_events_by_id, email_events_by_id)
    """
    from app.models import LeadEvent, EmailEvent

    lead_evts = (
        LeadEvent.query
        .filter(LeadEvent.ambassador_id.in_(ambassador_ids))
        .all()
    )
    email_evts = (
        EmailEvent.query
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


def build_whatsapp_message(ambassador, temp_result, app_lang: str = "en") -> str:
    """Build a contextual WhatsApp message based on what the lead has done.

    Returns the message text only (URL-encoded by the caller).
    """
    first_name = (ambassador.name or "there").split()[0]
    signals = temp_result.get("signals", [])
    max_pct = temp_result.get("max_pct", {})

    classes_watched = [cn for cn, pct in max_pct.items() if pct >= 25]
    completed = [cn for cn, pct in max_pct.items() if pct >= 95]

    if completed:
        body = (
            f"Hey {first_name} — saw you watched class "
            f"{', '.join(str(c) for c in completed)} all the way through, "
            f"that's a lot of focus. Curious what stood out and what you're "
            f"trying to figure out with your kizz right now?"
        )
    elif len(classes_watched) >= 2:
        body = (
            f"Hey {first_name} — saw you've already started "
            f"classes {', '.join(str(c) for c in classes_watched)}. "
            f"Wanted to check in: how's it landing? Any specific bit you'd want "
            f"us to go deeper on?"
        )
    elif classes_watched:
        body = (
            f"Hey {first_name} — saw you started class {classes_watched[0]}, "
            f"that's a good first move. Anything stopping you from finishing it? "
            f"Happy to help if it's a question of timing or content."
        )
    elif "attended past masterclass" in signals:
        body = (
            f"Hey {first_name} — you joined our masterclass back in March, "
            f"and we just kicked off Hacking the Urbankizz Code. "
            f"Wanted to make sure you saw the new classes are live."
        )
    else:
        body = (
            f"Hey {first_name} — Jesus & Anni from MetaKizz here. "
            f"Just checking in to see how you're doing with the launch content "
            f"and if there's anything we can help with."
        )
    return body
