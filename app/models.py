import secrets
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _generate_unsubscribe_token():
    """Random URL-safe token for one-click unsubscribe links."""
    return secrets.token_urlsafe(24)


class Ambassador(db.Model):
    __tablename__ = "ambassadors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    referral_code = db.Column(db.String(20), unique=True, nullable=False)
    dashboard_code = db.Column(db.String(20), unique=True, nullable=False)
    source = db.Column(db.String(20), default="community")  # "community" or "public"
    instagram_handle = db.Column(db.String(100))
    profile_picture_url = db.Column(db.String(1000))
    circle_member_id = db.Column(db.String(100))
    shared_on_instagram = db.Column(db.Boolean, default=False)
    instagram_proof_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Email opt-out (legal compliance + deliverability).
    # unsubscribe_token: random secret used in /unsubscribe/<token> links.
    # unsubscribed_at: nullable timestamp; if set, no further emails are sent.
    unsubscribe_token = db.Column(db.String(64), unique=True, default=_generate_unsubscribe_token)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)

    # Welcome email idempotency. Set after a successful welcome send.
    # Used so existing community members imported from Circle receive the welcome
    # the FIRST time they register through the landing — but not twice.
    welcome_sent_at = db.Column(db.DateTime, nullable=True)

    # Idempotency flag for email #3 (First Unplug — fires once when count goes 0→1).
    # Without this, a retried GHL webhook would double-send the celebration email.
    first_unplug_sent_at = db.Column(db.DateTime, nullable=True)

    # Idempotency flag for email #4 (Guaranteed Prize — fires once when count hits 5).
    guaranteed_prize_sent_at = db.Column(db.DateTime, nullable=True)

    # Idempotency flags for the 6 cron-driven emails. Each is set after a successful send.
    activation_nudge_sent_at = db.Column(db.DateTime, nullable=True)  # #2
    # Manual admin "almost there" / activation-push send (count 0-4 audience)
    activation_push_sent_at = db.Column(db.DateTime, nullable=True)
    midway_sent_at = db.Column(db.DateTime, nullable=True)            # #5
    final_48h_sent_at = db.Column(db.DateTime, nullable=True)         # #6
    last_6h_sent_at = db.Column(db.DateTime, nullable=True)           # #7
    results_sent_at = db.Column(db.DateTime, nullable=True)           # #8
    you_won_sent_at = db.Column(db.DateTime, nullable=True)           # #9

    # Manual admin sends — fired by the user from /admin/emails when each
    # piece of content drops. Idempotent so accidental double-clicks don't
    # double-send.
    class1_email_sent_at = db.Column(db.DateTime, nullable=True)
    class2_email_sent_at = db.Column(db.DateTime, nullable=True)
    class3_email_sent_at = db.Column(db.DateTime, nullable=True)
    webinar_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    final_signal_sent_at = db.Column(db.DateTime, nullable=True)
    live_imminent_sent_at = db.Column(db.DateTime, nullable=True)
    # "Masterclass save-the-date" invite — fired by admin days before the
    # live, with the Zoom link/date/passcode. Separate from webinar_reminder
    # (1h-before reminder) on purpose: different timing, different copy.
    masterclass_invitation_sent_at = db.Column(db.DateTime, nullable=True)
    # Weekend re-open reminders: one per class. Audience is "first-watched
    # during launch but didn't return during REWATCH_WINDOW_OPENS_AT".
    class1_rewatch_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    class2_rewatch_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    class3_rewatch_reminder_sent_at = db.Column(db.DateTime, nullable=True)

    # Personal outreach tracking — when the founder/team contacted this lead
    # via a 1:1 channel (WhatsApp DM, email, phone call, SMS). Used to:
    #   - hide already-contacted leads from the daily outreach queue
    #   - count "contacted today" KPI
    #   - record what channel worked (so re-engagement uses the right one)
    # Set by /admin/leads/<id>/mark-contacted; cleared by .../unmark-contacted.
    last_outreach_at = db.Column(db.DateTime, nullable=True, index=True)
    last_outreach_channel = db.Column(db.String(20), nullable=True)  # whatsapp|email|call|sms
    last_outreach_notes = db.Column(db.Text, nullable=True)

    # Engagement tracking — bumped on every /dashboard/<code> hit.
    last_dashboard_visit_at = db.Column(db.DateTime, nullable=True)
    dashboard_visit_count = db.Column(db.Integer, default=0)

    # Fraud-detection signals captured at signup. Both fields are NULL for
    # rows created before tracking was wired (legacy, GHL imports).
    signup_ip = db.Column(db.String(64), nullable=True, index=True)
    signup_user_agent = db.Column(db.String(500), nullable=True)

    # Set when ANY signup attributed to this ambassador is queued in
    # PendingReferral (i.e. velocity throttle fired). While this is non-NULL:
    #   - The ambassador is filtered out of public leaderboard / rankings
    #   - All future incoming referrals go to pending (regardless of velocity)
    #   - Admin sees them with a "⏸ UNDER REVIEW" badge
    # Auto-cleared when admin approves or rejects all of their pending items.
    under_review_at = db.Column(db.DateTime, nullable=True, index=True)

    # Cloudflare Turnstile result captured at signup. Status taxonomy:
    # 'valid' | 'invalid' | 'missing' | 'error' | 'not_configured' | None (legacy).
    # turnstile_codes stores Cloudflare's error-codes (comma-separated) on
    # invalid/error rows so we can debug why a verification failed.
    turnstile_status = db.Column(db.String(30), nullable=True, index=True)
    turnstile_codes = db.Column(db.String(160), nullable=True)

    # Phone number (E.164: "+34612345678") + ISO 3166-1 alpha-2 country code
    # (e.g. "ES"). Captured at signup from the Lovable form via GHL, or
    # backfilled from a GHL CSV export. country_code is derived from the
    # phone via libphonenumber so we can chart distribution by country.
    phone_number = db.Column(db.String(30), nullable=True, index=True)
    country_code = db.Column(db.String(4), nullable=True, index=True)

    # GoHighLevel mirror columns. Backfilled by tools/import_ghl_csv.py
    # from a Contacts CSV export, and (later) kept in sync via GHL API.
    # ghl_contact_id is the GHL contact UUID — used to deep-link from
    # the leads dashboard to the GHL contact card. ghl_tags is the raw
    # comma-separated tag string from GHL (e.g. "mkot3_registrado,
    # masterclass march17th"). Both nullable for rows that pre-date the
    # import or never appeared in GHL.
    ghl_contact_id = db.Column(db.String(40), nullable=True, index=True)
    ghl_tags = db.Column(db.Text, nullable=True)

    # Form-question answers from the Lovable signup form, mirrored
    # from GHL custom fields. Stored as raw strings (the form answers
    # are short multi-choice values, e.g. "I'm just getting started
    # with UrbanKiz"). Used for segmentation in /admin/leads.
    dance_level = db.Column(db.String(200), nullable=True, index=True)
    dance_goal = db.Column(db.String(500), nullable=True)
    training_interest = db.Column(db.String(200), nullable=True)
    is_community_member = db.Column(db.String(60), nullable=True)

    # Attribution snapshot at first touch — populated either by GHL signup webhook
    # (when GHL forwards the UTMs as custom data) or backfilled by /api/lead-event
    # the first time the ambassador's email shows up with non-empty UTMs.
    utm_source = db.Column(db.String(100), nullable=True, index=True)
    utm_medium = db.Column(db.String(100), nullable=True)
    utm_campaign = db.Column(db.String(100), nullable=True, index=True)
    utm_content = db.Column(db.String(200), nullable=True)
    utm_term = db.Column(db.String(100), nullable=True)
    fbclid = db.Column(db.String(200), nullable=True)
    gclid = db.Column(db.String(200), nullable=True)
    ttclid = db.Column(db.String(200), nullable=True)

    referrals = db.relationship("Referral", backref="ambassador", lazy=True)
    notifications = db.relationship("MilestoneNotification", backref="ambassador", lazy=True)

    @property
    def referral_count(self):
        return len(self.referrals)

    def current_tier(self, tiers):
        """Return the highest tier this ambassador has reached."""
        earned = [t for t in tiers if self.referral_count >= t.threshold]
        return earned[-1] if earned else None

    def next_tier(self, tiers):
        """Return the next tier to reach."""
        remaining = [t for t in tiers if self.referral_count < t.threshold]
        return remaining[0] if remaining else None


