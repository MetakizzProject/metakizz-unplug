# MetaKizz Ambassador App

Flask app that powers the MetaKizz Ambassador Challenge, MKOT 3.0 reservation
flow, and partner invites. Deployed on Render.

## Local development

```bash
# Install
pip install -r requirements.txt

# Set up env
cp .env.example .env   # then fill in values

# Run
python -m flask --app app.app:create_app run
# → http://localhost:5000
```

## Partner Invite flow (MKOT 3.0 Couple plan)

Lets a buyer who paid the Couple plan submit their partner's info. The
backend looks up the buyer in Circle, mirrors their access group
(Dancers or Instructors), and adds the partner to the same group via the
Circle V2 admin API. Resend then sends a welcome to the partner + a
confirmation to the buyer.

### URLs

- Public form: `/invite-partner`
- API endpoint: `POST /api/invite-partner` (JSON)
- Admin list: `/admin/partner-invites` (login first via `/admin/login`)

### Required env vars

```
CIRCLE_API_TOKEN                    # Circle V2 admin API token
CIRCLE_COMMUNITY_ID                 # 161276
CIRCLE_ACCESS_GROUP_DANCERS_ID      # Access group ID for the Dancers track
CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID  # Access group ID for the Instructors track
RESEND_API_KEY                      # Resend API key
EMAIL_FROM                          # MetaKizz <noreply@metakizzproject.com>
ADMIN_NOTIFICATION_EMAIL            # Where admin alerts go on Circle failure
```

### How the access-group mirroring works

1. We search Circle for the buyer by email.
2. We list the access groups the buyer belongs to.
3. If they're in **Dancers** → partner goes to Dancers.
4. If they're in **Instructors** → partner goes to Instructors.
5. If they're in **both** → partner defaults to Dancers (admin alert flags it).
6. If they're in **neither** → admin alert; buyer sees the friendly fallback.

### Test it locally

1. Start the app (`flask run`).
2. Visit `http://localhost:5000/invite-partner` and submit the form.
3. Check `/admin/partner-invites` to see the row land. The `Circle` column
   shows `created` (new partner), `added` (already a Circle member, added
   to the group), or `failed` (admin gets an email alert).
4. Curl example (skips the form):

   ```bash
   curl -X POST http://localhost:5000/api/invite-partner \
     -H 'Content-Type: application/json' \
     -d '{
       "buyer_name": "Test Buyer",
       "buyer_email": "buyer@example.com",
       "partner_name": "Test Partner",
       "partner_email": "partner@example.com",
       "location": "Madrid",
       "personal_note": "Excited to dance with you!"
     }'
   ```

### Failure handling

| Status | What happens |
|---|---|
| `created` | Partner did not exist in Circle, was created and added to the buyer's access group. |
| `added_to_group` | Partner already had a Circle account; we just added them to the buyer's access group. |
| `buyer_missing` | Buyer email not in the Circle community. Buyer is told to double-check the email. No admin alert (assumes typo). |
| `buyer_no_group` | Buyer is in neither Dancers nor Instructors. Admin alert; buyer sees friendly fallback. |
| `failed` | Auth / network / 5xx error. Admin alert; buyer sees friendly fallback. |
| `needs_followup=True` | Circle add succeeded but the partner welcome email failed. Row shows a yellow pill on the admin page. |

### Production deploy checklist

1. Push to main; let Render redeploy.
2. Set `CIRCLE_ACCESS_GROUP_DANCERS_ID`, `CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID`,
   and `ADMIN_NOTIFICATION_EMAIL` in the Render dashboard before sharing the
   form URL.
3. The `partner_invites` table is created automatically on first request
   (via `db.create_all()` in `create_app()`); the `target_group` column is
   added by an idempotent migration.
4. Send one real test invite to your own second email to verify Circle
   add + both emails arrive.
