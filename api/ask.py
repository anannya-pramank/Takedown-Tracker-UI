"""
Vercel serverless function: free-flow chat over the SELECTED incidents.

Replaces the keyword router with a Gemini-backed endpoint. The chat is scoped to
whatever the frontend has filtered to (the `filter` object in the request body).
Reliability is preserved by computing counts/breakdowns in SQL (exact) and handing
those to the model as authoritative aggregates; the model uses the row sample only
for specifics. It never counts rows itself.

Request  (unchanged, plus optional history):
    { "question": "...", "filter": {who,platform,provision,sector}, "history": [{role,text}] }
Response (unchanged):
    { "answer": "..." }

Env vars (Vercel -> Settings -> Environment Variables):
    SUPABASE_DB_PW   (required, as before)
    GEMINI_API_KEY   (required)
    GEMINI_MODEL     (optional; default gemini-2.5-flash)
    ALLOW_ORIGIN     (optional; default "*"; set to your frontend origin before publishing)
    DEBUG            (optional; if set, error responses include the real cause)
"""

import os
import json
import datetime
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler

import psycopg2

# --- config ------------------------------------------------------------------
HOST = "aws-1-ap-south-1.pooler.supabase.com"
PORT = 6543
DBNAME = "postgres"
USER = "postgres.mehsswdrmprtfookaqig"

ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")
DEBUG = bool(os.environ.get("DEBUG"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_ROWS = 400          # rows passed to the model; aggregates always cover the full selection
DESC_TRUNC = 400        # per-incident description cap (chars)
MAX_HISTORY = 6         # prior turns kept for follow-ups

SYSTEM_PROMPT = (
    "You are the assistant for a public Section 69A content-takedown transparency "
    "tracker for India. The user is viewing a FILTERED SELECTION of incidents; "
    "answer only about that selection.\n"
    "You are given three things: the active FILTER; an AGGREGATES block with counts "
    "computed in SQL over the ENTIRE selection (these are exact and authoritative - "
    "use them for any number or percentage, never recount the rows yourself); and an "
    "INCIDENTS sample for specific or qualitative questions. If the selection is larger "
    "than the rows shown, counts are still exact but row-level detail is limited to the "
    "sample, and you should say so.\n"
    "Answer anything asked about these incidents - patterns, specific entities, dates, "
    "platforms, sectors, legal-basis gaps, status, who issued actions. If the data does "
    "not support an answer, say so plainly; do not invent numbers, entities, or legal "
    "conclusions. Call out when a legal basis is 'Unknown' or a location is unidentified, "
    "because that disclosure gap is what this tracker exists to document. Be concise. "
    "Do not give legal advice."
)


# --- db -----------------------------------------------------------------------
def get_conn():
    pw = os.environ.get("SUPABASE_DB_PW")
    if not pw:
        raise RuntimeError(
            "SUPABASE_DB_PW is not set on this deployment. Add it in Vercel "
            "(Settings -> Environment Variables), then redeploy."
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


def _group_counts(cur, col, where, params):
    """Exact GROUP BY count over the selection. `col` is from a fixed whitelist."""
    extra = "AND" if where else "WHERE"
    cur.execute(
        f'SELECT {col}, count(*) FROM public."incidents" {where} '
        f'{extra} {col} IS NOT NULL GROUP BY {col} ORDER BY count(*) DESC;',
        params,
    )
    return {(k if k is not None else "Unknown"): int(n) for k, n in cur.fetchall()}


def gather_context(question, filt):
    """Pull exact aggregates + a capped row sample for the selection."""
    where, params = build_where(filt)
    extra = "AND" if where else "WHERE"

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT count(*) FROM public."incidents" {where};', params)
        total = int(cur.fetchone()[0])

        if total == 0:
            return {"total": 0}

        cur.execute(
            f'''SELECT count(*) FROM public."incidents" {where}
                {extra} legal_basis <> 'Unknown';''', params)
        known_basis = int(cur.fetchone()[0])

        cur.execute(
            f'''SELECT count(*) FROM public."incidents" {where}
                {extra} status = 'Challenged';''', params)
        challenged = int(cur.fetchone()[0])

        cur.execute(
            f'''SELECT min(incident_date), max(incident_date)
                FROM public."incidents" {where};''', params)
        dmin, dmax = cur.fetchone()

        aggregates = {
            "total_selected": total,
            "identified_legal_basis": known_basis,
            "unknown_legal_basis": total - known_basis,
            "challenged_in_court": challenged,
            "date_range": [
                dmin.isoformat() if isinstance(dmin, datetime.date) else None,
                dmax.isoformat() if isinstance(dmax, datetime.date) else None,
            ],
            "by_platform": _group_counts(cur, "platform", where, params),
            "by_status": _group_counts(cur, "status", where, params),
            "by_legal_basis": _group_counts(cur, "legal_basis", where, params),
            "by_sector": _group_counts(cur, "sector", where, params),
            "by_entity_type": _group_counts(cur, "entity_type", where, params),
        }

        # global litigation context (legal_cases is not filtered by these facets)
        cur.execute('''SELECT court, count(*) FROM public."legal_cases"
                       WHERE court IS NOT NULL GROUP BY court
                       ORDER BY count(*) DESC LIMIT 5;''')
        aggregates["litigation_by_court"] = {c: int(n) for c, n in cur.fetchall()}

        # row sample for specifics
        cur.execute(
            f'''SELECT incident_date, entity_name, entity_type, platform,
                       legal_basis, status, sector, action_taken, description
                FROM public."incidents" {where}
                ORDER BY incident_date DESC NULLS LAST LIMIT %s;''',
            params + [MAX_ROWS])
        rows = []
        for (d, ent, etype, plat, basis, status, sector, action, desc) in cur.fetchall():
            rows.append({
                "date": d.isoformat() if isinstance(d, datetime.date) else None,
                "entity": ent,
                "who": etype,
                "platform": plat,
                "provision": basis,
                "status": status,
                "sector": sector,
                "action": action,
                "note": (desc or "")[:DESC_TRUNC],
            })

        return {
            "total": total,
            "aggregates": aggregates,
            "rows_shown": len(rows),
            "rows_are_complete": total <= MAX_ROWS,
            "incidents": rows,
        }
    finally:
        cur.close()
        conn.close()


# --- gemini -------------------------------------------------------------------
def ask_gemini(question, filt, ctx, history):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set on this deployment.")

    active_filter = {k: v for k, v in (filt or {}).items() if v} or "none (all incidents)"
    data_block = (
        f"ACTIVE FILTER: {json.dumps(active_filter)}\n\n"
        f"AGGREGATES (exact, whole selection):\n{json.dumps(ctx['aggregates'], default=str)}\n\n"
        f"INCIDENTS ({ctx['rows_shown']} shown"
        f"{'' if ctx['rows_are_complete'] else ', SAMPLE ONLY of a larger selection'}):\n"
        f"{json.dumps(ctx['incidents'], default=str)}"
    )

    contents = []
    for turn in (history or [])[-MAX_HISTORY:]:
        role = "model" if turn.get("role") in ("assistant", "model") else "user"
        text = (turn.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({
        "role": "user",
        "parts": [{"text": f"{data_block}\n\nQUESTION: {question}"}],
    })

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1000,
            # 2.5-flash: 0 = lowest latency. Raise (e.g. 512) if you want the model
            # to reason harder on multi-step questions; watch total latency.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    req = urllib.request.Request(
        GEMINI_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        reason = (data.get("candidates") or [{}])[0].get("finishReason", "unknown")
        raise RuntimeError(f"Empty Gemini response (finishReason={reason})")


def answer(question, filt, history):
    question = (question or "").strip()
    if not question:
        return "Ask me anything about the currently selected incidents."

    ctx = gather_context(question, filt)
    if ctx.get("total") == 0:
        return "No documented incidents match that combination yet."
    return ask_gemini(question, filt, ctx, history)


# --- http ---------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
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
            return self._json(400, {"answer": "Invalid request body."})

        question = body.get("question", "")
        if len(question) > 2000:
            return self._json(400, {"answer": "That question is too long."})

        try:
            ans = answer(question, body.get("filter", {}) or {}, body.get("history", []))
            self._json(200, {"answer": ans})
        except Exception as e:
            # DEBUG env var surfaces the real cause; otherwise users see a clean
            # message. No more swallowing errors into a fake 200.
            if DEBUG:
                self._json(500, {"answer": f"Backend error (debug): {type(e).__name__}: {e}"})
            else:
                self._json(500, {"answer": "The assistant is temporarily unavailable. "
                                           "Please try again."})
