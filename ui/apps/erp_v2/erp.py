"""Nordvik ERP, release 2.0: the application a restored backup has to serve.

The compatibility question is not whether a dump loads but whether this code
runs on what it loaded, so the app is real code with its own data layer, booted
inside the sandbox on the restored database. Release 2.0 reads the upgraded
Odoo fields (account_move.move_type, payment_state [Odoo 14], stock_move.quantity
and picked [Odoo 17]).

Usage: python3 -I erp.py <database> [port]   (stdlib only, read-only connection)
"""
import hashlib
import hmac
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0"
DB = sys.argv[1]


def connect():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


# Data layer: every query the app runs, in release 2.0's field names.
def dashboard(db):
    open_invoices, receivable = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_residual), 0) FROM account_move "
        "WHERE move_type = 'out_invoice' AND state = 'posted' AND payment_state != 'paid'").fetchone()
    stock = db.execute("SELECT COALESCE(SUM(quantity), 0) FROM stock_move WHERE picked = 1").fetchone()[0]
    return {"version": VERSION, "open_invoices": open_invoices, "receivable_ore": receivable, "units_moved": stock}


def invoices(db):
    rows = db.execute("""
        SELECT m.name, m.amount_untaxed, m.amount_tax, m.amount_total,
               COALESCE(SUM(CASE WHEN l.display_type = 'product' THEN l.price_subtotal END), 0),
               COALESCE(SUM(CASE WHEN l.display_type = 'tax' THEN l.credit - l.debit END), 0)
        FROM account_move m LEFT JOIN account_move_line l ON l.move_id = m.id
        WHERE m.move_type = 'out_invoice' AND m.state = 'posted' GROUP BY m.id ORDER BY m.id""")
    return [dict(zip(("name", "untaxed", "tax", "total", "lines_untaxed", "lines_tax"), r)) for r in rows]


def trial_balance(db):
    debit, credit = db.execute("SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0) FROM account_move_line").fetchone()
    return {"debit_ore": debit, "credit_ore": credit}


def authenticate(db, login, password):
    row = db.execute("SELECT password FROM res_users WHERE login = ? AND active = 1", (login,)).fetchone()
    if not row:
        return False
    scheme, rounds, salt, digest = row[0].split("$")
    got = hashlib.pbkdf2_hmac(scheme.split("-")[1], password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(got, digest)


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_route(self, route):
        try:
            with connect() as db:
                code, body = route(db)
        except (sqlite3.Error, ValueError) as e:
            code, body = 500, {"error": f"{type(e).__name__}: {e}"}
        self.reply(code, body)

    def do_GET(self):
        routes = {"/api/dashboard": dashboard, "/api/invoices": invoices, "/api/trial-balance": trial_balance}
        if self.path not in routes:
            return self.reply(404, {"error": "not found"})
        self.handle_route(lambda db: (200, routes[self.path](db)))

    def do_POST(self):
        if self.path != "/api/login":
            return self.reply(404, {"error": "not found"})
        creds = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.handle_route(lambda db: (200, {"ok": True}) if authenticate(db, creds.get("login", ""), creds.get("password", ""))
                          else (401, {"ok": False}))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 8080), Handler).serve_forever()
