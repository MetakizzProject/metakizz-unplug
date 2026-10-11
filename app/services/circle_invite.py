"""Circle V2 Admin API client for the Partner Invite flow.

Flow:
  1. Look up the buyer by email and read which access groups they belong to.
  2. Pick the target access group (Dancers or Instructors) by mirroring the
     buyer's membership.
  3. Create the partner as a community member (or skip if they already exist).
  4. Add the partner to the target access group.

Returns:
  status — one of:
      "created"        — partner created + added to the target group
      "added_to_group" — partner already a member, added to the target group
      "buyer_missing"  — buyer email is not in the Circle community
      "buyer_no_group" — buyer is in neither Dancers nor Instructors
      "failed"         — anything else (auth, network, 5xx, ...)
  payload — dict with debugging info (status code, body, target_group, etc.)
  target_group — "dancers" | "instructors" | None (None on early failures)
"""

import os
import json
import logging

import requests as http_requests

logger = logging.getLogger(__name__)

CIRCLE_API_BASE = "https://app.circle.so/api/admin/v2"
TIMEOUT_SECONDS = 15


def _headers():
    token = os.getenv("CIRCLE_API_TOKEN")
    return {
        "Authorization": f"Bearer {token}" if token else "",
        "Content-Type": "application/json",
    }


def _params():
    community_id = os.getenv("CIRCLE_COMMUNITY_ID")
    return {"community_id": community_id} if community_id else {}


def _safe_body(resp):
    try:
        return resp.json()
    except Exception:
        return (resp.text or "")[:1000]


