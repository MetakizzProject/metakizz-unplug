# Onboard a Single Ambassador

## Objective
Add a new ambassador to the challenge manually (outside of the bulk Circle import).

## When to Use
- A new Circle member joins after the initial import
- Someone asks to join the community challenge directly
- You need to create a test ambassador

## Option A: Via the Web App (simplest)
1. Have them visit `challenge.metakizzproject.com/join`
2. They fill in name, email, and optionally Instagram handle
3. They're automatically created as a "public" ambassador

**To make them a community ambassador instead**, update their source in the admin panel or database.

## Option B: Via the Circle Fetch Tool
If they're already a Circle member:
```bash
python tools/circle_fetch_members.py
```
This will only create profiles for members who don't already exist — safe to re-run.

## Option C: Via Python (for scripting)
```python
from app.app import create_app
from app.models import db, Ambassador
from app.routes.home import _generate_qr
import secrets

app = create_app()
with app.app_context():
    amb = Ambassador(
        name="Maria Garcia",
        email="maria@example.com",
        referral_code=secrets.token_urlsafe(6)[:8],
        dashboard_code=secrets.token_urlsafe(6)[:8],
        source="community",
        instagram_handle="maria_dances",
    )
    db.session.add(amb)
    db.session.commit()
    _generate_qr(amb, app.config["APP_URL"])
    print(f"Created: /dashboard/{amb.dashboard_code}")
```

## After Onboarding
- The ambassador can access their dashboard at `/dashboard/{code}` by entering their email at the home page
- Optionally send them their dashboard link via email:
  ```bash
  python tools/send_email.py --to maria@example.com --subject "Your MetaKizz Dashboard" --body "<a href='https://challenge.metakizzproject.com/dashboard/{code}'>Your Dashboard</a>"
  ```
