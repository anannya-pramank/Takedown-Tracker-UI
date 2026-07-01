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
import re
import math
import hashlib
import datetime
import psycopg2
from collections import Counter

# --- geolocation --------------------------------------------------------------
# Resolve each incident to a city AND record how that resolution was made, so a
# genuine central-authority placement (MeitY/MIB/DoT/...) is never conflated with
# "we couldn't tell". The issuer is stored in the description as "Issued by: X"
# (SFLC rows); state police / High Courts resolve to their seat.
#
# Resolution order (first hit wins); the loc_source it returns is emitted per
# incident so the frontend can style/segment them:
#   1. issuer matches a STATE authority  -> that seat        [issuer_state]
#   2. issuer matches a CENTRAL body     -> New Delhi         [central]
#   3. description text mentions a state -> that seat         [text_scan]
#   4. description text mentions central -> New Delhi         [central_text]
#   5. nothing found                     -> unplaced (None)   [unknown]
#
# Flip UNKNOWN_TO_DEFAULT to restore the old "everything unknown -> Delhi" behaviour.

UNKNOWN_TO_DEFAULT = False
JITTER_RADIUS = 0.6          # degrees (~1 grid cell); tune for tighter/looser clusters

CITY_COORDS = {
    "New Delhi": (77.21, 28.61), "Mumbai": (72.88, 19.08),
    "Chennai": (80.27, 13.08), "Kolkata": (88.36, 22.57),
    "Bengaluru": (77.59, 12.97), "Hyderabad": (78.47, 17.38),
    "Ahmedabad": (72.57, 23.03), "Jaipur": (75.79, 26.91),
    "Patna": (85.14, 25.59), "Bhopal": (77.41, 23.26),
    "Thiruvananthapuram": (76.95, 8.52), "Agra": (78.01, 27.18),
    "Gurugram": (77.03, 28.46), "Shillong": (91.88, 25.57),
    "Chandigarh": (76.78, 30.73), "Vijayawada": (80.65, 16.51),
    # --- added coverage ---
    "Lucknow": (80.95, 26.85), "Prayagraj": (81.85, 25.44),
    "Bhubaneswar": (85.82, 20.30), "Guwahati": (91.75, 26.14),
    "Ranchi": (85.31, 23.34), "Raipur": (81.63, 21.25),
    "Dehradun": (78.03, 30.32), "Shimla": (77.17, 31.10),
    "Srinagar": (74.80, 34.08), "Panaji": (73.83, 15.50),
    "Imphal": (93.94, 24.82), "Agartala": (91.28, 23.83),
    "Gangtok": (88.61, 27.33),
}

# Ordered, most specific first. NOTE: the bare "delhi" catch MUST stay last —
# the text-scan stage deliberately skips it (see _match_city) so incidental
# mentions of Delhi in a description don't re-inflate the capital.
AUTHORITY_CITY = [
    (r"kerala", "Thiruvananthapuram"),
    (r"\bagra\b", "Agra"),
    (r"tamil\s*nadu|madras|chennai", "Chennai"),
    (r"telangana", "Hyderabad"),
    (r"maharashtra|bombay", "Mumbai"),
    (r"gurugram|gurgaon", "Gurugram"),
    (r"haryana", "Chandigarh"),
    (r"\bbihar", "Patna"),
    (r"rajasthan", "Jaipur"),
    (r"meghalaya", "Shillong"),
    (r"\bpunjab", "Chandigarh"),
    (r"madhya\s*pradesh", "Bhopal"),
    (r"andhra\s*pradesh", "Vijayawada"),
    (r"karnataka", "Bengaluru"),
    (r"gujarat", "Ahmedabad"),
    (r"calcutta|west\s*bengal|kolkata", "Kolkata"),
    (r"allahabad|prayagraj", "Prayagraj"),
    (r"uttar\s*pradesh|lucknow", "Lucknow"),
    (r"odisha|orissa|cuttack|bhubaneswar", "Bhubaneswar"),
    (r"assam|gauhati|guwahati", "Guwahati"),
    (r"jharkhand|ranchi", "Ranchi"),
    (r"chhattisgarh|bilaspur|raipur", "Raipur"),
    (r"uttarakhand|nainital|dehradun", "Dehradun"),
    (r"himachal|shimla", "Shimla"),
    (r"jammu|kashmir|srinagar|j\s*&\s*k|j\s*and\s*k", "Srinagar"),
    (r"\bgoa\b|panaji", "Panaji"),
    (r"manipur|imphal", "Imphal"),
    (r"tripura|agartala", "Agartala"),
    (r"sikkim|gangtok", "Gangtok"),
    (r"delhi", "New Delhi"),  # Delhi HC / Delhi Police — KEEP LAST
]
DEFAULT_CITY = "New Delhi"

