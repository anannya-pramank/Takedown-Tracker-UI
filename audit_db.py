"""
Supabase audit — one run, full map of what's in the database and what's usable.

For every table in the public schema it reports:
  - row count
  - each column: type, null/not-null, and FILL RATE (how many rows have a value)
  - for low-cardinality text columns: the distinct values and their counts

Reads SUPABASE_DB_PW from env. Prints a report and also writes audit.json.

Fill rate is the key "usable?" signal: a column that's 0% filled is dead weight;
a facet you want to expose as a pill needs to be substantially populated.
"""

import os
import sys
import json
import psycopg2

HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

# Columns with more distinct values than this are treated as free-text/ids,
# so we don't dump their value lists.
MAX_DISTINCT = 25

password = os.environ.get("SUPABASE_DB_PW")
if not password:
    sys.exit("SUPABASE_DB_PW is not set.")


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def main():
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname=DBNAME, user=USER, password=password,
        connect_timeout=20, sslmode="require",
    )
    cur = conn.cursor()

    tables = [r[0] for r in q(cur, """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' ORDER BY table_name;
    """)]

    report = {}

    for t in tables:
        # row count
        total = q(cur, f'SELECT count(*) FROM public."{t}";')[0][0]

        cols = q(cur, """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name=%s
            ORDER BY ordinal_position;
        """, (t,))

        col_report = []
        for name, dtype, nullable in cols:
            entry = {"column": name, "type": dtype, "nullable": nullable == "YES"}

            if total > 0:
                # fill rate
                filled = q(cur, f'SELECT count("{name}") FROM public."{t}";')[0][0]
                entry["filled"] = filled
                entry["fill_pct"] = round(100.0 * filled / total, 1)

                # distinct values for low-cardinality text-ish columns
                if dtype in ("text", "character varying", "boolean") and filled > 0:
                    nd = q(cur, f'SELECT count(DISTINCT "{name}") FROM public."{t}";')[0][0]
                    entry["distinct"] = nd
                    if nd <= MAX_DISTINCT:
                        vals = q(cur, f'''
                            SELECT "{name}", count(*) FROM public."{t}"
                            WHERE "{name}" IS NOT NULL
                            GROUP BY "{name}" ORDER BY count(*) DESC;
                        ''')
                        entry["values"] = [{"value": str(v), "count": c} for v, c in vals]
            else:
                entry["filled"] = 0
                entry["fill_pct"] = 0.0

            col_report.append(entry)

        report[t] = {"rows": total, "columns": col_report}

    # ---- print a human-readable report ----
    print("\n" + "=" * 72)
    print("SUPABASE AUDIT")
    print("=" * 72)

    empty, populated = [], []
    for t, info in report.items():
        (populated if info["rows"] > 0 else empty).append(t)

    print(f"\nTables with data ({len(populated)}): {', '.join(populated) or 'none'}")
    print(f"Empty tables ({len(empty)}): {', '.join(empty) or 'none'}")

    for t, info in report.items():
        print("\n" + "-" * 72)
        print(f"{t}  —  {info['rows']} rows")
        print("-" * 72)
        if info["rows"] == 0:
            print("  (empty — schema only)")
            continue
        for c in info["columns"]:
            line = f"  {c['column']:<28} {c['type']:<18} {c.get('fill_pct',0):>5}% filled"
            if "distinct" in c:
                line += f"  ({c['distinct']} distinct)"
            print(line)
            if "values" in c:
                shown = ", ".join(f"{v['value']}({v['count']})" for v in c["values"][:12])
                print(f"        \u2192 {shown}")

    with open("audit.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("\nWrote audit.json\n")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
