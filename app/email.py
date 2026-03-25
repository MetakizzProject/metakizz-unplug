"""
Branded email system for MetaKizz Ambassador Challenge.
All emails are sent via Resend API with The Unplugging narrative.
"""

import os
import logging
import requests as http_requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def _send(to, subject, html):
    """Send an email via Resend. Returns True on success."""
    api_key = os.getenv("RESEND_API_KEY")
    email_from = os.getenv("EMAIL_FROM", "MetaKizz <noreply@metakizzproject.com>")

    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
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
        if resp.status_code == 200:
            logger.info("Email sent to %s: %s", to, subject)
            return True
        else:
            logger.error("Email failed (%s) to %s: %s", resp.status_code, to, resp.text)
            return False
    except Exception as e:
        logger.error("Email exception to %s: %s", to, e)
        return False


def _wrap(content_html, app_url):
    """Wrap email content in the branded MetaKizz shell."""
    logo_url = f"{app_url}/static/brand/organized/logo-green.png"
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#000000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
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

<!-- Footer -->
<tr><td align="center" style="padding-top:24px;">
    <p style="color:#4B5563;font-size:12px;margin:0;">MetaKizz &middot; The Unplugging</p>
    <p style="color:#374151;font-size:11px;margin:6px 0 0 0;">You're receiving this because you joined the challenge.</p>
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
    """Send welcome email after joining the challenge."""
    referral_url = f"{app_url}/r/{ambassador.referral_code}"
    dashboard_url = f"{app_url}/dashboard/{ambassador.dashboard_code}"

    content = f"""
<h1 style="color:#FFFFFF;font-size:22px;margin:0 0 8px 0;">Hey {ambassador.name}!</h1>
<p style="color:#FFFFFF;font-size:16px;margin:0 0 20px 0;">Welcome to The Unplugging.</p>

<p style="color:#9CA3AF;font-size:14px;line-height:1.6;">
You're officially an ambassador. Your mission: unplug dancers into the MetaKizz masterclass and earn rewards along the way.
</p>

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:24px 0 8px 0;">Your Referral Link</p>
<p style="background-color:#1A1A2E;border:1px solid #2D2D44;border-radius:8px;padding:12px 16px;color:#FFFFFF;font-size:14px;word-break:break-all;margin:0;">
{referral_url}
</p>

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:24px 0 8px 0;">Your Dashboard</p>
<p style="background-color:#1A1A2E;border:1px solid #2D2D44;border-radius:8px;padding:12px 16px;color:#FFFFFF;font-size:14px;word-break:break-all;margin:0;">
{dashboard_url}
</p>
<p style="color:#6B7280;font-size:12px;margin:4px 0 0 0;">Bookmark this — it's your personal HQ.</p>

<hr style="border:none;border-top:1px solid #2D2D44;margin:28px 0;">

<p style="color:#2EDB99;font-size:12px;font-weight:bold;text-transform:uppercase;letter-spacing:1px;margin:0 0 12px 0;">Start Unplugging Now</p>

<p style="color:#9CA3AF;font-size:14px;line-height:1.7;margin:0;">
<strong style="color:#FFFFFF;">1.</strong> Send your link to 5 friends on WhatsApp right now. A personal message beats a group post every time.<br><br>
<strong style="color:#FFFFFF;">2.</strong> Download your QR code from your dashboard and share it on your Instagram stories.<br><br>
<strong style="color:#FFFFFF;">3.</strong> At a festival or dance class? Show your QR code on your phone screen — people can scan it instantly.
</p>

{_button("Open My Dashboard", dashboard_url)}

<p style="color:#6B7280;font-size:13px;margin:0;">See you on the leaderboard.</p>
"""
    return _send(
        ambassador.email,
        "You're in The Unplugging — here's your link",
        _wrap(content, app_url),
    )


# ─── EMAIL 2: FIRST REFERRAL ─────────────────────────────────────

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
    referral_url = f"{app_url}/r/{ambassador.referral_code}"
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