class Referral(db.Model):
    __tablename__ = "referrals"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), unique=True, nullable=False)
    registered_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Fraud-detection signals captured at signup. Used by the admin to flag
    # ambassadors whose referrals share IPs / user agents.
    signup_ip = db.Column(db.String(64), nullable=True, index=True)
    signup_user_agent = db.Column(db.String(500), nullable=True)


class RewardTier(db.Model):
    __tablename__ = "reward_tiers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    channel = db.Column(db.String(20), nullable=False)  # "community" or "public"
    threshold = db.Column(db.Integer, nullable=False)
    reward = db.Column(db.String(300), nullable=False)
    sort_order = db.Column(db.Integer, default=0)


class MilestoneNotification(db.Model):
    __tablename__ = "milestone_notifications"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=False)
    reward_tier_id = db.Column(db.Integer, db.ForeignKey("reward_tiers.id"), nullable=False)
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    delivered = db.Column(db.Boolean, default=False)
    delivered_at = db.Column(db.DateTime, nullable=True)

    reward_tier = db.relationship("RewardTier")


class EmailEvent(db.Model):
    """One row per email lifecycle event. Inserted on send (template_key=...);
    augmented by /api/webhook/resend with 'opened' / 'clicked' / 'bounced' rows
    that match back via resend_email_id.
    """
    __tablename__ = "email_events"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=True, index=True)
    template_key = db.Column(db.String(60), nullable=False, index=True)
    event_type = db.Column(db.String(30), nullable=False, index=True)  # sent | opened | clicked | bounced | complained | delivered
    resend_email_id = db.Column(db.String(120), nullable=True, index=True)
    to_email = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    extra = db.Column(db.Text, nullable=True)  # raw webhook payload (JSON string), for debugging


