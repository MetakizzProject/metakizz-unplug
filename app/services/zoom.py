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
    r.raise_for_status()
    data = r.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["access_token"]


def fetch_meeting_participants(meeting_id):
    """Returns a list of dicts. Keys typically include:
      - name, user_email, join_time, leave_time, duration, status
    Empty list if the meeting hasn't ended or has no participants yet
    (the report endpoint only returns data after the meeting closes).
    """
    token = _get_access_token()
    out = []
    next_token = None
    while True:
        params = {"page_size": 300}
        if next_token:
            params["next_page_token"] = next_token
        r = http_requests.get(
            f"{ZOOM_API_BASE}/report/meetings/{meeting_id}/participants",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if r.status_code == 404:
            raise RuntimeError(
                f"Zoom meeting {meeting_id} not found, or report not ready yet "
                "(reports populate a few minutes after the meeting ends)."
            )
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("participants") or [])
        next_token = data.get("next_page_token")
        if not next_token:
            break
    return out
