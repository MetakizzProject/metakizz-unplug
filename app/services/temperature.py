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
    # Live webinar — highest non-purchase intent. Showing up live for an
    # hour requires real commitment; this should dominate the score.
    "webinar_attended":      80,
    # Purchase — auto-bucket → Customer regardless of score.
    "purchase_completed":    150,
}


# Bucket thresholds. Tuned so:
#   - Just opening emails → Cold/Cool (passive)
#   - Watching half of one class → Warm (curious)
#   - Watching multiple classes → Hot (engaged)
#   - Webinar attendance OR all 3 classes → Burning (high intent)
TEMP_THRESHOLDS = {
    "cold":    (0, 14),
    "cool":    (15, 39),
    "warm":    (40, 79),
    "hot":     (80, 159),
    "burning": (160, 10_000),
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

    # ── Bucket ── (thresholds in TEMP_THRESHOLDS)
    if any(e.event_type == "purchase_completed" for e in lead_events):
        bucket, color = "💎 CUSTOMER", "#A78BFA"
    elif score >= TEMP_THRESHOLDS["burning"][0]:
        bucket, color = "🔥 BURNING", "#DC2626"
    elif score >= TEMP_THRESHOLDS["hot"][0]:
        bucket, color = "🚀 HOT", "#F97316"
    elif score >= TEMP_THRESHOLDS["warm"][0]:
        bucket, color = "🌡 WARM", "#FFC857"
    elif score >= TEMP_THRESHOLDS["cool"][0]:
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