class PrizeDelivery(db.Model):
    """Physical-prize delivery tracking. One row per (ambassador, slot)
    pair the moment the admin first toggles delivery for that prize.

    slot is one of:
      'guaranteed'  — earned at 5+ unplugs (one per ambassador)
      'top3'        — earned by being in top 3 of their source bucket

    Why a separate table from MilestoneNotification: MilestoneNotification
    is tied to RewardTier rows from the old reward system; the current
    campaign uses a fixed prize structure derived from referral_count +
    source bucket directly. This table records ONLY delivery status
    (the prize itself is computed on the fly from current state).
    """
    __tablename__ = "prize_deliveries"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=False, index=True)
    slot = db.Column(db.String(20), nullable=False, index=True)
    prize_label = db.Column(db.String(200), nullable=False)
    delivered_at = db.Column(db.DateTime, nullable=True)
    delivered_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    ambassador = db.relationship("Ambassador", foreign_keys=[ambassador_id])

    __table_args__ = (
        db.UniqueConstraint("ambassador_id", "slot", name="uq_prize_amb_slot"),
    )


class TurnstileRejection(db.Model):
    """One row per signup rejected by Cloudflare Turnstile in enforce mode.

    Created at the route layer when verify_token() returns 'missing' or
    'invalid' AND TURNSTILE_ENFORCE=1. Used by the admin to show "attacks
    blocked" — a counter that grows as bots try and fail (in contrast to
    Ambassador.turnstile_status, which only tracks signups that DID succeed
    in being created).
    """
    __tablename__ = "turnstile_rejections"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    status = db.Column(db.String(30), nullable=False, index=True)  # missing | invalid
    codes = db.Column(db.String(160), nullable=True)
    email_attempted = db.Column(db.String(200), nullable=True, index=True)
    name_attempted = db.Column(db.String(200), nullable=True)
    ip = db.Column(db.String(64), nullable=True, index=True)
    user_agent = db.Column(db.String(500), nullable=True)
    source = db.Column(db.String(20), nullable=False)  # 'webhook' | 'join'


