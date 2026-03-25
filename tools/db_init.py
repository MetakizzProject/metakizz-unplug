"""
Initialize the MetaKizz Ambassador Challenge database.
Creates all tables and optionally seeds sample reward tiers.

Usage:
    python tools/db_init.py [--seed]

Options:
    --seed    Add sample reward tiers for both channels
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app import create_app
from app.models import db, RewardTier


def init_db(seed=False):
    app = create_app()

    with app.app_context():
        db.create_all()
        print("Database tables created successfully.")

        if seed:
            # Only seed if no tiers exist yet
            if RewardTier.query.count() == 0:
                sample_tiers = [
                    # Community challenge tiers
                    RewardTier(name="Starter", channel="community", threshold=5, reward="1 free month in the community", sort_order=1),
                    RewardTier(name="Ambassador", channel="community", threshold=10, reward="Ambassador status inside the community", sort_order=2),
                    RewardTier(name="Champion", channel="community", threshold=20, reward="Another free month", sort_order=3),
                    # Public challenge tiers
                    RewardTier(name="Starter", channel="public", threshold=5, reward="Free access to the masterclass", sort_order=1),
                    RewardTier(name="Rising Star", channel="public", threshold=10, reward="1 free month in the community", sort_order=2),
                    RewardTier(name="Legend", channel="public", threshold=20, reward="3 months free in the community", sort_order=3),
                ]
                db.session.add_all(sample_tiers)
                db.session.commit()
                print(f"Seeded {len(sample_tiers)} sample reward tiers.")
            else:
                print("Reward tiers already exist, skipping seed.")


if __name__ == "__main__":
    seed = "--seed" in sys.argv
    init_db(seed=seed)
