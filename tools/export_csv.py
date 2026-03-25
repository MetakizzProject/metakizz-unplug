"""
Export ambassador and referral data to CSV files.

Usage:
    python tools/export_csv.py [--channel community|public|all] [--output-dir .tmp/]

Creates two files:
    - ambassadors_{channel}.csv
    - referrals_{channel}.csv
"""

import sys
import os
import csv
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app import create_app
from app.models import db, Ambassador, Referral


def export(channel="all", output_dir=".tmp"):
    app = create_app()
    os.makedirs(output_dir, exist_ok=True)

    with app.app_context():
        # Export ambassadors
        if channel == "all":
            ambassadors = Ambassador.query.all()
        else:
            ambassadors = Ambassador.query.filter_by(source=channel).all()

        ambassadors = sorted(ambassadors, key=lambda a: a.referral_count, reverse=True)

        amb_file = os.path.join(output_dir, f"ambassadors_{channel}.csv")
        with open(amb_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Rank", "Name", "Email", "Source", "Referral Code", "Referrals", "Instagram", "IG Shared", "Joined"])
            for i, amb in enumerate(ambassadors, 1):
                writer.writerow([
                    i,
                    amb.name,
                    amb.email,
                    amb.source,
                    amb.referral_code,
                    amb.referral_count,
                    amb.instagram_handle or "",
                    "Yes" if amb.shared_on_instagram else "No",
                    amb.created_at.strftime("%Y-%m-%d"),
                ])
        print(f"Exported {len(ambassadors)} ambassadors to {amb_file}")

        # Export referrals
        query = (
            db.session.query(Referral, Ambassador)
            .join(Ambassador, Referral.ambassador_id == Ambassador.id)
        )
        if channel != "all":
            query = query.filter(Ambassador.source == channel)
        referrals = query.order_by(Referral.registered_at.desc()).all()

        ref_file = os.path.join(output_dir, f"referrals_{channel}.csv")
        with open(ref_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Name", "Email", "Referred By", "Ambassador Email", "Channel", "Date"])
            for ref, amb in referrals:
                writer.writerow([
                    ref.name,
                    ref.email,
                    amb.name,
                    amb.email,
                    amb.source,
                    ref.registered_at.strftime("%Y-%m-%d %H:%M"),
                ])
        print(f"Exported {len(referrals)} referrals to {ref_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export MetaKizz data to CSV")
    parser.add_argument("--channel", default="all", choices=["community", "public", "all"])
    parser.add_argument("--output-dir", default=".tmp")
    args = parser.parse_args()

    export(channel=args.channel, output_dir=args.output_dir)
