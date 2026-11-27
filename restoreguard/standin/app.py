"""STAND-IN for Mohamed's Nordvik Handel ERP: replaced by his app when it is ready.

RestoreGuard has to be tested end to end before the real app exists, so this
follows handoff/PROMPT-FOR-MOHAMED.md exactly and nothing more: the same tables
and business rules, numbered migrations applied at startup, the seeded admin
login, /health, /api/dashboard, /api/login, /api/orders, /api/invoices/{id}.
An app built for schema N refuses a database whose schema is newer, the way a
real deploy would, which is what makes version drift visible to RestoreGuard.
"""
import os
from pathlib import Path

import bcrypt
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

HERE = Path(__file__).resolve().parent
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
DSN = os.environ.get("DATABASE_URL", "postgresql://nordvik@db/nordvik")
MIGRATIONS = sorted((HERE / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
SUPPORTED = int(MIGRATIONS[-1].name[:3])
TABLES = ["users", "customers", "products", "orders", "order_lines", "invoices", "payments", "stock_moves"]
startup_error = None


def db():
    return psycopg.connect(DSN, autocommit=True)


def migrate():
    """Apply the migrations the database has not seen; refuse a newer database."""
    with db() as c:
        if c.execute("SELECT to_regclass('schema_version')").fetchone()[0] is None:
            current = 0
        else:
            current = c.execute("SELECT coalesce(max(version), 0) FROM schema_version").fetchone()[0]
        if current > SUPPORTED:
            raise RuntimeError(f"database schema v{current} is newer than this app (v{SUPPORTED})")
        for m in MIGRATIONS:
            if int(m.name[:3]) > current:
                with c.transaction():
                    c.execute(m.read_text())
        if pw := os.environ.get("ADMIN_PASSWORD"):
            c.execute("INSERT INTO users (email, password_hash, role) VALUES ('admin@nordvik.test', %s, 'admin') "
                      "ON CONFLICT (email) DO NOTHING", (bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode(),))


app = FastAPI(title="Nordvik Handel (stand-in)", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup():
    global startup_error
    try:
        migrate()
    except Exception as e:
        startup_error = str(e)


def schema_version(c):
    return c.execute("SELECT max(version) FROM schema_version").fetchone()[0]


@app.get("/health")
def health():
    if startup_error:
        return JSONResponse({"ok": False, "error": startup_error, "app_version": APP_VERSION}, 503)
    with db() as c:
        return {"ok": True, "schema_version": schema_version(c), "app_version": APP_VERSION}


@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        counts = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
        revenue = c.execute("SELECT coalesce(sum(total_cents), 0) FROM invoices WHERE paid_at IS NOT NULL").fetchone()[0]
    return {"counts": counts, "revenue_cents": revenue}


@app.post("/api/login")
def login(body: dict):
    with db() as c:
        row = c.execute("SELECT password_hash FROM users WHERE email = %s", (str(body.get("email", "")),)).fetchone()
    if not row or not bcrypt.checkpw(str(body.get("password", "")).encode(), row[0].encode()):
        raise HTTPException(401, "bad credentials")
    return {"ok": True}


@app.get("/api/orders")
def orders(limit: int = 20):
    with db() as c:
        rows = c.execute("SELECT o.id, o.customer_id, o.status, o.created_at, i.total_cents FROM orders o "
                         "LEFT JOIN invoices i ON i.order_id = o.id ORDER BY o.id DESC LIMIT %s",
                         (max(1, min(limit, 200)),)).fetchall()
    return [{"id": r[0], "customer_id": r[1], "status": r[2], "created_at": r[3].isoformat(), "total_cents": r[4]}
            for r in rows]


@app.get("/api/invoices/{invoice_id}")
def invoice(invoice_id: int):
    with db() as c:
        r = c.execute("SELECT id, order_id, total_cents, issued_at, paid_at FROM invoices WHERE id = %s",
                      (invoice_id,)).fetchone()
        if not r:
            raise HTTPException(404, "no such invoice")
        paid = c.execute("SELECT coalesce(sum(amount_cents), 0) FROM payments WHERE invoice_id = %s", (invoice_id,)).fetchone()[0]
    return {"id": r[0], "order_id": r[1], "total_cents": r[2], "issued_at": r[3].isoformat(),
            "paid_at": r[4] and r[4].isoformat(), "paid_cents": paid}
