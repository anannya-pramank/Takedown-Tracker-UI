"""
Build snapshot.json the front-end reads, from the REAL populated schema.

The working takedown corpus lives in `incidents` (not `notices`, which is empty).
Litigation lives in `legal_cases` + the *_statistics tables. Headline numbers
are pre-computed in `stats_cache`.

Reads SUPABASE_DB_PW from env. Writes snapshot.json.

Field mapping (front-end -> real columns on `incidents`):
  who       -> entity_type     (Individual / Media Outlet / Technology / Journalist / Platform-wide)
  platform  -> platform        (Twitter/X / YouTube / Facebook / ...)
  provision -> legal_basis     (Section 69A / 79(3)(b) / Sahyog Portal / IT Rules / Unknown)
  sector    -> sector
  status    -> status          (Active / Restored / Challenged / Unknown)

Notes on coverage (from the audit):
  - legal_basis is "Unknown" for ~70% of incidents. That is surfaced, not hidden.
  - stated_reason / content_category are empty, so there is NO reason facet.
  - There is no disclosed flag. The transparency cut used here is
    "legal basis identified vs Unknown", which is the real story in this data.
"""

import os
import sys
import json
import datetime
import psycopg2

HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

password = os.environ.get("SUPABASE_DB_PW")
if not password:
    sys.exit("SUPABASE_DB_PW is not set.")


def q(cur, sql, args=None):
    cur.execute(sql, args or ())
    return cur.fetchall()


def distinct_counts(cur, col):
    return q(cur, f'''
        SELECT {col}, count(*) FROM public."incidents"
        WHERE {col} IS NOT NULL
        GROUP BY {col} ORDER BY count(*) DESC;
    ''')


def main():
    conn = psycopg2.connect(
        host=HOST, port=PORT, dbname=DBNAME, user=USER, password=password,
        connect_timeout=20, sslmode="require",
    )
    cur = conn.cursor()

    # ---- facets: only fields that are actually populated ----
    facets = {
        "who":      [{"id": v, "label": v} for v, _ in distinct_counts(cur, "entity_type")],
        "platform": [{"id": v, "label": v} for v, _ in distinct_counts(cur, "platform")],
        "provision":[{"id": v, "label": v} for v, _ in distinct_counts(cur, "legal_basis")],
        "sector":   [{"id": v, "label": v} for v, _ in distinct_counts(cur, "sector")],
    }

    # ---- authorities / grid groups: use legal_basis as the grouping axis ----
    # (incidents has no issuing-authority column; legal_basis is the meaningful
    #  categorical for "under what". Court split comes from legal_cases below.)
    auth_rows = distinct_counts(cur, "legal_basis")
    authorities = [{"id": a, "label": a} for a, _ in auth_rows]

    # ---- matrix: provision (legal_basis) x platform -> documented / known-basis ----
    # "documented" = all matching incidents
    # "identified" = incidents whose legal_basis is not 'Unknown'
    matrix_rows = q(cur, """
        SELECT legal_basis, platform,
               count(*) AS documented,
               count(*) FILTER (WHERE legal_basis <> 'Unknown') AS identified
        FROM public."incidents"
        WHERE legal_basis IS NOT NULL AND platform IS NOT NULL
        GROUP BY legal_basis, platform;
    """)
    matrix = {}
    for legal_basis, platform, documented, identified in matrix_rows:
        matrix.setdefault(legal_basis, {})[platform] = {
            "documented": int(documented),
            "disclosed": int(identified),  # keep key name the front-end expects
        }

    # ---- headline numbers from stats_cache ----
    cache = dict(q(cur, 'SELECT key, value FROM public."stats_cache";'))
    total_documented = q(cur, 'SELECT count(*) FROM public."incidents";')[0][0]
    total_identified = q(cur, '''SELECT count(*) FROM public."incidents"
                                 WHERE legal_basis <> 'Unknown';''')[0][0]

    # ---- litigation summary from legal_cases + court_statistics ----
    court_rows = q(cur, """
        SELECT court, count(*) FROM public."legal_cases"
        WHERE court IS NOT NULL GROUP BY court ORDER BY count(*) DESC;
    """)
    litigation = {
        "total_cases": q(cur, 'SELECT count(*) FROM public."legal_cases";')[0][0],
        "by_court": [{"court": c, "count": n} for c, n in court_rows],
        "active_challenges": cache.get("active_legal_challenges"),
    }

    # ---- browsable incident records ----
    inc_rows = q(cur, """
        SELECT id, incident_date, entity_name, entity_type, platform,
               legal_basis, status, sector, action_taken, source_urls
        FROM public."incidents"
        ORDER BY incident_date DESC NULLS LAST
        LIMIT 80;
    """)
    orders = []
    for (iid, idate, ename, etype, platform, basis, status, sector, action, urls) in inc_rows:
        orders.append({
            "id": str(iid),
            "date": idate.isoformat() if idate else None,
            "entity": ename,
            "who": etype,
            "platform": platform,
            "provision": basis,
            "status": status,
            "sector": sector,
            "action": action,
            "disclosed": basis is not None and basis != "Unknown",
            "url": (urls[0] if urls else ""),
        })

    snapshot = {
        "meta": {
            "last_updated": datetime.date.today().isoformat(),
            "note": "Generated from live incident data.",
            "total_documented": int(total_documented),
            "total_disclosed": int(total_identified),
            "orders_complete": total_documented <= len(orders),
            "labels": {
                "documented": "documented",
                "disclosed": "legal basis identified"
            }
        },
        "facets": facets,
        "authorities": authorities,
        "matrix": matrix,
        "litigation": litigation,
        "orders": orders,
    }

    with open("snapshot.json", "w") as f:
        json.dump(snapshot, f, indent=2, default=str)

    print(f"Wrote snapshot.json: {total_documented} incidents "
          f"({total_identified} with identified legal basis), "
          f"{len(facets['platform'])} platforms, {litigation['total_cases']} legal cases.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
