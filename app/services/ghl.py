"""GoHighLevel API v2 client.

Reads contact data (with custom fields → UTMs, segmentation answers,
lead score, etc.) and writes back tags. Used by the admin sync endpoint
and any future bidirectional integrations.

Auth: Private Integration Token (PIT) + Location ID. Set in env:
    GHL_PRIVATE_TOKEN    pit-xxxxxxx-...
    GHL_LOCATION_ID      <location uuid>

Docs: https://highlevel.stoplight.io/docs/integrations
"""

import os
import logging
from typing import Iterator, Optional, Dict, Any, List

import requests

logger = logging.getLogger(__name__)


GHL_BASE = "https://services.leadconnectorhq.com"
GHL_VERSION = "2021-07-28"

# Custom field IDs in this MetaKizz GHL location (introspected 2026-05-04
# via GET /locations/{id}/customFields). Update if GHL admin renames or
# rebuilds these fields. Mapping our internal name → GHL custom field id.
GHL_CUSTOM_FIELDS = {
    "utm_source":          "nBvNco36w3uuguc5IFhP",
    "utm_medium":          "P1DamT7gn3iBOvG8Ztwi",
    "utm_campaign":        "0Scofb6dP6qtLKxCEf8U",  # named "UTM Campaing" in GHL (typo upstream)
    "utm_content":         "M6yBmG8jcCLWMwCH8Fez",  # "UTm Content" in GHL
    "fbclid":              "HzoEhZFFmyyVnqURswTL",
    "referral_code":       "xlygrcsfSFwQMk2czIJd",
    # Segmentation / form answers (not synced into individual columns yet,
    # but extractable here for future use).
    "dance_level":         "8QH9MkiqtxBvF9zeJJpG",
    "dance_goal":          "RAanUleaGlPwywdAjr0Y",
    "training_interest":   "yE8EEJzq9nnZA8rDNXL9",
    "ghl_lead_score":      "I7JZ8HYWWKMzfmGoCw0S",
    "ghl_referral_count":  "fZEpJhIQmlczvze0Uh0r",
    "payment_option":      "ASnUDEV4EL1YWdabjSpj",
    "is_community_member": "CBnHsQo1Lxzg8lHe5QZU",
}


class GHLConfigError(RuntimeError):
    pass


