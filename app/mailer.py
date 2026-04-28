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

    try:
        resp = http_requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": email_from,
                "to": [to],
                "subject": subject,
                "html": html,
            },
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