def search_member_by_email(email):
    """Return the community_member dict for `email`, or None if not found.

    Uses GET /api/admin/v2/community_members/search?email=<email>.
    Returns None on 404. Raises on other non-2xx so the caller can decide.
    """
    url = f"{CIRCLE_API_BASE}/community_members/search"
    resp = http_requests.get(
        url,
        headers=_headers(),
        params={**_params(), "email": email},
        timeout=TIMEOUT_SECONDS,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def list_member_access_group_ids(member_id):
    """Return the list of access group IDs (ints) the member belongs to.

    Uses GET /api/admin/v2/community_members/{id}/access_groups. Paginates
    only on the off-chance the member is in >60 groups (won't happen for us).
    """
    ids = []
    page = 1
    while True:
        resp = http_requests.get(
            f"{CIRCLE_API_BASE}/community_members/{member_id}/access_groups",
            headers=_headers(),
            params={**_params(), "page": page, "per_page": 100},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        for rec in data.get("records", []) or []:
            try:
                ids.append(int(rec["id"]))
            except Exception:
                continue
        if not data.get("has_next_page"):
            break
        page += 1
    return ids


# MKOT Academy (sucesor de MKOT 3.0) vende desde oct-2026 con sus propios
# grupos. El comprador puede estar en el grupo de Academy o en el antiguo de
# MKOT 3.0; la pareja va al mismo grupo que el comprador.
ACADEMY_DANCERS_DEFAULT = "150936"
ACADEMY_INSTRUCTORS_DEFAULT = "150937"


def _env_group_id(name, default=""):
    raw = os.getenv(name, default).strip()
    return int(raw) if raw.isdigit() else None


def _resolve_target_group(buyer_group_ids, dancers_ids, instructors_ids):
    """Pick which access group the partner should be added to.

    dancers_ids / instructors_ids are ordered lists (Academy first, then the
    legacy MKOT 3.0 group). Returns ("dancers" | "instructors" | None,
    target_id_or_None): the first group of the list that the buyer is in.

    If the buyer is in BOTH tracks, prefer Dancers (more common for couples).
    The admin alert will note it so Álvaro can correct manually if needed.
    """
    for gid in dancers_ids:
        if gid in buyer_group_ids:
            return "dancers", gid
    for gid in instructors_ids:
        if gid in buyer_group_ids:
            return "instructors", gid
    return None, None


def invite_partner_to_circle(buyer_email, partner_email, partner_name):
    """Mirror the buyer's access group when inviting the partner.

    Args:
        buyer_email: email of the person who paid (lowercased by caller).
        partner_email: partner's email (lowercased by caller).
        partner_name: partner's full name.

    Returns:
        (status, payload, target_group)
    """
    token = os.getenv("CIRCLE_API_TOKEN")
    community_id = os.getenv("CIRCLE_COMMUNITY_ID")
    dancers_ids = [g for g in (
        _env_group_id("CIRCLE_ACCESS_GROUP_ACADEMY_DANCERS_ID", ACADEMY_DANCERS_DEFAULT),
        _env_group_id("CIRCLE_ACCESS_GROUP_DANCERS_ID"),
    ) if g]
    instructors_ids = [g for g in (
        _env_group_id("CIRCLE_ACCESS_GROUP_ACADEMY_INSTRUCTORS_ID", ACADEMY_INSTRUCTORS_DEFAULT),
        _env_group_id("CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID"),
    ) if g]

    if not token or not community_id or (not dancers_ids and not instructors_ids):
        msg = (
            "missing CIRCLE_API_TOKEN / CIRCLE_COMMUNITY_ID / "
            "CIRCLE_ACCESS_GROUP_DANCERS_ID / CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID"
        )
        logger.error("circle invite preflight failed: %s", msg)
        return "failed", {"error": msg}, None

    # Step 1: find the buyer.
    try:
        buyer = search_member_by_email(buyer_email)
    except Exception as e:
        logger.exception("circle search_member network error for buyer %s", buyer_email)
        return "failed", {"error": f"network on buyer search: {e}"}, None

    if buyer is None:
        logger.warning("circle: buyer %s not found in community", buyer_email)
        return "buyer_missing", {"buyer_email": buyer_email}, None

    buyer_id = buyer.get("id")
    if not buyer_id:
        return "failed", {"error": "buyer record missing id", "buyer": buyer}, None

    # Step 2: list buyer's access groups.
    try:
        buyer_group_ids = list_member_access_group_ids(buyer_id)
    except Exception as e:
        logger.exception("circle list_access_groups error for buyer %s", buyer_email)
        return "failed", {"error": f"network on access_groups list: {e}"}, None

    target_group, target_id = _resolve_target_group(
        buyer_group_ids, dancers_ids, instructors_ids,
    )
    if target_id is None:
        logger.warning(
            "circle: buyer %s (id=%s) is not in Dancers (%s) or Instructors (%s); groups=%s",
            buyer_email, buyer_id, dancers_ids, instructors_ids, buyer_group_ids,
        )
        return "buyer_no_group", {
            "buyer_email": buyer_email,
            "buyer_id": buyer_id,
            "buyer_group_ids": buyer_group_ids,
            "dancers_ids": dancers_ids,
            "instructors_ids": instructors_ids,
        }, None

    in_both = (
        any(g in buyer_group_ids for g in dancers_ids)
        and any(g in buyer_group_ids for g in instructors_ids)
    )

    # Step 3: create the partner. The POST /community_members body does NOT
    # accept access_group_ids — we attach the group in step 4.
    create_url = f"{CIRCLE_API_BASE}/community_members"
    create_body = {
        "email": partner_email,
        "name": partner_name,
        "skip_invitation": False,
    }

    try:
        create_resp = http_requests.post(
            create_url,
            headers=_headers(),
            params=_params(),
            json=create_body,
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("circle create_member network error for %s", partner_email)
        return "failed", {
            "error": f"network on create_member: {e}",
            "target_group": target_group,
        }, target_group

    partner_already_existed = False
    if create_resp.status_code >= 300:
        if create_resp.status_code == 422:
            partner_already_existed = True
            logger.info("circle: partner %s already exists, will add to group", partner_email)
        else:
            logger.error(
                "circle create_member failed for %s: %s %s",
                partner_email, create_resp.status_code, create_resp.text[:500],
            )
            return "failed", {
                "status_code": create_resp.status_code,
                "body": _safe_body(create_resp),
                "target_group": target_group,
            }, target_group

    # Step 4: add partner to the target access group.
    add_url = f"{CIRCLE_API_BASE}/access_groups/{target_id}/community_members"
    try:
        add_resp = http_requests.post(
            add_url,
            headers=_headers(),
            params=_params(),
            json={"email": partner_email},
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("circle add_to_group network error for %s", partner_email)
        return "failed", {
            "error": f"network on add_to_group: {e}",
            "target_group": target_group,
            "partner_already_existed": partner_already_existed,
        }, target_group

    if add_resp.status_code >= 300:
        logger.error(
            "circle add_to_group failed for %s: %s %s",
            partner_email, add_resp.status_code, add_resp.text[:500],
        )
        return "failed", {
            "status_code": add_resp.status_code,
            "body": _safe_body(add_resp),
            "target_group": target_group,
            "partner_already_existed": partner_already_existed,
        }, target_group

    final_status = "added_to_group" if partner_already_existed else "created"
    logger.info(
        "circle invite ok: partner=%s status=%s target_group=%s in_both_buyer_groups=%s",
        partner_email, final_status, target_group, in_both,
    )
    return final_status, {
        "target_group": target_group,
        "target_id": target_id,
        "buyer_in_both": in_both,
        "partner_already_existed": partner_already_existed,
        "create_status": create_resp.status_code,
        "add_status": add_resp.status_code,
    }, target_group


def serialize_response(payload):
    """Compact JSON string suitable for storing in PartnerInvite.circle_response."""
    try:
        return json.dumps(payload, default=str)[:4000]
    except Exception:
        return str(payload)[:4000]
