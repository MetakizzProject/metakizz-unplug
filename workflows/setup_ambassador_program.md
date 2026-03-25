# Setup Ambassador Program

## Objective
Set up the MetaKizz Ambassador Challenge from scratch — database, ambassador profiles, reward tiers, and deployment.

## Prerequisites
- `.env` file configured with all required credentials
- Python 3.10+ installed
- Circle API token and community ID (for auto-importing community members)

## Steps

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Initialize Database
```bash
python tools/db_init.py --seed
```
This creates the SQLite database and seeds sample reward tiers. You can customize tiers later from the admin panel (`/admin/tiers`).

### 3. Import Circle Community Members
```bash
python tools/circle_fetch_members.py
```
This fetches all members from your Circle community and creates ambassador profiles with:
- Name and email from Circle
- Profile picture URL from Circle
- Unique referral code and dashboard code
- QR code generated for each ambassador

**Note:** If Circle API credentials are not set up yet, you can add ambassadors manually through the `/join` page or by creating a simple script.

### 4. Configure Reward Tiers
1. Go to `/admin` → login with the ADMIN_PASSWORD from `.env`
2. Click "Manage Tiers"
3. Add your reward tiers for both Community and Public challenges
4. Set the threshold (number of referrals) and reward description for each

### 5. Test Locally
```bash
python app/app.py
```
Visit `http://localhost:5000` and:
- Check the home page email lookup works
- Test the `/join` page creates a new public ambassador
- Click a referral link → register a test person → verify it appears in the dashboard
- Check the leaderboard shows data
- Verify the admin panel works

### 6. Deploy to Hostinger VPS
See `deploy.md` for full deployment instructions.

### 7. Announce the Challenge
**In Circle:** Post a message with the link to `challenge.metakizzproject.com`
- "The MetaKizz Ambassador Challenge is live! Go to [link] to see your personal dashboard, get your referral link, and start competing for prizes!"

**On Instagram:** Post/story with the link to `challenge.metakizzproject.com/join`
- "Want to earn rewards? Join the MetaKizz Ambassador Challenge and bring your friends to our free masterclass!"

## Edge Cases
- **Member not found by email**: They should use `/join` to create a public profile, or check they're using the same email as their Circle account
- **Duplicate registrations**: The system rejects duplicate emails for both ambassadors and referrals
- **QR code not loading**: Check that `app/static/qrcodes/` directory exists and has write permissions

## Tools Used
- `tools/db_init.py` — Database initialization
- `tools/circle_fetch_members.py` — Circle member import
- `tools/check_milestones.py` — Milestone email notifications
- `tools/export_csv.py` — Data export
