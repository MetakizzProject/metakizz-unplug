"""Cloudflare Turnstile verification.

Verifies a token issued by the Turnstile widget on the Lovable landing page.
Runs in two modes controlled by env var TURNSTILE_ENFORCE:

- log-only (default): every signup is verified and the result stored on the
  Ambassador row, but the signup is NEVER rejected based on Turnstile alone.
  Lets us monitor token-arrival rates before flipping enforcement on.

- enforce (TURNSTILE_ENFORCE=1): signups with status 'invalid' or 'missing'
  are rejected with HTTP 400. 'error' (Cloudflare API down) and
  'not_configured' (no secret set) still fail open to avoid breaking signups.

The verify endpoint is documented at:
  https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Status taxonomy stored on Ambassador.turnstile_status
STATUS_VALID = "valid"            # CF says success: True
STATUS_INVALID = "invalid"        # CF says success: False
STATUS_MISSING = "missing"        # No token came through (form / GHL didn't pass it)
STATUS_ERROR = "error"            # Network or HTTP error talking to CF
STATUS_NOT_CONFIGURED = "not_configured"  # No TURNSTILE_SECRET_KEY env var


def is_enforce_mode():
    """Return True if invalid tokens should reject signups."""
    return os.environ.get("TURNSTILE_ENFORCE", "").strip().lower() in ("1", "true", "yes", "on")


def verify_token(token, remote_ip=None):
    """Verify a Turnstile token with Cloudflare.

    Returns a dict:
      {
        "status": "valid" | "invalid" | "missing" | "error" | "not_configured",
        "codes":  comma-separated CF error codes (string), or None
      }

    Never raises — always returns a dict, even on network failure (fails open
    to "error" status so the caller can decide what to do).
    """
    if not token or not token.strip():
        return {"status": STATUS_MISSING, "codes": None}

    secret = (os.environ.get("TURNSTILE_SECRET_KEY") or "").strip()
    if not secret:
        return {"status": STATUS_NOT_CONFIGURED, "codes": None}

    payload = {"secret": secret, "response": token.strip()}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        r = requests.post(VERIFY_URL, data=payload, timeout=5)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.warning("turnstile verify network error: %s", type(e).__name__)
        return {"status": STATUS_ERROR, "codes": f"network:{type(e).__name__}"}
    except ValueError:
        logger.warning("turnstile verify non-JSON response")
        return {"status": STATUS_ERROR, "codes": "non_json"}

    if data.get("success") is True:
        return {"status": STATUS_VALID, "codes": None}

    error_codes = data.get("error-codes") or []
    codes_str = ",".join(error_codes)[:160] if error_codes else None
    return {"status": STATUS_INVALID, "codes": codes_str}


def record_rejection(status, codes, email_attempted, name_attempted,
                     ip, user_agent, source):
    """Persist a TurnstileRejection row. Best-effort — never raises.

    Called from the route layer when an enforce-mode rejection happens.
    Failures here must not poison the request/response, so we swallow
    everything and log.
    """
    try:
        from app.models import db, TurnstileRejection
        rej = TurnstileRejection(
            status=status,
            codes=codes,
            email_attempted=(email_attempted or "")[:200] or None,
            name_attempted=(name_attempted or "")[:200] or None,
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:500] or None,
            source=source,
        )
        db.session.add(rej)
        db.session.commit()
    except Exception:
        try:
            from app.models import db
            db.session.rollback()
        except Exception:
            pass
        logger.exception("failed to persist turnstile rejection")


def extract_token_from_payload(payload):
    """Pull the Turnstile token from a JSON webhook payload.

    Lovable submits the field as 'cf-turnstile-response' (Cloudflare's
    canonical name). GHL might rename or wrap it depending on workflow
    config, so we tolerate a few variants.
    """
    if not isinstance(payload, dict):
        return ""

    # Direct top-level lookup of common variants.
    for key in (
        "cf-turnstile-response",
        "cf_turnstile_response",
        "cf_turnstile_token",
        "turnstile_token",
        "turnstileToken",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # Nested under common GHL containers.
    for container in ("custom_data", "customData", "data", "contact", "Contact"):
        nested = payload.get(container)
        if isinstance(nested, dict):
            for key in (
                "cf-turnstile-response",
                "cf_turnstile_response",
                "cf_turnstile_token",
                "turnstile_token",
                "turnstileToken",
            ):
                val = nested.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()

    return ""
