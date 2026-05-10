"""
Branded email system for MetaKizz Ambassador Challenge.
All emails are sent via Resend API with The Unplugging narrative.
"""

import os
import logging
import requests as http_requests
from flask import render_template

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def _landing_url():
    """Public landing URL where ambassadors send their referrals (Lovable PLF page)."""
    return os.getenv("LANDING_URL", "").rstrip("/")


def _whatsapp_group_url():
    """Optional WhatsApp group invite link shown in welcome emails."""
    return os.getenv("WHATSAPP_GROUP_URL", "").strip()


def _share_url(ambassador):
    """Build the public share URL for an ambassador (points to the Lovable landing)."""
    base = _landing_url()
    return f"{base}?ref={ambassador.referral_code}"


def _unsubscribe_url(ambassador, app_url):
    """Build the one-click unsubscribe URL for an ambassador."""
    token = getattr(ambassador, "unsubscribe_token", None)
    if not token:
        # Defensive fallback: if the model row predates the migration, link to a generic page.
        return f"{app_url.rstrip('/')}/unsubscribe/missing"
    return f"{app_url.rstrip('/')}/unsubscribe/{token}"


def is_unsubscribed(ambassador):
    """True if this ambassador has opted out and should not receive any further emails."""
    return getattr(ambassador, "unsubscribed_at", None) is not None


def _send_with_attachment(to, subject, html, attachment_bytes, attachment_filename,
                           from_name="MetaKizz Project"):
    """Send an email via Resend with a single PDF attachment.

    Resend wants attachments as base64 strings under the `attachments`
    field of the API payload. Returns True on success.
    """
    import base64

    api_key = os.getenv("RESEND_API_KEY")
    default_from = os.getenv("EMAIL_FROM", "MetaKizz <noreply@metakizzproject.com>")
    if from_name:
        addr = default_from.split("<", 1)[-1].rstrip(">").strip() if "<" in default_from else default_from
        email_from = f"{from_name} <{addr}>"
    else:
        email_from = default_from

    if not api_key:
        logger.warning("RESEND_API_KEY not set, skipping email to %s", to)
        return False

    if not attachment_bytes or not attachment_filename:
        logger.error("send_with_attachment called without attachment bytes/filename")
        return False

    payload = {
        "from": email_from,
        "to": [to],
        "subject": subject,
        "html": html,
        "attachments": [{
            "filename": attachment_filename,
            "content": base64.b64encode(attachment_bytes).decode("ascii"),
        }],
    }

    try:
        resp = http_requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code < 300:
            logger.info("Email + attachment sent to %s: %s", to, subject)
            return True
        logger.error("Email+attach failed (%s) to %s: %s", resp.status_code, to, resp.text[:500])
        return False
    except Exception as e:
        logger.error("Email+attach exception to %s: %s", to, e)
        return False