def _headers():
    token = os.getenv("GHL_PRIVATE_TOKEN")
    if not token:
        raise GHLConfigError("GHL_PRIVATE_TOKEN not set in env")
    return {
        "Authorization": f"Bearer {token}",
        "Version": GHL_VERSION,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _location_id():
    loc = os.getenv("GHL_LOCATION_ID")
    if not loc:
        raise GHLConfigError("GHL_LOCATION_ID not set in env")
    return loc


def is_configured() -> bool:
    """True if both credentials are present in env."""
    return bool(os.getenv("GHL_PRIVATE_TOKEN") and os.getenv("GHL_LOCATION_ID"))


def get_contact(contact_id: str) -> Dict[str, Any]:
    """Fetch a single contact with all custom fields."""
    r = requests.get(
        f"{GHL_BASE}/contacts/{contact_id}",
        headers=_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("contact", {})


def search_contacts_page(
    page: int = 1,
    page_limit: int = 100,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    """Search contacts using the v2 search endpoint. Returns the raw response
    body (with `contacts`, `total`, etc.).

    page_limit max is 100. To page, increment `page`.
    """
    body = {
        "locationId": _location_id(),
        "pageLimit": page_limit,
        "page": page,
    }
    if query:
        body["query"] = query
    r = requests.post(
        f"{GHL_BASE}/contacts/search",
        headers=_headers(),
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def iter_all_contacts(
    page_limit: int = 100,
    max_pages: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield every contact in the location, paging until the API returns
    fewer than page_limit (last page) or until max_pages is hit.

    Use max_pages=2 for smoke tests; omit for full sync.
    """
    page = 1
    while True:
        data = search_contacts_page(page=page, page_limit=page_limit)
        contacts = data.get("contacts", [])
        for c in contacts:
            yield c
        if len(contacts) < page_limit:
            return
        page += 1
        if max_pages is not None and page > max_pages:
            return


def extract_custom_fields(contact: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the `customFields` array on a contact into a dict keyed by
    our internal names (utm_source, utm_medium, etc.). Skips fields whose
    GHL ID isn't in our mapping. Empty values become None.
    """
    by_id = {cf.get("id"): cf.get("value") for cf in contact.get("customFields", [])}
    out = {}
    for our_name, ghl_id in GHL_CUSTOM_FIELDS.items():
        v = by_id.get(ghl_id)
        if v is None:
            out[our_name] = None
        elif isinstance(v, str):
            v = v.strip()
            out[our_name] = v if v else None
        else:
            # Multiple-options / checkbox fields come back as lists; keep as-is.
            out[our_name] = v
    return out


# Tags that mark a contact as "relevant for the launch dashboard" — they're
# kept as ghost leads when synced. Includes: current launch registrants,
# past-masterclass attendees (warm leads who already know us), and the
# webinar attendees from the previous campaign. Edit this to broaden/narrow.
RELEVANT_LEAD_TAGS = {
    "mkot3_registrado",
    "masterclass march17th",
    "webinnar 17 marzo",
}


def sync_all_contacts(
    create_missing: bool = True,
    only_with_any_tag: Optional[set] = None,
    max_pages: Optional[int] = None,
) -> Dict[str, Any]:
    """Pull every GHL contact and upsert into our Ambassador table.

    For each contact (matched by lowercase email):
      - Existing Ambassador → ALWAYS backfill ghl_contact_id, ghl_tags,
        phone, UTMs (only fills NULL/missing fields; preserves first-touch).
      - No match + create_missing=True + (only_with_tag is None OR contact
        has that tag) → insert ghost Ambassador with source='ghl_import'.

    `only_with_any_tag` defaults to RELEVANT_LEAD_TAGS so we don't pollute
    the DB with random old contacts. Includes launch registrants AND past
    masterclass / webinar attendees (warm leads). Pass None to import
    every contact regardless of tags.

    Returns a stats dict. Safe to re-run; idempotent.

    Heavy operation — ~1-2 min for 2000 contacts. Call from a background
    thread when triggered from a web request.
    """
    import secrets
    from sqlalchemy import func
    from app.models import db, Ambassador
    from app.services.phone import parse as parse_phone

    if only_with_any_tag is None:
        only_with_any_tag = RELEVANT_LEAD_TAGS

    stats = {
        "contacts_seen": 0,
        "matched_updated": 0,
        "ghost_created": 0,
        "ghost_skipped_no_email": 0,
        "ghost_skipped_no_create": 0,
        "ghost_skipped_no_tag": 0,
        "errors": 0,
    }

    def _gen_code():
        return secrets.token_urlsafe(6)[:8]

    for c in iter_all_contacts(page_limit=100, max_pages=max_pages):
        stats["contacts_seen"] += 1
        email = (c.get("email") or "").strip().lower()
        if not email:
            stats["ghost_skipped_no_email"] += 1
            continue

        try:
            cf = extract_custom_fields(c)
            ghl_id = c.get("id")
            first = (c.get("firstNameRaw") or c.get("firstName") or "").strip()
            last = (c.get("lastNameRaw") or c.get("lastName") or "").strip()
            full_name = (first + " " + last).strip() or email.split("@")[0]
            phone_raw = (c.get("phone") or "").strip()
            tags_list = c.get("tags") or []
            tags_str = ",".join(sorted(set(tags_list))) if tags_list else None

            phone_e164, country_iso = None, None
            if phone_raw:
                try:
                    parsed = parse_phone(phone_raw)
                    if parsed:
                        phone_e164 = parsed["e164"]
                        country_iso = parsed["country_code"]
                except Exception:
                    pass  # bad phone shouldn't block the sync

            amb = Ambassador.query.filter(
                func.lower(Ambassador.email) == email
            ).first()

            if amb is None:
                if not create_missing:
                    stats["ghost_skipped_no_create"] += 1
                    continue
                # Tag gate — keep contact only if they carry at least
                # one of the relevant tags (launch registrant, past
                # masterclass, past webinar). Filters out random old
                # contacts that have no business being in this dashboard.
                tag_set = set(tags_list or [])
                if only_with_any_tag and not (tag_set & only_with_any_tag):
                    stats["ghost_skipped_no_tag"] += 1
                    continue
                amb = Ambassador(
                    name=full_name[:200],
                    email=email[:200],
                    referral_code=_gen_code(),
                    dashboard_code=_gen_code(),
                    source="ghl_import",
                )
                db.session.add(amb)
                stats["ghost_created"] += 1
            else:
                stats["matched_updated"] += 1

            # Field updates — only fill if missing (preserve any existing
            # first-touch values) for UTMs; always overwrite ghl_id/tags
            # since GHL is authoritative for those.
            if ghl_id and amb.ghl_contact_id != ghl_id:
                amb.ghl_contact_id = ghl_id
            if tags_str and amb.ghl_tags != tags_str:
                amb.ghl_tags = tags_str
            if phone_e164 and not amb.phone_number:
                amb.phone_number = phone_e164
            if country_iso and not amb.country_code:
                amb.country_code = country_iso
            for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid"):
                v = cf.get(k)
                if not getattr(amb, k, None) and v and isinstance(v, str):
                    setattr(amb, k, v[:200])

            # Form-question answers — always overwrite (GHL is authoritative;
            # if the user re-submits the form with a different answer, we
            # want the new one). Length-cap to fit columns.
            for k, max_len in (
                ("dance_level", 200),
                ("dance_goal", 500),
                ("training_interest", 200),
                ("is_community_member", 60),
            ):
                v = cf.get(k)
                if v is None:
                    continue
                # Multi-options fields come back as lists — pick the
                # joined string for display.
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v if x)
                if isinstance(v, str) and v.strip():
                    new_val = v.strip()[:max_len]
                    if getattr(amb, k, None) != new_val:
                        setattr(amb, k, new_val)
        except Exception:
            stats["errors"] += 1
            logger.exception("GHL sync row failed for email=%s", email)

        # Commit in batches so a single bad row can't roll back hours of work.
        if stats["contacts_seen"] % 50 == 0:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("batch commit failed at row %d", stats["contacts_seen"])
                stats["errors"] += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("final commit failed")
        stats["errors"] += 1

    return stats


def cleanup_ghost_leads_without_relevant_tag(
    relevant_tags: Optional[set] = None,
) -> Dict[str, int]:
    """Delete Ambassadors that were imported from GHL (source='ghl_import')
    but don't carry ANY of the relevant tags. Used to undo a sync that
    ran without the tag filter.

    Safe to re-run: only acts on source='ghl_import' rows.
    Does NOT touch real signups (source='public' or 'community').
    """
    from app.models import db, Ambassador

    if relevant_tags is None:
        relevant_tags = RELEVANT_LEAD_TAGS

    stats = {"scanned": 0, "deleted": 0, "kept_with_tag": 0}

    rows = Ambassador.query.filter(Ambassador.source == "ghl_import").all()
    for amb in rows:
        stats["scanned"] += 1
        tags_csv = (amb.ghl_tags or "")
        tags_set = {t.strip() for t in tags_csv.split(",") if t.strip()}
        if tags_set & relevant_tags:
            stats["kept_with_tag"] += 1
        else:
            db.session.delete(amb)
            stats["deleted"] += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("cleanup commit failed")
        raise

    return stats


def add_tags(contact_id: str, tags: List[str]) -> bool:
    """Add tags to a contact (idempotent — GHL dedupes server-side)."""
    if not tags:
        return True
    r = requests.post(
        f"{GHL_BASE}/contacts/{contact_id}/tags",
        headers=_headers(),
        json={"tags": tags},
        timeout=15,
    )
    if r.status_code >= 400:
        logger.error("GHL add_tags failed contact=%s status=%d body=%s",
                     contact_id, r.status_code, r.text[:300])
        return False
    return True
