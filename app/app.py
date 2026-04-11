import os
from flask import Flask
from dotenv import load_dotenv

load_dotenv()


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