def _send(to, subject, html, from_name="MetaKizz Project", *, template_key=None, ambassador=None):
    """Send an email via Resend. Returns True on success.

    Default sender display name is "MetaKizz Project" (consistent across all
    transactional emails — Gmail/Outlook use this for inbox classification).
    Pass from_name to override (e.g. an internal-style send).

    Optional kwargs:
      template_key — logical name (welcome, activation_nudge, ...) for analytics.
      ambassador   — the Ambassador the email is going to, to link the EmailEvent row.

    When template_key is provided, every successful send writes an EmailEvent
    row (event_type='sent'), and Resend's webhook later augments it with
    'opened' / 'clicked' rows matched by resend_email_id.
    """
    api_key = os.getenv("RESEND_API_KEY")
    default_from = os.getenv("EMAIL_FROM", "MetaKizz <noreply@metakizzproject.com>")
    if from_name:
        # Replace the display name part of "Name <addr>" while keeping the address.
        addr = default_from.split("<", 1)[-1].rstrip(">").strip() if "<" in default_from else default_from
        email_from = f"{from_name} <{addr}>"
    else:
        email_from = default_from

    if not api_key:
        logger.warning("RESEND_API_KEY not set, skipping email to %s", to)
        return False

    # RFC 8058 List-Unsubscribe headers — Gmail/Outlook need these to render
    # the native one-click unsubscribe button. Without them, our emails get
    # classified as bulk and degrade sender reputation over time.
    email_payload = {
        "from": email_from,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if ambassador is not None:
        try:
            from flask import current_app
            app_url = current_app.config.get("APP_URL", "")
            unsub_url = _unsubscribe_url(ambassador, app_url) if app_url else None
            if unsub_url:
                email_payload["headers"] = {
                    "List-Unsubscribe": f"<{unsub_url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                }
        except Exception:
            # No request context (e.g. cron fallback) — skip the header gracefully.
            pass

    try:
        resp = http_requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=email_payload,
            timeout=10,
        )
        if resp.status_code < 300:
            logger.info("Email sent to %s: %s", to, subject)
            # Track this send in EmailEvent. Best-effort: failure here must
            # not stop the email-send caller.
            if template_key:
                try:
                    _record_send_event(resp, to, template_key, ambassador)
                except Exception:
                    logger.exception("failed to record EmailEvent for %s", to)
            return True
        else:
            logger.error("Email failed (%s) to %s: %s", resp.status_code, to, resp.text)
            return False
    except Exception as e:
        logger.error("Email exception to %s: %s", to, e)
        return False


def _record_send_event(resp, to_email, template_key, ambassador):
    """Insert a 'sent' row in email_events. Pulls Resend's email id from the
    response so later webhook events ('opened', 'clicked', etc.) can match back.
    """
    from app.models import db, EmailEvent
    resend_id = None
    try:
        body = resp.json()
        if isinstance(body, dict):
            resend_id = body.get("id")
    except Exception:
        pass
    evt = EmailEvent(
        ambassador_id=(ambassador.id if ambassador is not None else None),
        template_key=template_key,
        event_type="sent",
        resend_email_id=resend_id,
        to_email=to_email,
    )
    db.session.add(evt)
    db.session.commit()


def _wrap(content_html, app_url, preview_text=None, unsubscribe_url=None, unsubscribe_prominent=False):
    """Wrap email content in the branded MetaKizz shell.

    preview_text: shown by Gmail/Apple Mail next to the subject line.
    unsubscribe_url: if provided, render an unsubscribe block in the footer.
    unsubscribe_prominent: if True, render a visible block; otherwise a discreet footer link.
    """
    logo_url = f"{app_url}/static/brand/organized/logo-green.png"

    # Hidden preheader: rendered as 0px text so it doesn't show in the body, but
    # major clients pick it up and display it next to the subject line.
    preview_html = ""
    if preview_text:
        preview_html = (
            f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;'
            f'font-size:1px;line-height:1px;color:#000000;opacity:0;">{preview_text}</div>'
        )

    unsubscribe_block = ""
    if unsubscribe_url:
        if unsubscribe_prominent:
            unsubscribe_block = f"""
<tr><td style="padding-top:16px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border:1px solid #2D2D44;border-radius:12px;">
    <tr><td align="center" style="padding:18px 20px;">
        <p style="color:#FFFFFF;font-size:14px;margin:0 0 6px 0;">Don't want to keep receiving these emails?</p>
        <p style="margin:0;"><a href="{unsubscribe_url}" style="color:#2EDB99;text-decoration:underline;font-size:14px;font-weight:bold;">Click here to stop them.</a></p>
    </td></tr>
    </table>
</td></tr>"""
        else:
            unsubscribe_block = f"""
<tr><td align="center" style="padding-top:14px;">
    <p style="color:#6B7280;font-size:12px;margin:0;">Not feeling this? <a href="{unsubscribe_url}" style="color:#6B7280;text-decoration:underline;">Unsubscribe</a></p>
</td></tr>"""

    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#000000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
{preview_html}
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#000000;">
<tr><td align="center" style="padding:40px 20px;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;">

<!-- Logo -->
<tr><td align="center" style="padding-bottom:32px;">
    <img src="{logo_url}" alt="MetaKizz" width="120" style="display:block;">
</td></tr>

<!-- Content -->
<tr><td style="background-color:#111111;border-radius:16px;padding:32px 28px;">
    {content_html}
</td></tr>
{unsubscribe_block}

<!-- Footer -->
<tr><td align="center" style="padding-top:24px;">
    <p style="color:#4B5563;font-size:12px;margin:0;">MetaKizz &middot; The Unplugging Protocol</p>
    <p style="color:#4B5563;font-size:11px;margin:6px 0 0 0;">Need anything? <a href="mailto:info@metakizzproject.com" style="color:#6B7280;text-decoration:underline;">info@metakizzproject.com</a></p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _button(text, url, color="#2EDB99", text_color="#000000"):
    """Generate an email-safe button."""
    return f"""
<table cellpadding="0" cellspacing="0" style="margin:24px 0;">
<tr><td align="center" style="background-color:{color};border-radius:10px;">
    <a href="{url}" style="display:inline-block;padding:14px 32px;color:{text_color};text-decoration:none;font-weight:bold;font-size:15px;">{text}</a>
</td></tr>
</table>"""


def _stats_card(stats_html):
    """Generate a dark stats card."""
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#1A1A2E;border:1px solid #2D2D44;border-radius:12px;margin:20px 0;">
<tr><td style="padding:16px 20px;">
    {stats_html}
</td></tr>
</table>"""


# ─── MKOT 3.0 RESERVATION CONFIRMATION ───────────────────────────

def send_reservation_confirmed(reservation):
    """Confirmation email sent immediately after the buyer completes the
    post-payment form for MKOT 3.0.

    Standalone HTML template (same terminal/MetaKizz aesthetic as welcome.html).
    Best-effort: callers should not block on the result.
    """
    if not reservation or not reservation.email:
        return False

    # If the buyer matches an existing Ambassador and they unsubscribed,
    # respect that. Pure non-ambassador buyers receive the email regardless
    # (transactional purchase confirmation, not marketing).
    ambassador = reservation.ambassador
    if ambassador is not None and is_unsubscribed(ambassador):
        return False

    first_name = "there"
    if reservation.name and reservation.name.strip():
        first_name = reservation.name.strip().split()[0]
    elif ambassador is not None and ambassador.name and ambassador.name.strip():
        first_name = ambassador.name.strip().split()[0]

    amount_eur = "{:.0f}".format((reservation.amount_cents or 10000) / 100)

    html = render_template(
        "emails/reservation_confirmed.html",
        first_name=first_name,
        email=reservation.email,
        amount_eur=amount_eur,
    )

    return _send(
        reservation.email,
        "Your MKOT 3.0 reservation is confirmed ✦",
        html,
        template_key="reservation_confirmed",
        ambassador=ambassador,
    )


def send_reservation_first50_email(reservation):
    """Outreach email for paid Reservations we haven't reached on WhatsApp.
    Frames the buyer as 'first 50 to commit' and asks them to start a
    WhatsApp chat with us so we can finalize admission together.
    """
    if not reservation or not reservation.email:
        return False

    ambassador = reservation.ambassador
    if ambassador is not None and is_unsubscribed(ambassador):
        return False

    first_name = "there"
    if reservation.name and reservation.name.strip():
        first_name = reservation.name.strip().split()[0]
    elif ambassador is not None and ambassador.name and ambassador.name.strip():
        first_name = ambassador.name.strip().split()[0]

    amount_eur = "{:.0f}".format((reservation.amount_cents or 10000) / 100)

    html = render_template(
        "emails/reservation_first50.html",
        first_name=first_name,
        email=reservation.email,
        amount_eur=amount_eur,
    )

    return _send(
        reservation.email,
        "You're in the first 50 — let's talk",
        html,
        template_key="reservation_first50",
        ambassador=ambassador,
    )


# ─── EMAIL 1: WELCOME ────────────────────────────────────────────

def send_welcome_email(ambassador, app_url):
    """Send the welcome email after registering for Hacking the Urbankiz Code.

    Triggered by:
    - GHL webhook on PLF landing signup (app/services/signup.py)
    - The /join form (app/routes/home.py)

    Renders templates/emails/welcome.html (the redesigned terminal-aesthetic email)
    with personalization for community vs public source.
    """
    if is_unsubscribed(ambassador):
        return False

    first_name = (
        ambassador.name.strip().split()[0]
        if ambassador.name and ambassador.name.strip()
        else "there"
    )

    html = render_template(
        "emails/welcome.html",
        first_name=first_name,
        email=ambassador.email,
        referral_url=_share_url(ambassador),
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        community=(ambassador.source == "community"),
    )

    return _send(
        ambassador.email,
        "Welcome to Hacking the Urbankiz Code.",
        html,
        template_key="welcome",
        ambassador=ambassador,
    )


# ─── EMAIL 3: FIRST UNPLUG (new) ─────────────────────────────────

def send_first_unplug_email(ambassador, referral_name, app_url):
    """Send the celebratory email when an ambassador receives their first referral.

    Triggered in real time when ambassador.referral_count goes from 0 to 1
    (wired via app/services/signup.py).

    Renders templates/emails/first_unplug.html.
    """
    if is_unsubscribed(ambassador):
        return False

    first_name = (
        ambassador.name.strip().split()[0]
        if ambassador.name and ambassador.name.strip()
        else "there"
    )
    referral_first_name = (
        (referral_name or "someone").strip().split()[0]
        if (referral_name or "").strip()
        else "Someone"
    )

    count = ambassador.referral_count
    remaining = max(0, 5 - count)

    html = render_template(
        "emails/first_unplug.html",
        first_name=first_name,
        referral_first_name=referral_first_name,
        count=count,
        remaining=remaining,
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        community=(ambassador.source == "community"),
    )

    return _send(
        ambassador.email,
        f"{referral_first_name} is in.",
        html,
        template_key="first_unplug",
        ambassador=ambassador,
    )


# ─── HELPERS for the rest of the new email functions ─────────────

def _first_name(ambassador):
    """Extract the first name token from ambassador.name with safe fallback."""
    if ambassador.name and ambassador.name.strip():
        return ambassador.name.strip().split()[0]
    return "there"


def _whatsapp_share_url(text):
    """Build a WhatsApp deeplink with the message text URL-encoded."""
    return f"https://wa.me/?text={http_requests.utils.quote(text)}"


# Top 3 reward catalogue, indexed by (source, position) -> (name, value_str|None)
_TOP3 = {
    ("community", 1): ("1 year of MetaDancers, free", "€1,000+ value"),
    ("community", 2): ("Video feedback on your dancing", "direct from us · €150+ value"),
    ("community", 3): ("Personalized MetaKizz hoodie", None),
    ("public", 1): ("Video feedback on your dancing", "direct from us · €150+ value"),
    ("public", 2): ("Personalized MetaKizz hoodie", None),
    ("public", 3): ("Personalized MetaKizz t-shirt", None),
}


def _next_step_for_position(source, position):
    """Return the next-step instruction string the You Won email should show."""
    name, _ = _TOP3.get((source, position), ("", None))
    n = (name or "").lower()
    if "metadancers" in n and "year" in n:
        return "We'll activate your year of MetaDancers within 48h. No action needed."
    if "video feedback" in n:
        return "Reply with an unlisted YouTube link (or Dropbox) of the video you want corrected. We'll send the breakdown within 14 days."
    if "hoodie" in n or "t-shirt" in n or "tshirt" in n:
        return "Reply with size (S / M / L / XL) and shipping address. We'll get it in the post within 7 days."
    return "Reply to this email to claim your reward."


# ─── EMAIL 2: ACTIVATION NUDGE ───────────────────────────────────

def send_activation_push_email(ambassador, app_url):
    """Manual admin "almost there" push for ambassadors at 0-4 unplugs.

    Personalised: subject line and body both adapt to the recipient's
    current count + remaining-to-5. Skips opted-out and ambassadors
    who already have 5+ unplugs (the prize is theirs — no nudge needed).
    """
    if is_unsubscribed(ambassador):
        return False
    count = ambassador.referral_count
    if count >= 5:
        return False  # already unlocked, this template doesn't apply

    remaining_to_5 = 5 - count
    referral_url = _share_url(ambassador)
    wa_message = (
        f"Hey, just registered for a free Urbankiz training with Jesus & Anni (MetaKizz). "
        f"2 videos + 1 live. Thought of you. {referral_url}"
    )

    html = render_template(
        "emails/activation_push.html",
        first_name=_first_name(ambassador),
        community=(ambassador.source == "community"),
        count=count,
        remaining_to_5=remaining_to_5,
        referral_url=referral_url,
        whatsapp_url=_whatsapp_share_url(wa_message),
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip('/'),
    )

    # Subject phrased conversationally — avoids hard-promo words like "free" /
    # "now" that can flag Gmail's promotions classifier. Keeps the personalized
    # number for curiosity-driven open rate. Subject prize differs by source.
    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    n_word = "unplug" if remaining_to_5 == 1 else "unplugs"
    if ambassador.source == "community":
        subject = f"{first}, {remaining_to_5} {n_word} from MetaDancers"
    else:
        subject = f"{first}, {remaining_to_5} {n_word} to your masterclass"
    return _send(
        ambassador.email,
        subject,
        html,
        template_key="activation_push",
        ambassador=ambassador,
    )


def send_class_ready_email(ambassador, app_url, class_number):
    """Manual admin send: announce that Class N is now live.

    Class 1 and 2 are pre-recorded lessons. Class 3 is the live-masterclass
    replay (the Zoom session uploaded to Bunny Stream after the fact).
    """
    if is_unsubscribed(ambassador):
        return False
    if class_number not in (1, 2, 3):
        raise ValueError(f"class_number must be 1, 2 or 3, got {class_number}")

    # Landing where the lesson lives. Each class has its own page.
    landing_root = _landing_url() or app_url.rstrip("/")
    class_url = f"{landing_root}/class{class_number}"

    html = render_template(
        "emails/class_ready.html",
        first_name=_first_name(ambassador),
        email=ambassador.email,
        community=(ambassador.source == "community"),
        class_number=class_number,
        class_url=class_url,
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip("/"),
    )

    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    if class_number == 1:
        subject = f"{first}, Class 01 just dropped — go watch it"
    elif class_number == 2:
        subject = f"{first}, Class 02 is unlocked — last one before the live"
    else:
        subject = f"{first}, Class 03 is live — the masterclass replay is up"

    return _send(
        ambassador.email,
        subject,
        html,
        template_key=f"class{class_number}_ready",
        ambassador=ambassador,
    )


def send_class1_ready_email(ambassador, app_url):
    return send_class_ready_email(ambassador, app_url, 1)


def send_class2_ready_email(ambassador, app_url):
    return send_class_ready_email(ambassador, app_url, 2)


def send_class3_ready_email(ambassador, app_url):
    return send_class_ready_email(ambassador, app_url, 3)


def send_class_rewatch_reminder_email(ambassador, app_url, class_number):
    """Weekend re-open: remind ambassadors who watched class N during the
    launch but haven't returned this weekend that the link is open again.

    Audience is computed by /admin/class-views ("sleepers" = first-watched
    before REWATCH_WINDOW_OPENS_AT, no view since). Idempotency lives on
    Ambassador.class{N}_rewatch_reminder_sent_at.
    """
    if is_unsubscribed(ambassador):
        return False
    if class_number not in (1, 2, 3):
        raise ValueError(f"class_number must be 1, 2 or 3, got {class_number}")

    landing_root = _landing_url() or app_url.rstrip("/")
    class_url = f"{landing_root}/class{class_number}"

    html = render_template(
        "emails/class_rewatch_reminder.html",
        first_name=_first_name(ambassador),
        email=ambassador.email,
        class_number=class_number,
        class_url=class_url,
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip("/"),
    )

    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    class_label = f"Class 0{class_number}"
    subject = f"{first}, the {class_label} replay is open this weekend"

    return _send(
        ambassador.email,
        subject,
        html,
        template_key=f"class{class_number}_rewatch_reminder",
        ambassador=ambassador,
    )


def send_class1_rewatch_reminder_email(ambassador, app_url):
    return send_class_rewatch_reminder_email(ambassador, app_url, 1)


def send_class2_rewatch_reminder_email(ambassador, app_url):
    return send_class_rewatch_reminder_email(ambassador, app_url, 2)


def send_class3_rewatch_reminder_email(ambassador, app_url):
    return send_class_rewatch_reminder_email(ambassador, app_url, 3)


def send_webinar_reminder_email(ambassador, app_url):
    """Manual admin send: 1-hour-before reminder for the live webinar.

    Reads the Zoom URL from env var WEBINAR_JOIN_URL (set in Render so we
    can update it without redeploying if the URL changes last-minute).
    """
    if is_unsubscribed(ambassador):
        return False

    join_url = os.getenv("WEBINAR_JOIN_URL", "").strip()
    if not join_url:
        logger.warning("WEBINAR_JOIN_URL not set — webinar reminder template will show a placeholder")
        join_url = f"{app_url.rstrip('/')}/webinar"

    html = render_template(
        "emails/webinar_reminder.html",
        first_name=_first_name(ambassador),
        community=(ambassador.source == "community"),
        join_url=join_url,
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip("/"),
    )

    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    subject = f"{first}, the live starts in 1 hour"

    return _send(
        ambassador.email,
        subject,
        html,
        template_key="webinar_reminder",
        ambassador=ambassador,
    )


def send_live_imminent_email(ambassador, app_url):
    """T-30min reminder: minimal layout, single Zoom CTA, urgent red banner.

    Hardcoded fallback Zoom URL ships in code so a missing
    WEBINAR_JOIN_URL env var can't break a time-critical send.
    Set WEBINAR_JOIN_URL in Render to override (also picked up by
    send_webinar_reminder_email so both reminders stay in sync).
    """
    if is_unsubscribed(ambassador):
        return False

    join_url = os.getenv("WEBINAR_JOIN_URL", "").strip() or (
        "https://us06web.zoom.us/j/82504511534"
        "?pwd=QRvMY8y5htQHjbn5pDVaTFVeYe8K6E.1"
    )

    html = render_template(
        "emails/live_imminent.html",
        first_name=_first_name(ambassador),
        join_url=join_url,
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip("/"),
    )

    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    subject = f"{first}, we go live in 30 minutes"

    return _send(
        ambassador.email,
        subject,
        html,
        template_key="live_imminent",
        ambassador=ambassador,
    )


def send_final_signal_email(ambassador, app_url):
    """T-minus reminder fired ~3h before the live: Class 2 closing + live tonight.

    Single CTA points to the public Instructions page where the recipient
    finds both the Class 2 link and the live access in one place.
    """
    if is_unsubscribed(ambassador):
        return False

    instructions_url = os.getenv(
        "INSTRUCTIONS_URL",
        "https://inevitable.metakizzproject.com/instructions",
    )

    html = render_template(
        "emails/final_signal.html",
        first_name=_first_name(ambassador),
        email=ambassador.email,
        instructions_url=instructions_url,
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
        app_url=app_url.rstrip("/"),
    )

    first = ambassador.name.split()[0] if ambassador.name else "Hey"
    subject = f"{first}, T-minus 2 hours — the live is today"

    return _send(
        ambassador.email,
        subject,
        html,
        template_key="final_signal",
        ambassador=ambassador,
    )


def send_activation_nudge_email(ambassador, app_url):
    """Send the activation nudge email (Day 2-3, only if 0 referrals)."""
    if is_unsubscribed(ambassador):
        return False

    referral_url = _share_url(ambassador)
    wa_message = (
        f"Hey, just registered for a free Urbankiz training with Jesus & Anni (MetaKizz). "
        f"2 videos + 1 live. Thought of you. {referral_url}"
    )

    count = ambassador.referral_count
    remaining_to_5 = max(0, 5 - count)
    html = render_template(
        "emails/activation_nudge.html",
        first_name=_first_name(ambassador),
        community=(ambassador.source == "community"),
        count=count,
        remaining_to_5=remaining_to_5,
        referral_url=referral_url,
        whatsapp_url=_whatsapp_share_url(wa_message),
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(
        ambassador.email,
        "We drafted a message. Copy-paste if you want.",
        html,
        template_key="activation_nudge",
        ambassador=ambassador,
    )


# ─── EMAIL 4: GUARANTEED PRIZE (5 unplugs) ───────────────────────

def send_guaranteed_prize_email(ambassador, position, app_url):
    """Send the email when an ambassador hits 5 unplugs (reward locked)."""
    if is_unsubscribed(ambassador):
        return False

    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"
    referral_url = _share_url(ambassador)
    wa_message = (
        f"Hey, just registered for a free Urbankiz training with Jesus & Anni (MetaKizz). "
        f"2 videos + 1 live. Thought of you. {referral_url}"
    )

    html = render_template(
        "emails/guaranteed_prize.html",
        first_name=_first_name(ambassador),
        community=(ambassador.source == "community"),
        position=position,
        leaderboard_url=f"{dashboard_url}#leaderboard",
        share_url=_whatsapp_share_url(wa_message),
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(
        ambassador.email,
        "5 unplugs. Your reward is locked.",
        html,
        template_key="guaranteed_prize",
        ambassador=ambassador,
    )


# ─── EMAIL 5: MIDWAY REMINDER (Day 7) ────────────────────────────

def send_midway_reminder_email(ambassador, position, days_left, app_url):
    """Send the midway check-in reminder with status + one tactic."""
    if is_unsubscribed(ambassador):
        return False

    count = ambassador.referral_count
    html = render_template(
        "emails/midway_reminder.html",
        first_name=_first_name(ambassador),
        count=count,
        position=position,
        days_left=days_left,
        remaining_to_5=max(0, 5 - count),
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(
        ambassador.email,
        "Halfway through. Here's where you stand.",
        html,
        template_key="midway_reminder",
        ambassador=ambassador,
    )


# ─── EMAIL 6: FINAL 48H ──────────────────────────────────────────

def send_final_48h_email(ambassador, position, gap_to_top3, app_url):
    """Send the 48-hour final reminder with state-conditional copy."""
    if is_unsubscribed(ambassador):
        return False

    count = ambassador.referral_count
    referral_url = _share_url(ambassador)
    wa_message = (
        f"Hey, free Urbankiz training week with Jesus & Anni. "
        f"2 videos + 1 live. Closes in 48h. Thought of you. {referral_url}"
    )

    html = render_template(
        "emails/final_48h.html",
        first_name=_first_name(ambassador),
        count=count,
        position=position,
        remaining_to_5=max(0, 5 - count),
        gap_to_top3=gap_to_top3,
        referral_url=referral_url,
        whatsapp_url=_whatsapp_share_url(wa_message),
        dashboard_url=f"{app_url}/dashboard/{ambassador.dashboard_code}",
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(
        ambassador.email,
        "48 hours to close.",
        html,
        template_key="final_48h",
        ambassador=ambassador,
    )


# ─── EMAIL 7: LAST 6 HOURS (count IN (3, 4) only) ────────────────

def send_last_6h_email(ambassador, app_url):
    """Send the last-6-hours sprint email. Caller must filter audience to count IN (3, 4)."""
    if is_unsubscribed(ambassador):
        return False

    count = ambassador.referral_count
    referral_url = _share_url(ambassador)
    wa_message = (
        f"Hey, free Urbankiz training closes tonight at 19:00 Madrid. "
        f"2 videos + 1 live with Jesus & Anni. Last call. {referral_url}"
    )

    n = max(0, 5 - count)
    subject = f"6 hours. {n} unplug{'s' if n != 1 else ''} from your reward."

    html = render_template(
        "emails/last_6h.html",
        first_name=_first_name(ambassador),
        count=count,
        community=(ambassador.source == "community"),
        remaining_to_5=n,
        referral_url=referral_url,
        whatsapp_url=_whatsapp_share_url(wa_message),
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(ambassador.email, subject, html, template_key="last_6h", ambassador=ambassador)


# ─── EMAIL 8: RESULTS ANNOUNCEMENT ───────────────────────────────

def send_results_announcement_email(ambassador, total_ambassadors, total_unplugs, total_countries, top3, app_url):
    """Send the post-close results announcement with collective stats + top 3.

    top3 is a list of dicts: [{"name": "Maria", "count": 23}, ...]
    """
    if is_unsubscribed(ambassador):
        return False

    html = render_template(
        "emails/results_announcement.html",
        first_name=_first_name(ambassador),
        count=ambassador.referral_count,
        total_ambassadors=total_ambassadors,
        total_unplugs=total_unplugs,
        total_countries=total_countries,
        top3=top3,
        ambassadors_minus_3=max(0, total_ambassadors - 3),
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(
        ambassador.email,
        "The Unplugging is closed. Here's what happened.",
        html,
        template_key="results",
        ambassador=ambassador,
    )


# ─── EMAIL 9: YOU WON (3 ramas) ──────────────────────────────────

def send_you_won_email(ambassador, position, app_url):
    """Send the You Won email. Picks the right rama based on count + position.

    rama 1: count >= 5 AND not top 3 (guaranteed only)
    rama 2: count >= 5 AND top 3 (guaranteed + ranking)
    rama 3: count <  5 AND top 3 (ranking only — edge case)
    """
    if is_unsubscribed(ambassador):
        return False

    count = ambassador.referral_count
    in_top3 = position in (1, 2, 3)
    has_guaranteed = count >= 5

    if has_guaranteed and not in_top3:
        rama = 1
    elif has_guaranteed and in_top3:
        rama = 2
    else:
        rama = 3  # edge case

    position_text = {1: "1st", 2: "2nd", 3: "3rd"}.get(position, "")
    ranking_name, ranking_value = _TOP3.get((ambassador.source, position), ("", None))
    next_step = _next_step_for_position(ambassador.source, position) if in_top3 else ""

    # Subject per rama
    if rama == 1:
        if ambassador.source == "community":
            subject = "You won 1 month of MetaDancers."
        else:
            subject = "You won the musicality masterclass."
    elif rama == 2:
        subject = f"You finished {position_text}. Two rewards to claim."
    else:
        subject = f"You finished {position_text} in The Unplugging."

    html = render_template(
        "emails/you_won.html",
        first_name=_first_name(ambassador),
        count=count,
        community=(ambassador.source == "community"),
        rama=rama,
        position=position,
        position_text=position_text,
        ranking_reward_name=ranking_name,
        ranking_reward_value=ranking_value,
        next_step_instruction=next_step,
        unsubscribe_url=_unsubscribe_url(ambassador, app_url),
    )

    return _send(ambassador.email, subject, html, template_key="you_won", ambassador=ambassador)


# ─── EMAIL 2 (LEGACY): FIRST REFERRAL ────────────────────────────

def send_first_referral_email(ambassador, referral_name, rank, next_tier, app_url):
    """Send celebration email for first referral."""
    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"

    next_info = ""
    if next_tier:
        next_info = f"""
<p style="color:#9CA3AF;font-size:13px;margin:4px 0 0 0;">NEXT MILESTONE: {next_tier.name} at {next_tier.threshold} dancers</p>"""

    stats = f"""
<p style="color:#2EDB99;font-size:28px;font-weight:bold;margin:0;">1</p>
<p style="color:#9CA3AF;font-size:13px;margin:2px 0 0 0;">DANCER UNPLUGGED</p>
<p style="color:#9CA3AF;font-size:13px;margin:4px 0 0 0;">YOUR RANK: #{rank}</p>
{next_info}"""

    content = f"""
<h1 style="color:#FFFFFF;font-size:22px;margin:0 0 8px 0;">{ambassador.name}, it's happening!</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.6;">
<strong style="color:#FFFFFF;">{referral_name}</strong> just got unplugged through your link. That's your first dancer — nice work.
</p>

{_stats_card(stats)}

<p style="color:#9CA3AF;font-size:14px;line-height:1.6;">
The first one is always the hardest. Now keep the momentum going — every dancer you unplug brings you closer to your next reward.
</p>

{_button("View Your Dashboard", dashboard_url)}

<p style="color:#6B7280;font-size:13px;margin:0;">Keep unplugging.</p>
"""
    return _send(
        ambassador.email,
        "Your first dancer is unplugged!",
        _wrap(content, app_url),
    )


# ─── EMAIL 3: NEW REFERRAL NOTIFICATION ──────────────────────────

def send_referral_notification_email(ambassador, referral_name, next_tier, app_url):
    """Send notification for each new referral (after the first)."""
    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"
    count = ambassador.referral_count

    # Progress toward next tier
    progress_section = ""
    if next_tier:
        remaining = next_tier.threshold - count
        if remaining <= 2:
            progress_section = f"""
<p style="color:#2EDB99;font-size:14px;font-weight:bold;margin:16px 0 0 0;">
You're SO close to {next_tier.name}. Just {remaining} more dancer{"s" if remaining != 1 else ""} and you unlock: {next_tier.reward}.
</p>"""
        else:
            progress_section = f"""
<p style="color:#9CA3AF;font-size:14px;margin:16px 0 0 0;">
Next milestone: {next_tier.name} at {next_tier.threshold} dancers. Keep sharing your link — every dancer counts.
</p>"""

    stats = f"""
<p style="color:#2EDB99;font-size:28px;font-weight:bold;margin:0;">{count}</p>
<p style="color:#9CA3AF;font-size:13px;margin:2px 0 0 0;">DANCERS UNPLUGGED</p>"""

    content = f"""
<h1 style="color:#FFFFFF;font-size:20px;margin:0 0 4px 0;">{referral_name} is in.</h1>

<p style="color:#9CA3AF;font-size:14px;line-height:1.6;">
Another dancer unplugged through your link, {ambassador.name}. You're building something.
</p>

{_stats_card(stats)}

{progress_section}

{_button("View Your Dashboard", dashboard_url)}
"""
    return _send(
        ambassador.email,
        f"{referral_name} just got unplugged through your link",
        _wrap(content, app_url),
    )


# ─── EMAIL 4: MILESTONE UNLOCKED ─────────────────────────────────

def send_milestone_email(ambassador, tier, next_tier, app_url):
    """Send celebration email when a reward tier is unlocked."""
    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"
    count = ambassador.referral_count

    next_section = ""
    if next_tier:
        remaining = next_tier.threshold - count
        next_section = f"""
<p style="color:#9CA3AF;font-size:14px;line-height:1.6;margin:16px 0 0 0;">
Next up: <strong style="color:#FFFFFF;">{next_tier.name}</strong> at {next_tier.threshold} dancers. You're {remaining} away. Keep going.
</p>"""
    else:
        next_section = """
<p style="color:#2EDB99;font-size:14px;font-weight:bold;margin:16px 0 0 0;">
You've reached the top tier. Legend status.
</p>"""

    tier_card = f"""
<p style="color:#2EDB99;font-size:20px;margin:0 0 8px 0;">&#9733; {tier.name}</p>
<p style="color:#FFFFFF;font-size:16px;font-weight:bold;margin:0 0 8px 0;">{tier.reward}</p>
<p style="color:#6B7280;font-size:13px;margin:0;">{count} dancers unplugged</p>"""

    content = f"""
<h1 style="color:#FFFFFF;font-size:22px;margin:0 0 8px 0;">{ambassador.name}, you just leveled up.</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.6;">
You've unplugged {count} dancers and unlocked:
</p>

{_stats_card(tier_card)}

{next_section}

{_button("View Your Dashboard", dashboard_url)}
"""
    return _send(
        ambassador.email,
        f"You unlocked {tier.name}!",
        _wrap(content, app_url),
    )


# ─── EMAIL 5: ALMOST THERE NUDGE ─────────────────────────────────

def send_almost_there_email(ambassador, next_tier, app_url):
    """Send nudge when ambassador is 1 referral away from next tier."""
    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"
    referral_url = _share_url(ambassador)
    count = ambassador.referral_count
    whatsapp_url = f"https://wa.me/?text={http_requests.utils.quote(f'Check out this masterclass by MetaKizz! {referral_url}')}"

    tier_card = f"""
<p style="color:#2EDB99;font-size:18px;font-weight:bold;margin:0 0 8px 0;">{next_tier.name}</p>
<p style="color:#FFFFFF;font-size:15px;margin:0 0 4px 0;">{next_tier.reward}</p>
<p style="color:#6B7280;font-size:13px;margin:0;">{next_tier.threshold} dancers needed</p>"""

    content = f"""
<h1 style="color:#FFFFFF;font-size:22px;margin:0 0 8px 0;">{ambassador.name}, you're one dancer away.</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.6;">
You've unplugged {count} dancers. Just <strong style="color:#2EDB99;">ONE more</strong> and you unlock:
</p>

{_stats_card(tier_card)}

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:24px 0 12px 0;">Quick Ways to Get That Last One</p>

<p style="color:#9CA3AF;font-size:14px;line-height:1.8;margin:0;">
&#8594; Send your link to one friend right now on WhatsApp<br>
&#8594; Post your QR code on Instagram stories<br>
&#8594; Share in a dance group chat
</p>

{_button("Share on WhatsApp", whatsapp_url, color="#25D366", text_color="#FFFFFF")}
{_button("Open My Dashboard", dashboard_url)}

<p style="color:#6B7280;font-size:13px;margin:0;">One more. Let's go.</p>
"""
    return _send(
        ambassador.email,
        f"1 more dancer to unlock {next_tier.name}",
        _wrap(content, app_url),
    )


# ─── PARTNER INVITE FLOW (Couple plan) ────────────────────────────

# Note: Circle sends the partner's invitation email automatically (we pass
# skip_invitation=False when creating the member). We only send a confirmation
# to the BUYER from this app — see send_partner_buyer_confirmation below.


def _first_name_from(full_name):
    """Extract a first name from a free-form 'full name' field."""
    if not full_name:
        return "there"
    parts = full_name.strip().split()
    return parts[0] if parts else "there"


def send_partner_buyer_confirmation(invite, app_url=None):
    """Confirmation email to the buyer after a successful partner invite."""
    if not invite or not invite.buyer_email:
        return False

    if app_url is None:
        from flask import current_app
        app_url = current_app.config.get("APP_URL", "")

    buyer_first = _first_name_from(invite.buyer_name)
    partner_first = _first_name_from(invite.partner_name)
    safe_partner_name = (invite.partner_name or partner_first).replace("<", "&lt;").replace(">", "&gt;")
    safe_partner_email = (invite.partner_email or "").replace("<", "&lt;").replace(">", "&gt;")

    whatsapp_url = "https://wa.me/34623960962"

    content = f"""
<!-- Status badge -->
<table cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;">
<tr><td style="background-color:#0A2A1F;border:1px solid #1a7a55;border-radius:999px;padding:6px 14px;">
    <span style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;">● ACCESS GRANTED</span>
</td></tr>
</table>

<h1 style="color:#FFFFFF;font-size:24px;line-height:1.25;margin:0 0 8px 0;">
    {partner_first} is in. <span style="color:#2EDB99;">🟢</span>
</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.7;margin:0 0 24px 0;">
    Hi {buyer_first} — your partner has been added to the Academy.
</p>

<!-- Partner card -->
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border:1px solid #1F2937;border-radius:14px;margin:0 0 24px 0;">
<tr><td style="padding:18px 20px;">
    <p style="color:#6B7280;font-family:'Share Tech Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;margin:0 0 10px 0;">▌ Partner added</p>
    <p style="color:#FFFFFF;font-size:17px;font-weight:bold;margin:0 0 4px 0;">{safe_partner_name}</p>
    <p style="color:#9CA3AF;font-size:13px;font-family:'Share Tech Mono','Courier New',monospace;margin:0;word-break:break-all;">{safe_partner_email}</p>
</td></tr>
</table>

<p style="color:#E5E7EB;font-size:15px;line-height:1.7;margin:0 0 6px 0;">
    They'll receive their welcome email in the next few minutes.
</p>
<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:0 0 24px 0;">
    Didn't show up? Ask them to check spam.
</p>

<!-- WhatsApp CTA -->
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A1A0F;border:1px solid #1a7a55;border-radius:14px;margin:0 0 8px 0;">
<tr><td style="padding:20px;">
    <p style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;margin:0 0 8px 0;">▌ Need anything?</p>
    <p style="color:#FFFFFF;font-size:15px;line-height:1.5;margin:0 0 14px 0;">
        Hit me on WhatsApp — I'm here to help with anything either of you need.
    </p>
    <table cellpadding="0" cellspacing="0">
    <tr><td style="background-color:#25D366;border-radius:10px;">
        <a href="{whatsapp_url}" style="display:inline-block;padding:12px 22px;color:#FFFFFF;text-decoration:none;font-weight:bold;font-size:14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
            💬&nbsp;&nbsp;Chat on WhatsApp
        </a>
    </td></tr>
    </table>
</td></tr>
</table>

<p style="color:#6B7280;font-size:13px;line-height:1.6;margin:28px 0 0 0;">
    See you both on the other side.<br>
    <span style="color:#9CA3AF;">— Álvaro</span>
</p>
"""

    return _send(
        invite.buyer_email,
        f"{partner_first} is in 🟢 — your partner has access",
        _wrap(content, app_url),
    )


def send_partner_invite_failure_alert(invite, error_summary, app_url=None):
    """Internal alert email to the admin when a partner invite Circle add fails.

    Goes to ADMIN_NOTIFICATION_EMAIL (falls back to EMAIL_FROM address).
    """
    admin_email = os.getenv("ADMIN_NOTIFICATION_EMAIL", "").strip()
    if not admin_email:
        # Fall back to the sender address so failures aren't silently lost.
        default_from = os.getenv("EMAIL_FROM", "")
        if "<" in default_from:
            admin_email = default_from.split("<", 1)[-1].rstrip(">").strip()
        else:
            admin_email = default_from.strip()
    if not admin_email:
        logger.warning("no ADMIN_NOTIFICATION_EMAIL set, skipping admin alert")
        return False

    if app_url is None:
        from flask import current_app
        app_url = current_app.config.get("APP_URL", "")

    safe_note = ""
    if invite.personal_note:
        safe_note = invite.personal_note.replace("<", "&lt;").replace(">", "&gt;")

    safe_error = (error_summary or "").replace("<", "&lt;").replace(">", "&gt;")

    rows = [
        ("Buyer name", invite.buyer_name or ""),
        ("Buyer email", invite.buyer_email or ""),
        ("Partner name", invite.partner_name or ""),
        ("Partner email", invite.partner_email or ""),
        ("Location", invite.location or "—"),
        ("Personal note", safe_note or "—"),
    ]

    rows_html = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;vertical-align:top;">{k}</td>'
        f'<td style="padding:6px 0;color:#FFFFFF;font-size:14px;word-break:break-word;">{v}</td></tr>'
        for k, v in rows
    )

    content = f"""
<h1 style="color:#FFFFFF;font-size:20px;margin:0 0 12px 0;">⚠️ Partner invite failed</h1>

<p style="color:#9CA3AF;font-size:14px;line-height:1.6;">
The Circle add step failed for the buyer below. The buyer was shown the friendly fallback message ("Álvaro will reach out personally..."). You'll need to add the partner manually.
</p>

<table cellpadding="0" cellspacing="0" style="margin:18px 0;width:100%;background-color:#0A0F0A;border:1px solid #1A1A2E;border-radius:8px;padding:8px 12px;">
{rows_html}
</table>

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:18px 0 8px 0;">Circle response</p>
<pre style="color:#E5E7EB;font-size:12px;background-color:#0A0F0A;padding:10px;border-radius:6px;white-space:pre-wrap;word-break:break-all;margin:0;">{safe_error}</pre>

<p style="color:#6B7280;font-size:12px;margin:18px 0 0 0;">
PartnerInvite id #{invite.id} — see /admin/partner-invites for full row.
</p>
"""

    subject = f"⚠️ Partner invite failed — {invite.buyer_email}"
    return _send(admin_email, subject, _wrap(content, app_url), from_name="MetaKizz Alerts")


def send_refund_admin_alert(email, reason, reservations, circle_charge_id=None,
                             circle_amount_cents=None, app_url=None):
    """Internal alert when the auto-refund flow needs human review.

    Triggered by:
      - Multiple deposit reservations match the same buyer email
      - Stripe refund call failed (auth, network, etc.)
      - Configuration missing (STRIPE_DEPOSIT_API_KEY, payment_intent_id)
    """
    admin_email = os.getenv("ADMIN_NOTIFICATION_EMAIL", "").strip()
    if not admin_email:
        default_from = os.getenv("EMAIL_FROM", "")
        if "<" in default_from:
            admin_email = default_from.split("<", 1)[-1].rstrip(">").strip()
        else:
            admin_email = default_from.strip()
    if not admin_email:
        logger.warning("no ADMIN_NOTIFICATION_EMAIL set, skipping refund admin alert")
        return False

    if app_url is None:
        from flask import current_app
        try:
            app_url = current_app.config.get("APP_URL", "")
        except Exception:
            app_url = ""

    safe_email = (email or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_reason = (reason or "").replace("<", "&lt;").replace(">", "&gt;")

    rows_html = ""
    for r in (reservations or []):
        paid = r.paid_at.strftime("%Y-%m-%d %H:%M") if r.paid_at else "—"
        amt = (r.amount_cents or 0) / 100
        rows_html += (
            f'<tr>'
            f'<td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;">#{r.id}</td>'
            f'<td style="padding:6px 12px 6px 0;color:#FFFFFF;font-size:13px;">€{amt:.2f}</td>'
            f'<td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;">{paid}</td>'
            f'<td style="padding:6px 0;color:#9CA3AF;font-size:12px;font-family:monospace;word-break:break-all;">{r.stripe_payment_intent_id or "(no pi)"}</td>'
            f'</tr>'
        )

    circle_amt_str = ""
    if circle_amount_cents is not None:
        circle_amt_str = f"€{(circle_amount_cents or 0) / 100:.2f}"

    content = f"""
<h1 style="color:#FFFFFF;font-size:20px;margin:0 0 12px 0;">⚠️ Refund needs review</h1>

<p style="color:#9CA3AF;font-size:14px;line-height:1.6;">
A buyer just paid the full plan in the Circle Stripe account, but the
auto-refund could not be issued automatically.
</p>

<table cellpadding="0" cellspacing="0" style="margin:18px 0;width:100%;background-color:#0A0F0A;border:1px solid #1A1A2E;border-radius:8px;padding:8px 12px;">
<tr>
    <td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;">Buyer email</td>
    <td style="padding:6px 0;color:#FFFFFF;font-size:14px;">{safe_email}</td>
</tr>
<tr>
    <td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;">Circle charge</td>
    <td style="padding:6px 0;color:#FFFFFF;font-size:13px;font-family:monospace;word-break:break-all;">{circle_charge_id or "—"} {("(" + circle_amt_str + ")") if circle_amt_str else ""}</td>
</tr>
<tr>
    <td style="padding:6px 12px 6px 0;color:#9CA3AF;font-size:13px;">Reason</td>
    <td style="padding:6px 0;color:#FBBF24;font-size:14px;">{safe_reason}</td>
</tr>
</table>

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:18px 0 8px 0;">Matching deposit reservations</p>

<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border:1px solid #1A1A2E;border-radius:8px;padding:10px 14px;">
<tr style="color:#6B7280;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
    <td style="padding:6px 12px 6px 0;">ID</td>
    <td style="padding:6px 12px 6px 0;">Amount</td>
    <td style="padding:6px 12px 6px 0;">Paid at</td>
    <td style="padding:6px 0;">Payment Intent</td>
</tr>
{rows_html or '<tr><td colspan="4" style="padding:10px 0;color:#6B7280;font-size:13px;">(none)</td></tr>'}
</table>

<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:20px 0 0 0;">
Open <strong style="color:#FFFFFF;">/admin/reservations</strong> to review and refund manually if needed.
</p>
"""

    subject = f"⚠️ Refund needs review — {safe_email}"
    return _send(admin_email, subject, _wrap(content, app_url), from_name="MetaKizz Alerts")


def build_refund_confirmation_html(reservation, app_url=None):
    """Build the HTML body for the refund confirmation email. Pure function
    so the admin preview endpoint can render it without sending.
    """
    if app_url is None:
        from flask import current_app
        try:
            app_url = current_app.config.get("APP_URL", "")
        except Exception:
            app_url = ""

    first_name = "there"
    if getattr(reservation, "name", None) and reservation.name.strip():
        first_name = reservation.name.strip().split()[0]
    elif getattr(reservation, "ambassador", None) and getattr(reservation.ambassador, "name", None):
        first_name = reservation.ambassador.name.strip().split()[0]

    amount = (
        getattr(reservation, "refund_amount_cents", None)
        or getattr(reservation, "amount_cents", None)
        or 10000
    ) / 100
    whatsapp_url = "https://wa.me/34623960962"

    content = f"""
<!-- Status badge -->
<table cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;">
<tr><td style="background-color:#0A2A1F;border:1px solid #1a7a55;border-radius:999px;padding:6px 14px;">
    <span style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;">● REFUND ON THE WAY</span>
</td></tr>
</table>

<h1 style="color:#FFFFFF;font-size:24px;line-height:1.25;margin:0 0 10px 0;">
    Your €{amount:.0f} deposit is on its way back. <span style="color:#2EDB99;">🟢</span>
</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.7;margin:0 0 24px 0;">
    Hi {first_name} — thanks for going all-in with us on MKOT 3.0.
</p>

<!-- Amount card -->
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border:1px solid #1F2937;border-radius:14px;margin:0 0 24px 0;">
<tr><td style="padding:18px 20px;">
    <p style="color:#6B7280;font-family:'Share Tech Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;margin:0 0 10px 0;">▌ Refund details</p>
    <p style="color:#FFFFFF;font-size:22px;font-weight:bold;margin:0 0 4px 0;">€{amount:.2f}</p>
    <p style="color:#9CA3AF;font-size:13px;margin:0;">Your reservation deposit, returned automatically now that you've completed your full enrollment.</p>
</td></tr>
</table>

<p style="color:#E5E7EB;font-size:15px;line-height:1.7;margin:0 0 8px 0;">
    The refund has been issued to the same card you paid with. Most banks show it within
    <strong style="color:#FFFFFF;">5–10 business days</strong>.
</p>
<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:0 0 24px 0;">
    Don't see it after 10 days? Check with your bank first, then ping us.
</p>

<!-- WhatsApp CTA -->
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A1A0F;border:1px solid #1a7a55;border-radius:14px;margin:0 0 8px 0;">
<tr><td style="padding:20px;">
    <p style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;margin:0 0 8px 0;">▌ Questions?</p>
    <p style="color:#FFFFFF;font-size:15px;line-height:1.5;margin:0 0 14px 0;">
        WhatsApp me directly — fastest way to reach me.
    </p>
    <table cellpadding="0" cellspacing="0">
    <tr><td style="background-color:#25D366;border-radius:10px;">
        <a href="{whatsapp_url}" style="display:inline-block;padding:12px 22px;color:#FFFFFF;text-decoration:none;font-weight:bold;font-size:14px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
            💬&nbsp;&nbsp;Chat on WhatsApp
        </a>
    </td></tr>
    </table>
</td></tr>
</table>

<p style="color:#6B7280;font-size:13px;line-height:1.6;margin:28px 0 0 0;">
    See you on the other side.<br>
    <span style="color:#9CA3AF;">— Álvaro</span>
</p>
"""

    return _wrap(content, app_url), amount


def send_refund_confirmation_email(reservation, app_url=None):
    """Notify the buyer that their €100 deposit refund is on the way.

    Triggered:
      - automatically when /api/webhook/stripe-circle issues a real refund
      - manually when admin clicks "Mark as refunded + send email" on a row
    """
    if not reservation or not reservation.email:
        return False
    html, amount = build_refund_confirmation_html(reservation, app_url=app_url)
    return _send(
        reservation.email,
        f"Your €{amount:.0f} deposit is on its way back 🟢",
        html,
    )


# ─── INVOICES ─────────────────────────────────────────────────────

def build_invoice_email_html(circle_payment, invoice_number, app_url=None):
    """Branded HTML body for the email that delivers the invoice PDF."""
    if app_url is None:
        from flask import current_app
        try:
            app_url = current_app.config.get("APP_URL", "")
        except Exception:
            app_url = ""

    biz_name = os.getenv("INVOICE_BUSINESS_NAME", "Virtual Flow LLC").strip()
    contact_email = os.getenv("INVOICE_BUSINESS_EMAIL", "info@metakizzproject.com").strip()

    first_name = "there"
    if circle_payment.customer_name and circle_payment.customer_name.strip():
        first_name = circle_payment.customer_name.strip().split()[0]

    amount = (circle_payment.amount_cents or 0) / 100
    currency = (circle_payment.currency or "usd").upper()
    description = circle_payment.description or "Digital services"

    safe_desc = description.replace("<", "&lt;").replace(">", "&gt;")

    content = f"""
<table cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;">
<tr><td style="background-color:#0A0A0A;border:1px solid #2EDB99;border-radius:999px;padding:6px 14px;">
    <span style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;">📄 INVOICE ATTACHED</span>
</td></tr>
</table>

<h1 style="color:#FFFFFF;font-size:22px;line-height:1.25;margin:0 0 12px 0;">
    Invoice {invoice_number}
</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.7;margin:0 0 22px 0;">
    Hi {first_name} — your receipt and invoice for the payment below are
    attached as a PDF for your records.
</p>

<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border:1px solid #1F2937;border-radius:14px;margin:0 0 24px 0;">
<tr><td style="padding:18px 20px;">
    <p style="color:#6B7280;font-family:'Share Tech Mono','Courier New',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;margin:0 0 10px 0;">▌ Receipt</p>
    <p style="color:#FFFFFF;font-size:22px;font-weight:bold;margin:0 0 4px 0;">{currency} {amount:,.2f}</p>
    <p style="color:#9CA3AF;font-size:14px;margin:0;">{safe_desc}</p>
</td></tr>
</table>

<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:0 0 8px 0;">
    Issued by {biz_name}.<br>
    Status: <strong style="color:#2EDB99;">PAID</strong>
</p>

<p style="color:#6B7280;font-size:13px;line-height:1.6;margin:24px 0 0 0;">
    Questions? Reply to this email or write to <a href="mailto:{contact_email}" style="color:#2EDB99;">{contact_email}</a>.
</p>
"""
    return _wrap(content, app_url)


def send_invoice_email(circle_payment, invoice_number, pdf_bytes, app_url=None):
    """Send the invoice PDF as an attachment via Resend.

    Returns True on success, False otherwise. Does NOT stamp invoice_sent_at —
    the caller is responsible (so we can keep that single source of truth in
    the admin route).
    """
    from app.services.invoice_pdf import safe_pdf_filename

    if not circle_payment or not circle_payment.email:
        return False
    if not invoice_number:
        return False
    if not pdf_bytes:
        return False

    biz_name = os.getenv("INVOICE_BUSINESS_NAME", "Virtual Flow LLC").strip()
    subject = f"Your invoice from {biz_name} — {invoice_number}"
    html = build_invoice_email_html(circle_payment, invoice_number, app_url=app_url)
    filename = safe_pdf_filename(
        invoice_number,
        customer_name=circle_payment.customer_name,
        customer_email=circle_payment.email,
    )

    return _send_with_attachment(
        to=circle_payment.email,
        subject=subject,
        html=html,
        attachment_bytes=pdf_bytes,
        attachment_filename=filename,
        from_name=biz_name,
    )


# ─── BUDDY FINDER ─────────────────────────────────────────────────

def send_buddy_contact_relay(post, contactor_name, contactor_email, message_text, app_url=None):
    """Forward a contact-form message to the BuddyPost publisher.

    The publisher's email is NOT exposed to the contactor; the contactor's
    email IS the reply-to header so the publisher can reply directly if
    they want to take the conversation off-platform.
    """
    if not post or not contactor_email:
        return False
    target_email = (post.contact_email_override or
                    (post.ambassador.email if post.ambassador else None))
    if not target_email:
        logger.warning("buddy contact relay: no target email for post %s", post.id)
        return False

    if app_url is None:
        from flask import current_app
        try:
            app_url = current_app.config.get("APP_URL", "")
        except Exception:
            app_url = ""

    publisher_first = "there"
    if post.ambassador and post.ambassador.name:
        parts = post.ambassador.name.strip().split()
        if parts:
            publisher_first = parts[0]

    safe_msg = (message_text or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_name = (contactor_name or "").replace("<", "&lt;").replace(">", "&gt;")
    safe_city = (post.city or "").replace("<", "&lt;").replace(">", "&gt;")

    content = f"""
<table cellpadding="0" cellspacing="0" style="margin:0 0 20px 0;">
<tr><td style="background-color:#0A0A0A;border:1px solid #2EDB99;border-radius:999px;padding:6px 14px;">
    <span style="color:#2EDB99;font-family:'Share Tech Mono','Courier New',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;">🤝 NEW BUDDY MESSAGE</span>
</td></tr>
</table>

<h1 style="color:#FFFFFF;font-size:22px;line-height:1.25;margin:0 0 12px 0;">
    Hi {publisher_first}, someone wants to train with you.
</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.7;margin:0 0 20px 0;">
    A dancer found your profile on the MetaKizz Buddy Map (you in {safe_city}) and sent you a message:
</p>

<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#0A0F0A;border-left:3px solid #2EDB99;border-radius:6px;margin:0 0 24px 0;">
<tr><td style="padding:18px 20px;">
    <p style="color:#9CA3AF;font-size:11px;text-transform:uppercase;letter-spacing:2px;margin:0 0 8px 0;">From {safe_name}</p>
    <p style="color:#FFFFFF;font-size:15px;line-height:1.6;margin:0;font-style:italic;">"{safe_msg}"</p>
</td></tr>
</table>

<p style="color:#E5E7EB;font-size:14px;line-height:1.7;margin:0 0 12px 0;">
    Reply directly to this email to talk to {safe_name}. Your reply goes to <strong style="color:#FFFFFF;">{contactor_email}</strong>.
</p>
<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:0 0 24px 0;">
    Your email stays hidden from them until you write back.
</p>

<p style="color:#6B7280;font-size:12px;line-height:1.6;margin:24px 0 0 0;">
    — The MetaKizz Buddy Map<br>
    <span style="color:#9CA3AF;">If you ever want to take your profile down, head to your dashboard.</span>
</p>
"""

    subject = f"🤝 Someone wants to train with you in {post.city}"

    # Use the regular _send but inject a Reply-To header so the publisher's
    # reply lands directly in the contactor's inbox.
    api_key = os.getenv("RESEND_API_KEY")
    default_from = os.getenv("EMAIL_FROM", "MetaKizz <noreply@metakizzproject.com>")
    addr = default_from.split("<", 1)[-1].rstrip(">").strip() if "<" in default_from else default_from
    email_from = f"MetaKizz Buddy Map <{addr}>"
    if not api_key:
        logger.warning("RESEND_API_KEY not set, skipping buddy relay")
        return False

    payload = {
        "from": email_from,
        "to": [target_email],
        "subject": subject,
        "html": _wrap(content, app_url),
        "reply_to": [contactor_email],
    }
    try:
        resp = http_requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        if resp.status_code < 300:
            logger.info("buddy contact relay sent: post=%s to=%s from_addr=%s",
                        post.id, target_email, contactor_email)
            return True
        logger.error("buddy contact relay failed (%s): %s", resp.status_code, resp.text[:300])
        return False
    except Exception:
        logger.exception("buddy contact relay exception")
        return False


def send_buddy_renewal_reminder(post, app_url=None):
    """Send the 7-day-before-expiration nudge with a magic re-publish link.

    The link points to /buddies/<dashboard_code>/edit — when the
    publisher hits Save the post is renewed automatically.
    """
    if not post or not post.ambassador or not post.ambassador.email:
        return False
    if app_url is None:
        from flask import current_app
        try:
            app_url = current_app.config.get("APP_URL", "")
        except Exception:
            app_url = ""

    first = "there"
    if post.ambassador.name:
        parts = post.ambassador.name.strip().split()
        if parts:
            first = parts[0]

    edit_url = f"{(app_url or '').rstrip('/')}/buddies/{post.ambassador.dashboard_code}/edit"
    if post.expires_at:
        exp = post.expires_at
        if exp.tzinfo is not None:
            exp = exp.replace(tzinfo=None)
        days_left = max(1, (exp - datetime.utcnow()).days)
    else:
        days_left = 7

    content = f"""
<h1 style="color:#FFFFFF;font-size:22px;line-height:1.25;margin:0 0 12px 0;">
    Hi {first}, your buddy profile expires in {days_left} days.
</h1>

<p style="color:#9CA3AF;font-size:15px;line-height:1.7;margin:0 0 18px 0;">
    Still looking for someone to train with? Renew your post on the
    Metakizz Buddy Map in 1 click — same info, fresh 60 days.
</p>

{_button("Renew my profile →", edit_url)}

<p style="color:#9CA3AF;font-size:13px;line-height:1.6;margin:24px 0 0 0;">
    Already found someone? Cool — just ignore this and the post will
    expire on its own.
</p>

<p style="color:#6B7280;font-size:12px;margin:18px 0 0 0;">
    — The MetaKizz Buddy Map
</p>
"""
    return _send(
        post.ambassador.email,
        "Your buddy profile expires soon — renew in 1 click",
        _wrap(content, app_url),
    )
