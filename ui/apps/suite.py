"""The business-logic suite, run inside the sandbox against the booted app version.

Exit code 0 from a restore proves nothing; these four tests ask what the
business needs from a restored ERP: can the bookkeeper log in, do the ledger
and every invoice add up as the app reads them, does every row point at a real
record, does the app's API answer. Deterministic, no model. They talk to the
app over loopback (the sandbox has no other network) and print one tagged JSON
line; the control plane re-checks the books on the returned database itself.

Usage: python3 -I suite.py <database> <login.json> [port]
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB, LOGIN = sys.argv[1], json.load(open(sys.argv[2]))
BASE = f"http://127.0.0.1:{sys.argv[3] if len(sys.argv) > 3 else 8080}"
TAG = "@@SUITE "


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def api_health():
    code, body = call("/api/dashboard")
    if code != 200:
        return False, f"GET /api/dashboard {code}: {body.get('error', '')}"
    return True, f"GET /api/dashboard 200: app v{body['version']}, {body['open_invoices']} open invoices"


def login():
    code, _ = call("/api/login", LOGIN)
    wrong, _ = call("/api/login", {**LOGIN, "password": LOGIN["password"] + "x"})
    ok = code == 200 and wrong == 401
    return ok, (f"{LOGIN['login']} logs in, a wrong password is refused" if ok else
                f"{LOGIN['login']}: right password {code}, wrong password {wrong} (expected 200 / 401)")


def ledger():
    code, tb = call("/api/trial-balance")
    if code != 200:
        return False, f"GET /api/trial-balance {code}: {tb.get('error', '')}"
    code, inv = call("/api/invoices")
    if code != 200:
        return False, f"GET /api/invoices {code}: {inv.get('error', '')}"
    bad = [i["name"] for i in inv if not (i["untaxed"] + i["tax"] == i["total"]
                                          and i["lines_untaxed"] == i["untaxed"] and i["lines_tax"] == i["tax"])]
    sek = lambda ore: f"{ore / 100:,.2f} SEK"
    if tb["debit_ore"] != tb["credit_ore"]:
        return False, f"debits {sek(tb['debit_ore'])} != credits {sek(tb['credit_ore'])}"
    if bad or not inv:
        return False, f"{len(bad)} of {len(inv)} invoices do not add up" + (f", first {bad[0]}" if bad else "")
    return True, f"debits = credits = {sek(tb['debit_ore'])}; {len(inv):,} invoices: SUM(lines) + VAT = total"


def foreign_keys():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute("PRAGMA foreign_key_check").fetchall()
    tables = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    if rows:
        return False, f"{len(rows)} rows point at missing records, first {rows[0][0]} row {rows[0][1]} -> {rows[0][2]}"
    return True, f"{tables} tables, every foreign key resolves"


def main():
    for _ in range(100):  # the app boots in the background; give it up to 5 s
        try:
            urllib.request.urlopen(BASE + "/api/none", timeout=1)
        except urllib.error.HTTPError:
            break
        except OSError:
            time.sleep(0.05)
    results = []
    for name, test in (("API health", api_health), ("User login (hash check)", login),
                       ("Ledger + invoice math", ledger), ("FK integrity", foreign_keys)):
        try:
            ok, detail = test()
        except Exception as e:  # a test that crashes fails; it never takes the suite down
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append({"test": name, "pass": ok, "detail": detail})
    print(TAG + json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
