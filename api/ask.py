"""
Vercel serverless function: Level 1 chat endpoint (DEBUG build).

Same logic as before, but failures return the real error text + a 500 status
so you can see *why* in curl / the Network tab. Revert the do_POST except-block
to the generic message before anything public-facing.
"""

import os
import json
import datetime
from http.server import BaseHTTPRequestHandler

import psycopg2

HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")


def get_conn():
    pw = os.environ.get("SUPABASE_DB_PW")
    if not pw:
        raise RuntimeError(
            "SUPABASE_DB_PW is not set on this deployment. Add it in Vercel "
            "(Settings -> Environment Variables) for the right environment, "
            "then redeploy so a fresh build picks it up."
        )
    return psycopg2.connect(
        host=HOST, port=PORT, dbname=DBNAME, user=USER,
        password=pw, connect_timeout=15, sslmode="require",
    )


def build_where(filt):
    clauses, params = [], []
    mapping = {"who": "entity_type", "platform": "platform",
               "provision": "legal_basis", "sector": "sector"}
    for key, col in mapping.items():
        val = filt.get(key)
        if val:
            clauses.append(f"{col} = %s")
            params.append(val)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def answer(question, filt):
    q = (question or "").lower()
    where, params = build_where(filt)
    extra = "AND" if where else "WHERE"

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(f'SELECT count(*) FROM public."incidents" {where};', params)
    total = cur.fetchone()[0]

    if total == 0:
        cur.close(); conn.close()
        return "No documented incidents match that combination yet."

    if any(w in q for w in ("challenge", "court", "litigat", "case")):
        cur.execute(f'''SELECT count(*) FROM public."incidents" {where}
                        {extra} status = 'Challenged';''', params)
        challenged = cur.fetchone()[0]
        cur.execute('''SELECT court, count(*) FROM public."legal_cases"
                       WHERE court IS NOT NULL GROUP BY court
                       ORDER BY count(*) DESC LIMIT 3;''')
        courts = cur.fetchall()
        cur.close(); conn.close()
        court_str = ", ".join(f"{c} ({n})" for c, n in courts)
        return (f"{challenged} of the {total} matching incidents are recorded as challenged. "
                f"Across the wider case record, challenges concentrate in: {court_str}.")

    if any(w in q for w in ("recent", "latest", "last")):
        cur.execute(f'''SELECT incident_date, entity_name, platform, legal_basis, status
                        FROM public."incidents" {where}
                        ORDER BY incident_date DESC NULLS LAST LIMIT 1;''', params)
        r = cur.fetchone()
        cur.close(); conn.close()
        if not r:
            return "No dated incident found for that selection."
        d, ent, plat, basis, status = r
        d = d.isoformat() if isinstance(d, datetime.date) else str(d)
        return (f"Most recent matching incident: {ent or 'unnamed'} on {plat or 'unknown platform'}, "
                f"{d}. Legal basis: {basis or 'Unknown'}. Status: {status or 'Unknown'}.")

    if any(w in q for w in ("basis", "known", "provision", "disclosed", "transparen", "why")):
        cur.execute(f'''SELECT count(*) FROM public."incidents" {where}
                        {extra} legal_basis <> 'Unknown';''', params)
        known = cur.fetchone()[0]
        cur.close(); conn.close()
        pct = round(100 * known / total) if total else 0
        return (f"Of the {total} matching incidents, {known} ({pct}%) have an identified legal basis. "
                f"The rest are documented from public reporting but the legal authority for the "
                f"action was never made public.")

    if "platform" in q:
        cur.execute(f'''SELECT platform, count(*) FROM public."incidents" {where}
                        {extra} platform IS NOT NULL
                        GROUP BY platform ORDER BY count(*) DESC LIMIT 5;''', params)
        rows = cur.fetchall()
        cur.close(); conn.close()
        spread = ", ".join(f"{p} ({n})" for p, n in rows)
        return f"Across the {total} matching incidents, platforms break down as: {spread}."

    if any(w in q for w in ("status", "restored", "active")):
        cur.execute(f'''SELECT status, count(*) FROM public."incidents" {where}
                        {extra} status IS NOT NULL
                        GROUP BY status ORDER BY count(*) DESC;''', params)
        rows = cur.fetchall()
        cur.close(); conn.close()
        spread = ", ".join(f"{s} ({n})" for s, n in rows)
        return f"Status of the {total} matching incidents: {spread}."

    cur.close(); conn.close()
    return (f"{total} documented incidents match your selection. Ask about how many were "
            f"challenged in court, the most recent one, how many have a known legal basis, "
            f"or the platform breakdown.")


class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        try:
            ans = answer(body.get("question", ""), body.get("filter", {}) or {})
            self._json(200, {"answer": ans})
        except Exception as e:
            # DEBUG: surfaces the real cause. Swap back to the generic
            # "unavailable" message before publishing.
            self._json(500, {"answer": "Backend error (debug): "
                             + type(e).__name__ + ": " + str(e)})