class LeadEvent(db.Model):
    """One row per behavioral event from a lead — class viewed, video progress,
    resource downloaded, etc. Posted to /api/lead-event from the Lovable class
    pages (and later from Zoom + Circle webhooks).

    Email is the join key: we look up Ambassador by lowercase email and link
    `ambassador_id`. Events from emails not in our DB are still recorded with
    ambassador_id=NULL — these are "ghost leads" who got the link but never
    registered through the GHL signup form.

    The original payload is preserved in `extra` (truncated JSON) so we can
    backfill new fields later without reshipping the schema.
    """
    __tablename__ = "lead_events"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=True, index=True)
    email = db.Column(db.String(200), nullable=True, index=True)

    # Event taxonomy from Lovable's fireClassEvent:
    #   class1_viewed | class1_progress_25/50/75/95 | class1_completed
    #   class1_resource_unlocked | class1_resource_downloaded
    #   class_calendar_open | class_calendar_added
    # Future: webinar_link_clicked, webinar_joined, webinar_left, purchase_completed
    event_type = db.Column(db.String(60), nullable=False, index=True)

    # For progress events (Lovable sends `percent`, `watched_seconds`, `duration_seconds`).
    pct = db.Column(db.Integer, nullable=True)
    current_time_sec = db.Column(db.Integer, nullable=True)
    duration_sec = db.Column(db.Integer, nullable=True)

    # Class number (1, 2, 3) for class-* events. NULL for other event types.
    class_number = db.Column(db.Integer, nullable=True, index=True)

    # Page that triggered the event (e.g. https://inevitable.metakizzproject.com/class1)
    page_url = db.Column(db.String(500), nullable=True)

    # Attribution at time of event (snapshot from URL params + localStorage).
    # Useful when an ambassador returns via a different campaign than the one
    # they originally signed up with.
    utm_source = db.Column(db.String(100), nullable=True, index=True)
    utm_medium = db.Column(db.String(100), nullable=True)
    utm_campaign = db.Column(db.String(100), nullable=True, index=True)
    utm_content = db.Column(db.String(200), nullable=True)
    utm_term = db.Column(db.String(100), nullable=True)
    ref = db.Column(db.String(50), nullable=True, index=True)
    fbclid = db.Column(db.String(200), nullable=True)
    gclid = db.Column(db.String(200), nullable=True)
    ttclid = db.Column(db.String(200), nullable=True)

    # Webinar attendance fields (populated by /admin/zoom/import-participants).
    # Only relevant for event_type='webinar_joined' rows; NULL otherwise.
    # webinar_duration_min sums multiple sessions if the participant rejoined.
    webinar_duration_min = db.Column(db.Integer, nullable=True, index=True)
    webinar_country = db.Column(db.String(80), nullable=True, index=True)
    webinar_device = db.Column(db.String(40), nullable=True)
    webinar_joined_at = db.Column(db.DateTime, nullable=True)
    webinar_left_at = db.Column(db.DateTime, nullable=True)
    # Display name from Zoom — important for guest joiners who don't have
    # an email captured. Used as the only identifier in that case, and as
    # the matching key against Ambassador.name for indirect linking.
    webinar_name = db.Column(db.String(120), nullable=True)

    # Raw JSON payload for debugging / future fields. Truncated to 5KB.
    extra = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class LeadNote(db.Model):
    """Manual admin notes + contact log per lead. Lets the admin record outreach
    ("sent WhatsApp on May 5", "called and got voicemail") and free-form notes
    that complement the automatic temperature score.

    type taxonomy:
      'note'             — free-form text
      'whatsapp_sent'    — admin marked: I messaged them on WhatsApp
      'email_sent'       — admin marked: I sent them a personal email
      'call'             — phone outreach
    """
    __tablename__ = "lead_notes"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=False, index=True)
    type = db.Column(db.String(30), nullable=False, default="note")
    content = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class PendingReferral(db.Model):
    """A signup attribution (Referral) waiting for admin approval.

    Created when a referrer hits the velocity threshold (e.g. 5 new referrals
    within 30 minutes). The Ambassador row is still created normally for the
    new signup; only the Referral row is held for review. The referrer's
    public referral_count does NOT increment until the admin approves.

    On approve → a real Referral row is created (and the referrer's count
    goes up). On reject → the row stays in this table with status='rejected'
    for audit, and the new Ambassador can optionally be deleted manually.
    """
    __tablename__ = "pending_referrals"

    id = db.Column(db.Integer, primary_key=True)
    referrer_ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=True, index=True)
    new_ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=True)
    referrer_code = db.Column(db.String(20), nullable=True)  # captured snapshot
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    received_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    flagged_reason = db.Column(db.String(160), nullable=False)  # human-readable
    signup_ip = db.Column(db.String(64), nullable=True)
    signup_user_agent = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), default="pending", index=True)  # pending | approved | rejected
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_notes = db.Column(db.Text, nullable=True)

    referrer = db.relationship("Ambassador", foreign_keys=[referrer_ambassador_id])
    new_ambassador = db.relationship("Ambassador", foreign_keys=[new_ambassador_id])


