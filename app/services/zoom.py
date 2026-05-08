"""Zoom Reports API integration: fetch participants from a finished meeting.

Auth via Server-to-Server OAuth — set in Render:
  ZOOM_ACCOUNT_ID
  ZOOM_CLIENT_ID
  ZOOM_CLIENT_SECRET

Required scope on the Zoom app: report:read:list_meeting_participants:admin

Tokens are cached in-process (1h expiry, refreshed lazily). The participants
endpoint is paginated (page_size up to 300, follow `next_page_token`).
"""
import os
import base64
import time
import logging
import requests as http_requests

logger = logging.getLogger(__name__)

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"

_token_cache = {"access_token": None, "expires_at": 0}


def _credentials():
    return {
        "account_id": os.getenv("ZOOM_ACCOUNT_ID", "").strip(),
        "client_id": os.getenv("ZOOM_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("ZOOM_CLIENT_SECRET", "").strip(),
    }


def credentials_present():
    """True if all 3 Zoom env vars are set. UI uses this to enable/disable
    the API import button."""
    c = _credentials()
    return all(c.values())


def _get_access_token():
    now = int(time.time())
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    c = _credentials()
    if not all(c.values()):
        raise RuntimeError(
            "Zoom credentials missing — set ZOOM_ACCOUNT_ID, "
            "ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET in Render."
        )

    auth = base64.b64encode(
        f"{c['client_id']}:{c['client_secret']}".encode()
    ).decode()
    r = http_requests.post(
        ZOOM_OAUTH_URL,
        params={
            "grant_type": "account_credentials",
            "account_id": c["account_id"],
        },
        headers={"Authorization": f"Basic {auth}"},
        timeout=15,
    )
    if r.status_code != 200:
        # Surface Zoom's actual error reason — they put the diagnosis in
        # the JSON body, not the HTTP status. Common bodies:
        #   {"reason":"Invalid client_id or client_secret","error":"invalid_client"}
        #   {"reason":"Account does not exist","error":"invalid_request"}
        #   {"reason":"Account does not enabled the OAuth app type","error":"invalid_request"}
        try:
            body = r.json()
            reason = body.get("reason") or body.get("error_description") or body.get("error") or r.text[:200]
        except Exception:
            reason = (r.text or "")[:200]
        # Also report which char-length account_id we're sending so a hidden
        # whitespace/newline shows up as a length mismatch.
        diag = (
            f"Zoom OAuth {r.status_code}: {reason} "
            f"[account_id len={len(c['account_id'])}, "
            f"client_id len={len(c['client_id'])}, "
            f"client_secret len={len(c['client_secret'])}]"
        )
        logger.error(diag)
        raise RuntimeError(diag)
    data = r.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["access_token"]


def _fetch_participants_endpoint(token, endpoint, meeting_id):
    """Pull paginated participants from a /report/{kind}/{id}/participants
    endpoint. Returns (list_of_participants, error_or_None).
    Raises RuntimeError only on hard auth failures.
    """
    out = []
    next_token = None
    while True:
        params = {"page_size": 300}
        if next_token:
            params["next_page_token"] = next_token
        r = http_requests.get(
            f"{ZOOM_API_BASE}/report/{endpoint}/{meeting_id}/participants",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if r.status_code != 200:
            try:
                body = r.json()
            except Exception:
                body = {"raw": (r.text or "")[:200]}
            return out, {
                "status": r.status_code,
                "code": body.get("code"),
                "message": body.get("message") or body.get("reason") or body.get("raw"),
                "endpoint": endpoint,
            }
        data = r.json()
        out.extend(data.get("participants") or [])
        next_token = data.get("next_page_token")
        if not next_token:
            return out, None


def list_past_instances(meeting_id):
    """Returns a list of dicts describing every past instance of a recurring
    or repeat-started meeting: [{ uuid, start_time }, ...]. Empty list if
    the meeting has no recorded past instances or the call fails (caller
    will fall back to single-shot fetch).
    """
    try:
        token = _get_access_token()
    except Exception:
        return []
    r = http_requests.get(
        f"{ZOOM_API_BASE}/past_meetings/{meeting_id}/instances",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if r.status_code != 200:
        return []
    return r.json().get("meetings") or []


def _double_url_encode(uuid):
    """Zoom UUIDs that start with `/` or contain `//` MUST be double-URL-encoded
    when used as a path segment. We always double-encode for safety; safe values
    pass through unchanged after the second decode."""
    from urllib.parse import quote
    return quote(quote(uuid, safe=""), safe="")


def fetch_meeting_participants(meeting_id):
    """Pull participants for a finished session.

    Strategy:
      1. List past instances of the meeting. Recurring meetings or meetings
         started multiple times in one day produce one instance per session.
         Each instance has a unique UUID with its own participant report.
      2. If 2+ instances exist, fetch participants for EACH and concatenate.
         The downstream importer dedups by email and sums durations across
         sessions, which is the right behavior whether the same person
         attended one instance or several.
      3. If only 1 instance (or the list call fails), fall back to the
         numeric meeting_id which Zoom resolves to the latest instance.
      4. Final fallback: try the same call against /report/webinars/ in case
         this is a Zoom Webinar (different product, same payload shape).
    """
    token = _get_access_token()

    instances = list_past_instances(meeting_id)
    if len(instances) >= 2:
        merged = []
        for inst in instances:
            uuid = inst.get("uuid")
            if not uuid:
                continue
            encoded = _double_url_encode(uuid)
            out, err = _fetch_participants_endpoint(token, "meetings", encoded)
            if err is None:
                merged.extend(out)
            else:
                logger.warning(
                    "zoom: instance %s skipped (%s code=%s msg=%s)",
                    uuid, err["status"], err.get("code"), err.get("message"),
                )
        if merged:
            logger.info(
                "zoom: merged %d participant rows from %d instances of meeting %s",
                len(merged), len(instances), meeting_id,
            )
            return merged
        # Fall through to single-shot below if every per-UUID call failed.

    out, err = _fetch_participants_endpoint(token, "meetings", meeting_id)
    if err is None:
        return out

    # Common case worth retrying as webinar: 404 or 400 with "not found" code.
    # If meetings 400'd because this is actually a Zoom Webinar (a different
    # product/endpoint), the webinars endpoint will succeed.
    if err["status"] in (400, 404):
        out2, err2 = _fetch_participants_endpoint(token, "webinars", meeting_id)
        if err2 is None:
            logger.info("zoom: meeting %s resolved via /report/webinars/", meeting_id)
            return out2
        # If the webinar attempt failed for a *different* reason than
        # "not found", surface that one — it's likely the more useful one.
        if err2["status"] not in (400, 404):
            err = err2

    raise RuntimeError(
        f"Zoom Reports API {err['status']}: "
        f"code={err.get('code')} · {err.get('message')} "
        f"(tried /report/meetings/{meeting_id}/participants"
        + (", then /report/webinars/" + meeting_id + "/participants" if err["status"] in (400, 404) else "")
        + ")"
    )
