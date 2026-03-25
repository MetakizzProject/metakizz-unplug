"""
Fetch all members from a Circle community (V2 Admin API) and create ambassador profiles.
Pulls name, email, avatar, and Instagram handle from Circle.

Usage:
    python tools/circle_fetch_members.py

Requires:
    CIRCLE_API_TOKEN and CIRCLE_COMMUNITY_ID in .env
"""

import sys
import os
import secrets

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import qrcode
from dotenv import load_dotenv

load_dotenv()

from app.app import create_app
from app.models import db, Ambassador


CIRCLE_API_BASE = "https://app.circle.so/api/admin/v2"


def fetch_circle_members():
    token = os.getenv("CIRCLE_API_TOKEN")
    community_id = os.getenv("CIRCLE_COMMUNITY_ID")

    if not token or not community_id:
        print("ERROR: Set CIRCLE_API_TOKEN and CIRCLE_COMMUNITY_ID in .env")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    members = []
    page = 1

    print("Fetching members from Circle (V2 Admin API)...")

    while True:
        resp = requests.get(
            f"{CIRCLE_API_BASE}/community_members",
            headers=headers,
            params={"community_id": community_id, "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        records = data.get("records", [])
        if not records:
            break

        members.extend(records)
        print(f"  Fetched page {page} ({len(records)} members, total so far: {len(members)})")

        if not data.get("has_next_page", False):
            break
        page += 1

    print(f"Total members fetched: {len(members)}")
    return members


def create_ambassadors(members):
    app = create_app()
    app_url = app.config["APP_URL"]

    with app.app_context():
        created = 0
        skipped = 0

        for member in members:
            email = (member.get("email") or "").strip().lower()
            if not email:
                skipped += 1
                continue

            # Skip if already exists
            if Ambassador.query.filter_by(email=email).first():
                skipped += 1
                continue

            name = member.get("name") or ""
            if not name:
                first = member.get("first_name", "")
                last = member.get("last_name", "")
                name = f"{first} {last}".strip()
            if not name:
                name = email.split("@")[0]

            referral_code = secrets.token_urlsafe(6)[:8]
            dashboard_code = secrets.token_urlsafe(6)[:8]

            while Ambassador.query.filter_by(referral_code=referral_code).first():
                referral_code = secrets.token_urlsafe(6)[:8]
            while Ambassador.query.filter_by(dashboard_code=dashboard_code).first():
                dashboard_code = secrets.token_urlsafe(6)[:8]

            avatar_url = member.get("avatar_url")

            # Extract Instagram handle from profile fields
            instagram_handle = None
            profile_fields = member.get("flattened_profile_fields", {})
            if isinstance(profile_fields, dict):
                ig_url = profile_fields.get("instagram_url", "")
                if ig_url:
                    # Extract handle from Instagram URL
                    ig_url = ig_url.split("?")[0].rstrip("/")
                    instagram_handle = ig_url.split("/")[-1] if "/" in ig_url else ig_url

            ambassador = Ambassador(
                name=name,
                email=email,
                referral_code=referral_code,
                dashboard_code=dashboard_code,
                source="community",
                profile_picture_url=avatar_url,
                circle_member_id=str(member.get("id", "")),
                instagram_handle=instagram_handle,
            )
            db.session.add(ambassador)
            db.session.flush()

            # Generate QR code
            _generate_qr(ambassador, app_url, app.root_path)
            created += 1

        db.session.commit()
        print(f"\nDone! Created {created} ambassadors, skipped {skipped} (already exist or no email).")


def _generate_qr(ambassador, app_url, app_root):
    referral_url = f"{app_url}/r/{ambassador.referral_code}"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(referral_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    qr_dir = os.path.join(app_root, "static", "qrcodes")
    os.makedirs(qr_dir, exist_ok=True)
    img.save(os.path.join(qr_dir, f"{ambassador.referral_code}.png"))


if __name__ == "__main__":
    members = fetch_circle_members()
    create_ambassadors(members)