# Central / union authorities. Kept separate so a genuine MeitY/MIB order is
# labelled "central" rather than masquerading as an unknown-defaulted guess.
CENTRAL_ISSUER = re.compile(
    r"meity|ministry of electronics|ministry of information|broadcasting|"
    r"\bmib\b|\bdot\b|department of telecom|telecommunications|"
    r"central\s*gov|union of india|\bgoi\b|\bnixi\b|sahyog|"
    r"cert-?in|\bi4c\b|home affairs|\bmha\b",
    re.I,
)


def _issuer_from_description(description):
    m = re.search(r"Issued by:?\s*([^;)\n]+)", description or "")
    return m.group(1).strip() if m else ""


def _match_city(text, skip_generic_delhi=False):
    """Return (city, True) on a state/HC authority match, else (None, False).
    skip_generic_delhi drops the trailing bare-'delhi' rule (text-scan use)."""
    if not text:
        return None, False
    patterns = AUTHORITY_CITY[:-1] if skip_generic_delhi else AUTHORITY_CITY
    for pat, city in patterns:
        if re.search(pat, text, re.I):
            return city, True
    return None, False


def _is_central(text):
    return bool(text and CENTRAL_ISSUER.search(text))


def resolve_location(description):
    """Return (city_or_None, loc_source). city None => unplaced."""
    issuer = _issuer_from_description(description)

    city, ok = _match_city(issuer)
    if ok:
        return city, "issuer_state"
    if _is_central(issuer):
        return DEFAULT_CITY, "central"

    # issuer absent or unrecognised -> scan the whole record text
    city, ok = _match_city(description, skip_generic_delhi=True)
    if ok:
        return city, "text_scan"
    if _is_central(description):
        return DEFAULT_CITY, "central_text"

    if UNKNOWN_TO_DEFAULT:
        return DEFAULT_CITY, "default"
    return None, "unknown"


def _jitter(lon, lat, seed):
    h = hashlib.md5(str(seed).encode()).hexdigest()
    a = int(h[:8], 16) / 0xFFFFFFFF          # angle fraction
    b = int(h[8:16], 16) / 0xFFFFFFFF        # radius fraction
    ang = a * 2 * math.pi
    rad = JITTER_RADIUS * math.sqrt(b)       # sqrt -> uniform over the disc
    return round(lon + rad * math.cos(ang), 3), round(lat + rad * math.sin(ang), 3)


def incident_lonlat(description, seed):
    """Return (lon, lat, loc_source). lon/lat are None when unplaced."""
    city, source = resolve_location(description)
    if city is None:
        return None, None, source
    lon, lat = CITY_COORDS.get(city, CITY_COORDS[DEFAULT_CITY])
    lon, lat = _jitter(lon, lat, seed)
    return lon, lat, source
# -----------------------------------------------------------------------------

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

    # ---- browsable incident records (all of them) ----
    inc_rows = q(cur, """
        SELECT id, incident_date, entity_name, entity_type, platform,
               legal_basis, status, sector, action_taken, source_urls, description
        FROM public."incidents"
        ORDER BY incident_date DESC NULLS LAST;
    """)
    orders = []
    for (iid, idate, ename, etype, platform, basis, status, sector,
         action, urls, description) in inc_rows:
        lon, lat, loc_source = incident_lonlat(description, iid)
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
            "lon": lon,
            "lat": lat,
            "loc_source": loc_source,  # issuer_state | central | text_scan | central_text | unknown
        })

    # ---- location-resolution breakdown (diagnostic + surfaced in meta) ----
    loc_counts = Counter(o["loc_source"] for o in orders)
    placed = sum(1 for o in orders if o["lon"] is not None)
    unplaced = len(orders) - placed

    snapshot = {
        "meta": {
            "last_updated": datetime.date.today().isoformat(),
            "note": "Generated from live incident data.",
            "total_documented": int(total_documented),
            "total_disclosed": int(total_identified),
            "orders_complete": total_documented <= len(orders),
            "located": placed,
            "unplaced": unplaced,
            "location_breakdown": dict(loc_counts),
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
    print(f"Location: {placed} placed, {unplaced} unplaced. Breakdown: "
          + ", ".join(f"{k}={v}" for k, v in loc_counts.most_common()))

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