class Reservation(db.Model):
    """A €100 deposit reservation for MKOT 3.0, captured during a live session.

    Two-phase lifecycle:
      1. Stripe webhook (checkout.session.completed) inserts the row with
         stripe_session_id + paid_at + email (from Stripe customer_details).
      2. The buyer is redirected to /reservation/form where they fill in
         name/surname/program/modality/clarity. The same row is updated
         and form_completed_at is set.

    Raffle eligibility = paid_at IS NOT NULL AND form_completed_at IS NOT NULL
    AND form_completed_at < raffle_state.window_closed_at (or window still open).
    """
    __tablename__ = "reservations"

    id = db.Column(db.Integer, primary_key=True)

    # Stripe identifiers — session_id is unique per checkout, used for idempotency.
    stripe_session_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    stripe_payment_intent_id = db.Column(db.String(120), nullable=True)
    amount_cents = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(3), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True, index=True)

    # Customer fields. Email comes from Stripe; the rest from the form.
    email = db.Column(db.String(200), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=True)
    surname = db.Column(db.String(200), nullable=True)

    # Form choices — persisted as short strings, not enums, for SQLite friendliness.
    # All choice fields accept 'not_sure' so the buyer can leave the decision open
    # for the call. None of these are binding — they're orientative for prep.
    program_choice = db.Column(db.String(20), nullable=True)   # 'dancers' | 'instructors' | 'not_sure'
    modality_choice = db.Column(db.String(20), nullable=True)  # 'solo' | 'duo' | 'not_sure'
    payment_plan = db.Column(db.String(20), nullable=True)     # 'one_payment' | 'six_installments' | 'not_sure'
    clarity = db.Column(db.String(20), nullable=True)          # 'clear' | 'doubts'
    notes = db.Column(db.Text, nullable=True)

    # Match by email at webhook time. Nullable — buyer may not be in the ambassador list.
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"), nullable=True, index=True)

    form_completed_at = db.Column(db.DateTime, nullable=True, index=True)
    confirmation_email_sent_at = db.Column(db.DateTime, nullable=True)

    # Admin follow-up state — turns /admin/reservations into a CRM hub.
    # last_contacted_at is the source of truth for "have we reached out
    # to this buyer yet?" The channel is what was used (wa/email/call).
    # admin_notes is free-text scratchpad maintained by the admin.
    last_contacted_at = db.Column(db.DateTime, nullable=True, index=True)
    last_contacted_channel = db.Column(db.String(20), nullable=True)
    admin_notes = db.Column(db.Text, nullable=True)

    # Auto-refund state. When the buyer pays the full plan in the Circle
    # Stripe account, our /api/webhook/stripe-circle endpoint refunds the
    # €100 deposit on this reservation. Idempotency guard: refunded_at
    # being set means we already issued the refund — never double-refund.
    # circle_payment_id records which Circle-side charge triggered the
    # refund so we can trace it back. refund_status: success | failed | dry_run.
    refunded_at = db.Column(db.DateTime, nullable=True, index=True)
    refund_id = db.Column(db.String(120), nullable=True)
    refund_amount_cents = db.Column(db.Integer, nullable=True)
    refund_status = db.Column(db.String(20), nullable=True)
    refund_attempted_at = db.Column(db.DateTime, nullable=True)
    refund_error = db.Column(db.Text, nullable=True)
    refund_email_sent_at = db.Column(db.DateTime, nullable=True)
    circle_payment_id = db.Column(db.String(120), nullable=True, index=True)

    # "I tried to reach you on WhatsApp but couldn't" outreach. Sent when
    # the buyer paid the deposit but we have no phone on file (no
    # Ambassador match, or Ambassador with empty phone_number). Email
    # gives them WhatsApp/SMS/email CTAs to reach back.
    no_phone_email_sent_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    ambassador = db.relationship("Ambassador", foreign_keys=[ambassador_id])


