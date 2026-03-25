"""
Check all ambassadors for newly reached milestones and send notification emails.
Run this after new referrals come in, or on a schedule.

Usage:
    python tools/check_milestones.py [--dry-run]

Options:
    --dry-run    Show what would be sent without actually sending emails
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.app import create_app
from app.models import db, Ambassador, RewardTier, MilestoneNotification
from tools.send_email import send_milestone_email


def check_milestones(dry_run=False):
    app = create_app()
    app_url = app.config["APP_URL"]

    with app.app_context():
        ambassadors = Ambassador.query.all()
        new_milestones = 0

        for amb in ambassadors:
            tiers = (
                RewardTier.query
                .filter_by(channel=amb.source)
                .order_by(RewardTier.sort_order)
                .all()
            )

            for tier in tiers:
                if amb.referral_count >= tier.threshold:
                    already = MilestoneNotification.query.filter_by(
                        ambassador_id=amb.id,
                        reward_tier_id=tier.id,
                    ).first()

                    if not already:
                        dashboard_url = f"{app_url}/dashboard/{amb.dashboard_code}"

                        if dry_run:
                            print(f"[DRY RUN] Would notify {amb.name} ({amb.email}): "
                                  f"reached '{tier.name}' ({amb.referral_count}/{tier.threshold}) -> {tier.reward}")
                        else:
                            success = send_milestone_email(
                                ambassador_name=amb.name,
                                ambassador_email=amb.email,
                                tier_name=tier.name,
                                reward=tier.reward,
                                dashboard_url=dashboard_url,
                                referral_count=amb.referral_count,
                            )

                            notification = MilestoneNotification(
                                ambassador_id=amb.id,
                                reward_tier_id=tier.id,
                            )
                            db.session.add(notification)
                            db.session.commit()

                            status = "sent" if success else "FAILED"
                            print(f"[{status}] {amb.name}: {tier.name} -> {tier.reward}")

                        new_milestones += 1

        if new_milestones == 0:
            print("No new milestones to notify.")
        else:
            print(f"\nTotal new milestones: {new_milestones}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    check_milestones(dry_run=dry_run)
