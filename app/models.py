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

    # Idempotency flag for email #4 (Guaranteed Prize — fires once when count hits 5).
    guaranteed_prize_sent_at = db.Column(db.DateTime, nullable=True)

    # Idempotency flags for the 6 cron-driven emails. Each is set after a successful send.
    activation_nudge_sent_at = db.Column(db.DateTime, nullable=True)  # #2
    midway_sent_at = db.Column(db.DateTime, nullable=True)            # #5
    final_48h_sent_at = db.Column(db.DateTime, nullable=True)         # #6
    last_6h_sent_at = db.Column(db.DateTime, nullable=True)           # #7
    results_sent_at = db.Column(db.DateTime, nullable=True)           # #8
    you_won_sent_at = db.Column(db.DateTime, nullable=True)           # #9

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
