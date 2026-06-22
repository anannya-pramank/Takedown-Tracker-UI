"""
Inspect the tables that matter for the snapshot: columns + a few sample rows.
Reads the password from SUPABASE_DB_PW (GitHub Actions secret or local env var).
"""

import os
import sys
import json
import psycopg2

HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

TABLES = [
    "notices",
    "notice_targets",
    "notice_stats_by_authority",
    "notice_stats_by_content_type",
    "notice_stats_by_ground",
    "notice_stats_by_platform",
]

password = os.environ.get("SUPABASE_DB_PW")
if not password:
    sys.exit("SUPABASE_DB_PW is not set.")


def serialize(v):
    # make jsonb / arrays / dates printable
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)[:300]
    return str(v)[:300]


def main():
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname=DBNAME, user=USER, password=password,
        connect_timeout=15, sslmode="require",
    )
    cur = conn.cursor()
    print("Connected.\n")

    for t in TABLES:
        print("=" * 70)
        print(f"TABLE: {t}")
        print("=" * 70)

        # columns
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position;
        """, (t,))
        cols = cur.fetchall()
        if not cols:
            print("  (table not found)\n")
            continue
        print("Columns:")
        colnames = []
        for name, dtype, nullable in cols:
            colnames.append(name)
            flag = "NULL" if nullable == "YES" else "NOT NULL"
            print(f"    {name:<28} {dtype:<22} {flag}")

        # row count
        cur.execute(f'SELECT count(*) FROM public."{t}";')
        total = cur.fetchone()[0]
        print(f"  row count: {total}")

        # sample rows
        cur.execute(f'SELECT * FROM public."{t}" LIMIT 3;')
        rows = cur.fetchall()
        print(f"\n  Sample rows (up to 3):")
        for r in rows:
            print("    " + " | ".join(f"{c}={serialize(v)}" for c, v in zip(colnames, r)))
        print()

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