class PartnerInvite(db.Model):
    """One row per Couple-plan partner invite submitted via /invite-partner.

    Lifecycle:
      1. Buyer submits the public form. We INSERT a row immediately so we
         have a record even if Circle / Resend later fail.
      2. We call Circle's V2 Admin API to create the partner (or add them
         to the access group if they already exist) and stamp circle_status.
      3. We send the partner welcome + buyer confirmation via Resend and
         stamp the *_email_sent_at columns. If a Resend send fails after
         a successful Circle add, needs_followup is set so the admin page
         can flag the row for manual outreach.
    """
    __tablename__ = "partner_invites"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    buyer_name = db.Column(db.String(120), nullable=False)
    buyer_email = db.Column(db.String(200), nullable=False, index=True)
    partner_name = db.Column(db.String(120), nullable=False)
    partner_email = db.Column(db.String(200), nullable=False, index=True)
    location = db.Column(db.String(200), nullable=True)
    personal_note = db.Column(db.String(220), nullable=True)

    # "created" | "added_to_group" | "buyer_missing" | "buyer_no_group" | "failed"
    circle_status = db.Column(db.String(20), nullable=True, index=True)
    circle_response = db.Column(db.Text, nullable=True)
    # "dancers" | "instructors" | None — mirrors the buyer's access group.
    target_group = db.Column(db.String(20), nullable=True, index=True)

    partner_email_sent_at = db.Column(db.DateTime, nullable=True)
    buyer_email_sent_at = db.Column(db.DateTime, nullable=True)
    admin_alert_sent_at = db.Column(db.DateTime, nullable=True)

    # True when Circle add succeeded but partner welcome email failed.
    needs_followup = db.Column(db.Boolean, default=False, nullable=False, index=True)


