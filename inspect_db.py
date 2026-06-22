"""
Connection test + schema inspection for the (third-party) Supabase database.

Reads the password from the SUPABASE_DB_PW environment variable.
Never hardcode the password here. The non-secret connection details
(host, port, user, db) are safe to keep in the repo.

Run locally:   export SUPABASE_DB_PW='...'; python inspect_db.py
Run in CI:     password comes from the GitHub Actions secret of the same name.
"""

import os
import sys
import psycopg2

# Non-secret connection details (from the Supabase connection panel).
HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

password = os.environ.get("SUPABASE_DB_PW")
if not password:
    sys.exit("SUPABASE_DB_PW is not set. Export it locally or set it as a GitHub Actions secret.")


def main():
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname=DBNAME, user=USER, password=password,
        connect_timeout=15,
        sslmode="require",
    )
    cur = conn.cursor()

    print("Connected.\n")

    # 1. List tables in the public schema.
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name;
    """)
    tables = [r[0] for r in cur.fetchall()]
    print("Tables in 'public':")
    for t in tables:
        print("  -", t)
    print()

    # 2. For each table, show columns + types + nullability.
    for t in tables:
        cur.execute("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position;
        """, (t,))
        cols = cur.fetchall()
        print(f"Schema of '{t}':")
        for name, dtype, nullable in cols:
            null_flag = "NULL" if nullable == "YES" else "NOT NULL"
            print(f"    {name:<28} {dtype:<22} {null_flag}")

        # row count, to gauge coverage
        cur.execute(f'SELECT count(*) FROM public."{t}";')
        print(f"    row count: {cur.fetchone()[0]}\n")

    cur.close()
    conn.close()
    print("Done. No credentials were written anywhere.")


if __name__ == "__main__":
    main()
