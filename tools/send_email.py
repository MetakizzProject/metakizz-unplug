"""
Send emails via Resend API.
Used for milestone notifications and dashboard link delivery.

Usage:
    python tools/send_email.py --to email@example.com --subject "Subject" --body "HTML body"

Requires:
    RESEND_API_KEY and EMAIL_FROM in .env
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from dotenv import load_dotenv

load_dotenv()

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to, subject, html_body):
    api_key = os.getenv("RESEND_API_KEY")
    email_from = os.getenv("EMAIL_FROM", "MetaKizz <noreply@metakizzproject.com>")

    if not api_key:
        print("ERROR: Set RESEND_API_KEY in .env")
        return False

    resp = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": email_from,
            "to": [to],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )

    if resp.status_code == 200:
        print(f"Email sent to {to}: {subject}")
        return True
    else:
        print(f"ERROR sending email to {to}: {resp.status_code} {resp.text}")
        return False


def send_milestone_email(ambassador_name, ambassador_email, tier_name, reward, dashboard_url, referral_count):
    """Send a milestone notification email."""
    subject = f"You hit a new milestone! — {tier_name}"
    html = f"""
    <div style="font-family: system-ui, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2EDB99;">Congratulations, {ambassador_name}!</h2>
        <p>You've reached the <strong>{tier_name}</strong> milestone — you've unplugged <strong>{referral_count} dancers</strong>!</p>
        <div style="background: #1a1a2e; border: 1px solid #333; border-radius: 12px; padding: 16px; margin: 20px 0;">
            <p style="margin: 0; color: #ccc;">Your reward:</p>
            <p style="margin: 8px 0 0 0; font-size: 18px; font-weight: bold; color: #2EDB99;">{reward}</p>
        </div>
        <p>Keep going! Share your link and unplug more dancers into the masterclass.</p>
        <a href="{dashboard_url}" style="display: inline-block; background: #2EDB99; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: bold; margin-top: 10px;">
            View Your Dashboard
        </a>
        <p style="color: #888; font-size: 12px; margin-top: 30px;">MetaKizz Ambassador Challenge</p>
    </div>
    """
    return send_email(ambassador_email, subject, html)


def send_dashboard_link_email(ambassador_name, ambassador_email, dashboard_url):
    """Send an ambassador their dashboard link."""
    subject = "Your MetaKizz Ambassador Dashboard"
    html = f"""
    <div style="font-family: system-ui, sans-serif; max-width: 500px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #2EDB99;">Hey {ambassador_name}!</h2>
        <p>Here's your personal Ambassador Challenge dashboard link. Bookmark it for easy access:</p>
        <a href="{dashboard_url}" style="display: inline-block; background: #2EDB99; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; font-weight: bold; margin: 20px 0;">
            Open My Dashboard
        </a>
        <p>From your dashboard you can:</p>
        <ul>
            <li>Copy your referral link and QR code</li>
            <li>Track how many people you've brought in</li>
            <li>See your position on the leaderboard</li>
            <li>Check your reward milestones</li>
        </ul>
        <p style="color: #888; font-size: 12px; margin-top: 30px;">MetaKizz Ambassador Challenge</p>
    </div>
    """
    return send_email(ambassador_email, subject, html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send an email via Resend")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="HTML body")
    args = parser.parse_args()

    send_email(args.to, args.subject, args.body)