class BuddyPost(db.Model):
    """A published "looking for training partner" profile from an Ambassador.

    One post per ambassador (unique FK). Auth = the ambassador's
    `dashboard_code` (same pattern as the existing /dashboard route).
    Filters intentionally biased toward separating committed dancers
    from weekend casuals — see "festivals_per_year" + "commitment".
    """
    __tablename__ = "buddy_posts"

    id = db.Column(db.Integer, primary_key=True)
    ambassador_id = db.Column(db.Integer, db.ForeignKey("ambassadors.id"),
                              nullable=False, unique=True, index=True)

    # Geo
    city = db.Column(db.String(120), nullable=False)
    country_code = db.Column(db.String(4), nullable=True, index=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    # Profile
    role = db.Column(db.String(20), nullable=False)  # lead | follower | ambi
    looking_for_partner = db.Column(db.Boolean, default=False, nullable=False)
    looking_to_train = db.Column(db.Boolean, default=False, nullable=False)
    looking_to_socialize = db.Column(db.Boolean, default=False, nullable=False)
    looking_for_mkot_buddy = db.Column(db.Boolean, default=False, nullable=False)

    # Matching filters — solve the "weekend dancer vs committed" mismatch.
    festivals_per_year = db.Column(db.String(20), nullable=True, index=True)
    dance_level = db.Column(db.String(20), nullable=True, index=True)
    years_dancing = db.Column(db.String(20), nullable=True, index=True)
    commitment = db.Column(db.String(20), nullable=True, index=True)
    goal = db.Column(db.String(30), nullable=True, index=True)
    availability = db.Column(db.String(60), nullable=True)

    # Viral attribution
    invited_by_dashboard_code = db.Column(db.String(20), nullable=True, index=True)

    message = db.Column(db.String(300), nullable=True)
    contact_email_override = db.Column(db.String(200), nullable=True)

    # Lifecycle
    published_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                             nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    hidden = db.Column(db.Boolean, default=False, nullable=False, index=True)
    renewal_reminder_sent_at = db.Column(db.DateTime, nullable=True)
    contact_count = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    ambassador = db.relationship("Ambassador", foreign_keys=[ambassador_id])


class BuddyContact(db.Model):
    """One row per "send message" attempt on a BuddyPost. Used for the
    daily quota (max 3 sent / 3 received per email) and for traceability
    of who's hitting the system.
    """
    __tablename__ = "buddy_contacts"

    id = db.Column(db.Integer, primary_key=True)
    target_post_id = db.Column(db.Integer, db.ForeignKey("buddy_posts.id"),
                               nullable=False, index=True)
    sender_email = db.Column(db.String(200), nullable=False, index=True)
    sender_name = db.Column(db.String(120), nullable=True)
    message = db.Column(db.String(1000), nullable=False)
    sender_ip = db.Column(db.String(64), nullable=True)
    relay_email_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           nullable=False, index=True)

    target_post = db.relationship("BuddyPost", foreign_keys=[target_post_id])


class CirclePayment(db.Model):
    """One row per payment received in the Circle Stripe account.

    Captured by /api/webhook/stripe-circle on `checkout.session.completed`
    or `charge.succeeded`. Email is the join key against Reservation
    (deposit) and Ambassador (Circle member). Idempotent on
    stripe_charge_id — repeated webhooks for the same charge are no-ops.
    """
    __tablename__ = "circle_payments"

    id = db.Column(db.Integer, primary_key=True)
    stripe_charge_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    stripe_payment_intent_id = db.Column(db.String(120), nullable=True)
    email = db.Column(db.String(200), nullable=False, index=True)
    customer_name = db.Column(db.String(200), nullable=True)
    amount_cents = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(3), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True, index=True)
    description = db.Column(db.String(500), nullable=True)
    raw_event_type = db.Column(db.String(60), nullable=True)
    # Invoice tracking — populated once the invoice system ships.
    invoice_id = db.Column(db.String(120), nullable=True, index=True)
    invoice_sent_at = db.Column(db.DateTime, nullable=True)
    invoice_pdf_url = db.Column(db.String(500), nullable=True)
    # Immutable copy of the PDF as generated at send time. Stored in DB
    # because Render's filesystem is ephemeral. Postgres BYTEA / SQLite BLOB.
    invoice_pdf_bytes = db.Column(db.LargeBinary, nullable=True)

    # Optional explicit link to an Ambassador, set by the admin from the
    # reservations dashboard when an orphan payment matches a known launch
    # profile but the email on Stripe doesn't equal the Ambassador's email
    # (so the auto-match by email failed). Manually-curated identity.
    ambassador_id = db.Column(
        db.Integer, db.ForeignKey("ambassadors.id"),
        nullable=True, index=True,
    )
    ambassador = db.relationship("Ambassador", foreign_keys=[ambassador_id])

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class RaffleState(db.Model):
    """Singleton row (id=1) holding the current raffle window state.

    A separate table from Reservation avoids mass updates when the admin
    clicks "Close window". window_closed_at = NULL means the window is open
    and any newly-completed reservation is eligible.
    """
    __tablename__ = "raffle_state"

    id = db.Column(db.Integer, primary_key=True)
    window_closed_at = db.Column(db.DateTime, nullable=True)
    winner_reservation_id = db.Column(db.Integer, db.ForeignKey("reservations.id"), nullable=True)
    closed_by_admin = db.Column(db.String(80), nullable=True)
    spun_at = db.Column(db.DateTime, nullable=True)

    winner = db.relationship("Reservation", foreign_keys=[winner_reservation_id])
