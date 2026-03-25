"""Migrate data from local SQLite to Render PostgreSQL."""

import sqlite3
import psycopg2
import os

# Paths
SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metakizz.db")
POSTGRES_URL = os.getenv("DATABASE_URL", "")

def migrate():
    if not POSTGRES_URL:
        print("ERROR: Set DATABASE_URL environment variable to your Render External Database URL")
        return

    # Fix postgres:// → postgresql:// for psycopg2
    pg_url = POSTGRES_URL.replace("postgres://", "postgresql://", 1) if POSTGRES_URL.startswith("postgres://") else POSTGRES_URL

    print(f"Reading from SQLite: {SQLITE_PATH}")
    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    print(f"Connecting to PostgreSQL...")
    pg_conn = psycopg2.connect(pg_url)
    pg_cur = pg_conn.cursor()

    # Migrate ambassadors
    rows = sqlite_conn.execute("SELECT * FROM ambassadors").fetchall()
    print(f"Migrating {len(rows)} ambassadors...")
    for r in rows:
        pg_cur.execute(
            """INSERT INTO ambassadors (id, name, email, referral_code, dashboard_code, source,
               instagram_handle, profile_picture_url, circle_member_id,
               shared_on_instagram, instagram_proof_url, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["name"], r["email"], r["referral_code"], r["dashboard_code"],
             r["source"], r["instagram_handle"], r["profile_picture_url"],
             r["circle_member_id"], bool(r["shared_on_instagram"]), r["instagram_proof_url"],
             r["created_at"])
        )

    # Migrate reward tiers
    rows = sqlite_conn.execute("SELECT * FROM reward_tiers").fetchall()
    print(f"Migrating {len(rows)} reward tiers...")
    for r in rows:
        pg_cur.execute(
            """INSERT INTO reward_tiers (id, name, channel, threshold, reward, sort_order)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["name"], r["channel"], r["threshold"], r["reward"], r["sort_order"])
        )

    # Migrate referrals
    rows = sqlite_conn.execute("SELECT * FROM referrals").fetchall()
    print(f"Migrating {len(rows)} referrals...")
    for r in rows:
        pg_cur.execute(
            """INSERT INTO referrals (id, ambassador_id, name, email, registered_at)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["ambassador_id"], r["name"], r["email"], r["registered_at"])
        )

    # Migrate milestone notifications
    rows = sqlite_conn.execute("SELECT * FROM milestone_notifications").fetchall()
    print(f"Migrating {len(rows)} milestone notifications...")
    for r in rows:
        pg_cur.execute(
            """INSERT INTO milestone_notifications (id, ambassador_id, reward_tier_id, sent_at)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["ambassador_id"], r["reward_tier_id"], r["sent_at"])
        )

    # Reset PostgreSQL sequences to avoid ID conflicts with future inserts
    for table in ["ambassadors", "referrals", "reward_tiers", "milestone_notifications"]:
        pg_cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1)) FROM {table}")

    pg_conn.commit()
    pg_cur.close()
    pg_conn.close()
    sqlite_conn.close()

    print("Migration complete!")


if __name__ == "__main__":
    migrate()
