import os
import logging
import secrets
from flask import Flask
from dotenv import load_dotenv
from sqlalchemy import text, inspect

load_dotenv()

logger = logging.getLogger(__name__)


def _ensure_unsubscribe_columns(db):
    """
    Idempotent migration: add new ambassadors columns if missing, backfill where
    relevant. Works on both SQLite (dev) and Postgres (prod).

    Columns managed:
    - unsubscribe_token (VARCHAR 64): random secret for opt-out links, backfilled.
    - unsubscribed_at (TIMESTAMP nullable): set when user opts out.
    - welcome_sent_at (TIMESTAMP nullable): idempotency for the welcome email so
      community members imported pre-launch receive the welcome only once.
    """
    engine = db.engine
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("ambassadors")}

    with engine.begin() as conn:
        if "unsubscribe_token" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN unsubscribe_token VARCHAR(64)"))
            logger.info("added column ambassadors.unsubscribe_token")
        if "unsubscribed_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN unsubscribed_at TIMESTAMP"))
            logger.info("added column ambassadors.unsubscribed_at")
        if "welcome_sent_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN welcome_sent_at TIMESTAMP"))
            logger.info("added column ambassadors.welcome_sent_at")
        if "guaranteed_prize_sent_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN guaranteed_prize_sent_at TIMESTAMP"))
            logger.info("added column ambassadors.guaranteed_prize_sent_at")
        if "first_unplug_sent_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN first_unplug_sent_at TIMESTAMP"))
            logger.info("added column ambassadors.first_unplug_sent_at")
        # Cron-driven email idempotency flags + manual class/webinar sends.
        for col in (
            "activation_nudge_sent_at",
            "activation_push_sent_at",
            "midway_sent_at",
            "final_48h_sent_at",
            "last_6h_sent_at",
            "results_sent_at",
            "you_won_sent_at",
            "class1_email_sent_at",
            "class2_email_sent_at",
            "class3_email_sent_at",
            "webinar_reminder_sent_at",
            "final_signal_sent_at",
            "live_imminent_sent_at",
            "class1_rewatch_reminder_sent_at",
            "class2_rewatch_reminder_sent_at",
            "class3_rewatch_reminder_sent_at",
            "last_outreach_at",
        ):
            if col not in cols:
                conn.execute(text(f"ALTER TABLE ambassadors ADD COLUMN {col} TIMESTAMP"))
                logger.info("added column ambassadors.%s", col)
        # Outreach tracking columns with non-timestamp types (channel = short
        # string, notes = free-form text). Same pattern as the GHL block below.
        for col_name, col_type in [
            ("last_outreach_channel", "VARCHAR(20)"),
            ("last_outreach_notes",   "TEXT"),
        ]:
            if col_name not in cols:
                conn.execute(text(f"ALTER TABLE ambassadors ADD COLUMN {col_name} {col_type}"))
                logger.info("added column ambassadors.%s", col_name)
        # Engagement tracking columns (added later).
        if "last_dashboard_visit_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN last_dashboard_visit_at TIMESTAMP"))
            logger.info("added column ambassadors.last_dashboard_visit_at")
        if "dashboard_visit_count" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN dashboard_visit_count INTEGER DEFAULT 0"))
            logger.info("added column ambassadors.dashboard_visit_count")
        # Fraud-detection columns (IP / UA at signup time).
        if "signup_ip" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN signup_ip VARCHAR(64)"))
            logger.info("added column ambassadors.signup_ip")
        if "signup_user_agent" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN signup_user_agent VARCHAR(500)"))
            logger.info("added column ambassadors.signup_user_agent")
        if "under_review_at" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN under_review_at TIMESTAMP"))
            logger.info("added column ambassadors.under_review_at")
        if "turnstile_status" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN turnstile_status VARCHAR(30)"))
            logger.info("added column ambassadors.turnstile_status")
        if "turnstile_codes" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN turnstile_codes VARCHAR(160)"))
            logger.info("added column ambassadors.turnstile_codes")
        if "phone_number" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN phone_number VARCHAR(30)"))
            logger.info("added column ambassadors.phone_number")
        if "country_code" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN country_code VARCHAR(4)"))
            logger.info("added column ambassadors.country_code")

        # GHL mirror columns (added 2026-05-04 for the Leads dashboard).
        if "ghl_contact_id" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN ghl_contact_id VARCHAR(40)"))
            logger.info("added column ambassadors.ghl_contact_id")
        if "ghl_tags" not in cols:
            conn.execute(text("ALTER TABLE ambassadors ADD COLUMN ghl_tags TEXT"))
            logger.info("added column ambassadors.ghl_tags")
        # Form-question answers mirrored from GHL custom fields.
        for col_name, col_type in [
            ("dance_level",         "VARCHAR(200)"),
            ("dance_goal",          "VARCHAR(500)"),
            ("training_interest",   "VARCHAR(200)"),
            ("is_community_member", "VARCHAR(60)"),
        ]:
            if col_name not in cols:
                conn.execute(text(f"ALTER TABLE ambassadors ADD COLUMN {col_name} {col_type}"))
                logger.info("added column ambassadors.%s", col_name)

        # Attribution / UTM columns (added 2026-05-04 for the Leads tracker).
        # Populated either by the GHL signup webhook (when GHL custom-data
        # forwards the UTMs) OR backfilled by /api/lead-event the first time
        # the lead's email shows up with non-empty UTM params.
        for col_name, col_type in [
            ("utm_source",   "VARCHAR(100)"),
            ("utm_medium",   "VARCHAR(100)"),
            ("utm_campaign", "VARCHAR(100)"),
            ("utm_content",  "VARCHAR(200)"),
            ("utm_term",     "VARCHAR(100)"),
            ("fbclid",       "VARCHAR(200)"),
            ("gclid",        "VARCHAR(200)"),
            ("ttclid",       "VARCHAR(200)"),
        ]:
            if col_name not in cols:
                conn.execute(text(f"ALTER TABLE ambassadors ADD COLUMN {col_name} {col_type}"))
                logger.info("added column ambassadors.%s", col_name)

        # Same fraud columns on referrals (the more actionable signal — duplicates here
        # mean the same person is registering many "friends" via their own link).
        ref_cols = {c["name"] for c in inspector.get_columns("referrals")}
        if "signup_ip" not in ref_cols:
            conn.execute(text("ALTER TABLE referrals ADD COLUMN signup_ip VARCHAR(64)"))
            logger.info("added column referrals.signup_ip")
        if "signup_user_agent" not in ref_cols:
            conn.execute(text("ALTER TABLE referrals ADD COLUMN signup_user_agent VARCHAR(500)"))
            logger.info("added column referrals.signup_user_agent")

        # MKOT 3.0 reservations table — add payment_plan if missing, plus the
        # admin follow-up columns that turn /admin/reservations into a CRM hub.
        if "reservations" in inspector.get_table_names():
            res_cols = {c["name"] for c in inspector.get_columns("reservations")}
            if "payment_plan" not in res_cols:
                conn.execute(text("ALTER TABLE reservations ADD COLUMN payment_plan VARCHAR(20)"))
                logger.info("added column reservations.payment_plan")
            for col_name, col_type in [
                ("last_contacted_at",      "TIMESTAMP"),
                ("last_contacted_channel", "VARCHAR(20)"),
                ("admin_notes",            "TEXT"),
                # Auto-refund state (added 2026-05-10).
                ("refunded_at",            "TIMESTAMP"),
                ("refund_id",              "VARCHAR(120)"),
                ("refund_amount_cents",    "INTEGER"),
                ("refund_status",          "VARCHAR(20)"),
                ("refund_attempted_at",    "TIMESTAMP"),
                ("refund_error",           "TEXT"),
                ("refund_email_sent_at",   "TIMESTAMP"),
                ("circle_payment_id",      "VARCHAR(120)"),
            ]:
                if col_name not in res_cols:
                    conn.execute(text(f"ALTER TABLE reservations ADD COLUMN {col_name} {col_type}"))
                    logger.info("added column reservations.%s", col_name)

        # Partner-invite target_group column (added 2026-05-09). Mirrors the
        # buyer's access group so we know whether a couple was put on the
        # Dancers or Instructors track.
        if "partner_invites" in inspector.get_table_names():
            pi_cols = {c["name"] for c in inspector.get_columns("partner_invites")}
            if "target_group" not in pi_cols:
                conn.execute(text("ALTER TABLE partner_invites ADD COLUMN target_group VARCHAR(20)"))
                logger.info("added column partner_invites.target_group")

        # CirclePayment.invoice_pdf_bytes (added 2026-05-10). Stores the
        # rendered PDF immutably so the admin can re-download what the
        # customer received, not a regenerated version.
        if "circle_payments" in inspector.get_table_names():
            cp_cols = {c["name"] for c in inspector.get_columns("circle_payments")}
            if "invoice_pdf_bytes" not in cp_cols:
                # Postgres uses BYTEA; SQLite uses BLOB. Use LargeBinary
                # syntax that both engines accept via the column type alias.
                if engine.dialect.name == "postgresql":
                    conn.execute(text("ALTER TABLE circle_payments ADD COLUMN invoice_pdf_bytes BYTEA"))
                else:
                    conn.execute(text("ALTER TABLE circle_payments ADD COLUMN invoice_pdf_bytes BLOB"))
                logger.info("added column circle_payments.invoice_pdf_bytes")

        # Webinar attendance enrichment columns on lead_events. Populated by
        # /admin/zoom/import-participants — sums duration across rejoins,
        # captures country / device / first-join / last-leave from the Zoom
        # Reports API. Only meaningful for event_type='webinar_joined' rows.
        if "lead_events" in inspector.get_table_names():
            le_cols = {c["name"] for c in inspector.get_columns("lead_events")}
            for col_name, col_type in [
                ("webinar_duration_min", "INTEGER"),
                ("webinar_country",      "VARCHAR(80)"),
                ("webinar_device",       "VARCHAR(40)"),
                ("webinar_joined_at",    "TIMESTAMP"),
                ("webinar_left_at",      "TIMESTAMP"),
                ("webinar_name",         "VARCHAR(120)"),
            ]:
                if col_name not in le_cols:
                    conn.execute(text(f"ALTER TABLE lead_events ADD COLUMN {col_name} {col_type}"))
                    logger.info("added column lead_events.%s", col_name)

        # Backfill tokens for any rows that don't have one yet (existing ambassadors).
        rows = conn.execute(text("SELECT id FROM ambassadors WHERE unsubscribe_token IS NULL")).fetchall()
        for row in rows:
            token = secrets.token_urlsafe(24)
            conn.execute(
                text("UPDATE ambassadors SET unsubscribe_token = :tok WHERE id = :id"),
                {"tok": token, "id": row[0]},
            )
        if rows:
            logger.info("backfilled unsubscribe_token for %d existing ambassadors", len(rows))


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metakizz.db"
        ),
    )
    # Render's Postgres uses "postgres://" but SQLAlchemy requires "postgresql://"
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config["SQLALCHEMY_DATABASE_URI"].replace(
            "postgres://", "postgresql://", 1
        )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["APP_URL"] = os.getenv("APP_URL", "http://localhost:5000")
    app.config["LANDING_URL"] = os.getenv("LANDING_URL", "http://localhost:5000")
    app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "admin")
    app.config["GHL_WEBHOOK_SECRET"] = os.getenv("GHL_WEBHOOK_SECRET", "")
    app.config["WHATSAPP_GROUP_URL"] = os.getenv("WHATSAPP_GROUP_URL", "")
    app.config["CRON_SECRET"] = os.getenv("CRON_SECRET", "")
    # Partner Invite flow (MKOT 3.0 Couple plan). The flow mirrors the
    # buyer's access group, so we need both Dancers and Instructors IDs.
    app.config["CIRCLE_ACCESS_GROUP_DANCERS_ID"] = os.getenv("CIRCLE_ACCESS_GROUP_DANCERS_ID", "")
    app.config["CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID"] = os.getenv("CIRCLE_ACCESS_GROUP_INSTRUCTORS_ID", "")
    app.config["ADMIN_NOTIFICATION_EMAIL"] = os.getenv("ADMIN_NOTIFICATION_EMAIL", "")
    # Hard campaign close: 2026-05-07 19:00 Europe/Madrid. Used by cron logic.
    app.config["CAMPAIGN_CLOSE_ISO"] = os.getenv("CAMPAIGN_CLOSE_ISO", "2026-05-07T19:00:00+02:00")
    # Weekend re-open of all 3 classes. Anything in lead_events.created_at
    # at-or-after this timestamp counts as a "rewatch", anything before is
    # a "first view". Used by /admin/class-views and the rewatch-reminder
    # segment computation. Default: Friday 2026-05-09 00:00 Madrid.
    app.config["REWATCH_WINDOW_OPENS_AT"] = os.getenv("REWATCH_WINDOW_OPENS_AT", "2026-05-09T00:00:00+02:00")
    # Per-class overrides for the rewatch cutoff. Class 3 (live-replay)
    # was uploaded AFTER the live (May 7+); the "rewatch" semantics for
    # class 3 may need a different cutoff than 1+2. Default to the
    # global REWATCH_WINDOW_OPENS_AT if not overridden per class.
    for _cn in (1, 2, 3):
        key = f"REWATCH_WINDOW_OPENS_AT_CLASS{_cn}"
        app.config[key] = os.getenv(key, app.config["REWATCH_WINDOW_OPENS_AT"])
    del _cn

    from app.models import db

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_unsubscribe_columns(db)
        _seed_raffle_state(db)

    from app.routes.home import home_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.leaderboard import leaderboard_bp
    from app.routes.admin import admin_bp
    from app.routes.webhook import webhook_bp
    from app.routes.cron import cron_bp
    from app.routes.reservation import reservation_bp
    from app.routes.stripe_webhook import stripe_bp
    from app.routes.partner_invite import partner_invite_bp
    from app.routes.stripe_circle_webhook import stripe_circle_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(leaderboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(cron_bp)
    app.register_blueprint(reservation_bp)
    app.register_blueprint(stripe_bp)
    app.register_blueprint(partner_invite_bp)
    app.register_blueprint(stripe_circle_bp)

    return app


def _seed_raffle_state(db):
    """Ensure the singleton RaffleState row exists. The /admin/raffle page
    creates it on demand, but seeding here keeps the JSON state endpoint
    valid from the very first request."""
    from app.models import RaffleState
    if RaffleState.query.get(1) is None:
        db.session.add(RaffleState(id=1))
        db.session.commit()
        logger.info("seeded raffle_state row id=1")


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
