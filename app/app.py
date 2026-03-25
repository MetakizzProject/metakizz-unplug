import os
from flask import Flask
from dotenv import load_dotenv

load_dotenv()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metakizz.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["APP_URL"] = os.getenv("APP_URL", "http://localhost:5000")
    app.config["ADMIN_PASSWORD"] = os.getenv("ADMIN_PASSWORD", "admin")

    from app.models import db

    db.init_app(app)

    with app.app_context():
        db.create_all()

    from app.routes.home import home_bp
    from app.routes.register import register_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.leaderboard import leaderboard_bp
    from app.routes.admin import admin_bp

    app.register_blueprint(home_bp)
    app.register_blueprint(register_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(leaderboard_bp)
    app.register_blueprint(admin_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
