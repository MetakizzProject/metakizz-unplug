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

    from app.models import db

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _ensure_unsubscribe_columns(db)

    from app.routes.home import home_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.leaderboard import leaderboard_bp
    from app.routes.admin import admin_bp
    from app.routes.webhook import webhook_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(leaderboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(webhook_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
