"""Deterministic checks of a restored ERP database.

Two kinds, no model in either: the verdict must not depend on anything that
can be talked into a different answer.
  The books: accounting and stock invariants that hold in any healthy ERP,
    whatever the backup claims (debits = credits, invoices add up, ledgers
    agree). They catch damage even where no manifest exists.
  The manifest: row counts, row hashes, schema and a smoke query against what
    was recorded at backup time, in the control plane, never in the backup.
They report in business terms because the reader is whoever owns the books.
The restored file is untrusted, so it is opened read-only with trusted_schema
off (no view/trigger side effects).
"""
import sqlite3

from fixtures.build import row_hash, table_digest


def _connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _check(name, ok, detail, items):
    return {"check": name, "pass": ok, "detail": detail, "items": items}


def _sek(ore):
    return f"{ore / 100:,.2f} SEK"


def _guard(name, f):
    """A restore that lost a table or column fails the check instead of crashing the verifier."""
    def check(db, exp):
        try:
            return f(db)
        except sqlite3.Error as e:
            return _check(name, False, f"cannot be read: {e}", [])
    return check


def books_balance(db):
    debit, credit = db.execute("SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0) FROM account_move_line").fetchone()
    off = db.execute("SELECT m.name, SUM(l.debit), SUM(l.credit) FROM account_move_line l "
                     "JOIN account_move m ON m.id = l.move_id GROUP BY l.move_id "
                     "HAVING SUM(l.debit) != SUM(l.credit) ORDER BY l.move_id").fetchall()
    ok = debit == credit and not off
    items = [{"what": "debits", "expected": _sek(credit), "got": _sek(debit), "pass": debit == credit}]
    items += [{"what": n, "expected": "debit = credit", "got": f"{_sek(d)} / {_sek(c)}", "pass": False} for n, d, c in off]
    return _check("Books balance", ok, f"debits {_sek(debit)} = credits" if ok else
                  f"do not balance: debits {_sek(debit)}, credits {_sek(credit)}, off by {_sek(abs(debit - credit))}; "
                  f"{len(off)} journal {'entry' if len(off) == 1 else 'entries'} with debit != credit", items)


def invoice_totals(db):
    """Each invoice: untaxed = its product lines, VAT = 25 % per line, total = untaxed + VAT = the receivable."""
    rows = db.execute("""
        SELECT m.name, m.amount_untaxed, m.amount_tax, m.amount_total,
               COALESCE(SUM(CASE WHEN l.display_type = 'product' THEN l.price_subtotal END), 0),
               COALESCE(SUM(CASE WHEN l.display_type = 'tax' THEN l.credit - l.debit END), 0),
               COALESCE(SUM(CASE WHEN l.display_type = 'payment_term' THEN l.debit - l.credit END), 0),
               COALESCE(SUM(CASE WHEN l.display_type = 'product'
                            AND l.price_total != l.price_subtotal + (l.price_subtotal * 25 + 50) / 100 THEN 1 END), 0)
        FROM account_move m LEFT JOIN account_move_line l ON l.move_id = m.id
        WHERE m.move_type = 'out_invoice' AND m.state = 'posted' GROUP BY m.id ORDER BY m.id""").fetchall()
    bad = [r for r in rows if not (r[1] == r[4] and r[2] == r[5] and r[3] == r[1] + r[2] == r[6] and not r[7])]
    items = [{"what": r[0], "expected": _sek(r[3]), "got": f"lines {_sek(r[4] + r[5])}", "pass": False} for r in bad]
    items += [{"what": r[0], "expected": _sek(r[3]), "got": _sek(r[3]), "pass": True} for r in rows[::max(1, len(rows) // 3)]]
    return _check("Invoice totals", bool(rows) and not bad,
                  f"{len(rows):,} invoices: total = lines + 25 % VAT" if not bad else
                  f"{len(bad):,} of {len(rows):,} invoices do not add up, first {bad[0][0]}", items)


def receivables(db):
    """Per customer, the receivable account (1510) must equal what their open invoices say they owe."""
    rows = db.execute("""
        WITH ledger AS (SELECT l.partner_id p, SUM(l.debit - l.credit) amt FROM account_move_line l
                        JOIN account_account a ON a.id = l.account_id
                        WHERE a.account_type = 'asset_receivable' GROUP BY l.partner_id),
             open AS (SELECT partner_id p, SUM(amount_residual) amt FROM account_move
                      WHERE move_type = 'out_invoice' AND state = 'posted' GROUP BY partner_id)
        SELECT r.id, COALESCE(ledger.amt, 0), COALESCE(open.amt, 0) FROM res_partner r
        LEFT JOIN ledger ON ledger.p = r.id LEFT JOIN open ON open.p = r.id
        WHERE r.customer_rank > 0 OR ledger.p IS NOT NULL OR open.p IS NOT NULL ORDER BY r.id""").fetchall()
    bad = [r for r in rows if r[1] != r[2]]
    # The biggest open balances first: a match on 0.00 SEK proves little.
    shown = bad[:4] + sorted((r for r in rows if r[1] == r[2]), key=lambda r: -r[2])[:6 - min(len(bad), 4)]
    items = [{"what": f"Customer {r[0]}", "expected": _sek(r[2]), "got": _sek(r[1]), "pass": r[1] == r[2]} for r in shown]
    total = sum(r[2] for r in rows)
    return _check("Customer receivables", bool(rows) and not bad,
                  f"{len(rows)} customers: ledger = open invoices ({_sek(total)} owed)" if not bad else
                  f"{len(bad)} of {len(rows)} customers' ledger differs from their open invoices", items)


def stock_on_hand(db):
    rows = db.execute("""
        WITH moved AS (SELECT m.product_id p, SUM(CASE WHEN d.usage = 'internal' THEN m.quantity ELSE 0 END)
                              - SUM(CASE WHEN s.usage = 'internal' THEN m.quantity ELSE 0 END) qty
                       FROM stock_move m JOIN stock_location s ON s.id = m.location_id
                       JOIN stock_location d ON d.id = m.location_dest_id WHERE m.state = 'done' GROUP BY m.product_id),
             quant AS (SELECT q.product_id p, SUM(q.quantity) qty FROM stock_quant q
                       JOIN stock_location l ON l.id = q.location_id WHERE l.usage = 'internal' GROUP BY q.product_id)
        SELECT pp.default_code, COALESCE(quant.qty, 0), COALESCE(moved.qty, 0) FROM product_product pp
        LEFT JOIN quant ON quant.p = pp.id LEFT JOIN moved ON moved.p = pp.id ORDER BY pp.id""").fetchall()
    bad = [r for r in rows if r[1] != r[2]]
    items = [{"what": r[0], "expected": f"{r[2]} moved", "got": f"{r[1]} on hand", "pass": r[1] == r[2]}
             for r in (bad + [r for r in rows if r[1] == r[2]])[:6]]
    return _check("Stock on hand", bool(rows) and not bad,
                  f"{len(rows)} products: on hand = sum of stock moves" if not bad else
                  f"{len(bad)} of {len(rows)} products differ from their stock moves", items)


# (what, rows that point at nothing)
ORPHANS = [
    ("journal lines without entry", "SELECT COUNT(*) FROM account_move_line WHERE move_id NOT IN (SELECT id FROM account_move)"),
    ("journal lines on unknown account", "SELECT COUNT(*) FROM account_move_line WHERE account_id NOT IN (SELECT id FROM account_account)"),
    ("journal lines for unknown partner", "SELECT COUNT(*) FROM account_move_line WHERE partner_id NOT IN (SELECT id FROM res_partner)"),
    ("journal entries without lines", "SELECT COUNT(*) FROM account_move WHERE id NOT IN (SELECT move_id FROM account_move_line)"),
    ("invoices for unknown customer", "SELECT COUNT(*) FROM account_move WHERE partner_id NOT IN (SELECT id FROM res_partner)"),
    ("order lines without order", "SELECT COUNT(*) FROM sale_order_line WHERE order_id NOT IN (SELECT id FROM sale_order)"),
    ("order lines for unknown product", "SELECT COUNT(*) FROM sale_order_line WHERE product_id NOT IN (SELECT id FROM product_product)"),
    ("stock moves for unknown product", "SELECT COUNT(*) FROM stock_move WHERE product_id NOT IN (SELECT id FROM product_product)"),
]


def orphans(db):
    items = [{"what": what, "expected": 0, "got": n, "pass": n == 0}
             for what, sql in ORPHANS for (n,) in [db.execute(sql).fetchone()]]
    bad = [i for i in items if not i["pass"]]
    return _check("No orphan lines", not bad, f"{len(items)} relations, every row points at a real record" if not bad else
                  ", ".join(f"{i['got']:,} {i['what']}" for i in bad), sorted(items, key=lambda i: i["pass"]))


BOOKS = [_guard("Books balance", books_balance), _guard("Invoice totals", invoice_totals),
         _guard("Customer receivables", receivables), _guard("Stock on hand", stock_on_hand),
         _guard("No orphan lines", orphans)]


def row_counts(db, exp):
    items = []
    for t, want in exp["row_counts"].items():
        try:
            got = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error as e:
            got = f"error: {e}"
        items.append({"what": t, "expected": want, "got": got, "pass": got == want})
    bad = [i for i in items if not i["pass"]]
    return _check("Row counts", not bad, "all tables match the manifest" if not bad else
                  ", ".join(f"{i['what']} {i['got']}/{i['expected']}" for i in bad), items)


def row_hashes(db, exp):
    items = []
    for t, ids in exp["row_hashes"].items():
        for rid, want in ids.items():
            try:
                row = db.execute(f'SELECT * FROM "{t}" WHERE id=?', (int(rid),)).fetchone()
            except sqlite3.Error:
                row = None
            got = row_hash(row) if row else "missing"
            items.append({"what": f"{t}#{rid}", "expected": want[:16], "got": got[:16], "pass": got == want})
    bad = [i for i in items if not i["pass"]]
    return _check("Row hashes (sha256)", not bad, f"{len(items) - len(bad)}/{len(items)} sampled rows identical", items)


def schema(db, exp):
    items = []
    for t, cols in exp["schema"].items():
        got = [[c[1], c[2], bool(c[3])] for c in db.execute(f'PRAGMA table_info("{t}")')]
        items.append({"what": t, "expected": f"{len(cols)} columns", "got": f"{len(got)} columns" if got else "missing",
                      "pass": got == cols})
    if "user_version" in exp:
        got = db.execute("PRAGMA user_version").fetchone()[0]
        items.append({"what": "user_version", "expected": exp["user_version"], "got": got,
                      "pass": got == exp["user_version"]})
    bad = [i for i in items if not i["pass"]]
    return _check("Schema assertions", not bad, "tables, columns, types, NOT NULL match" if not bad else
                  "mismatch in " + ", ".join(i["what"] for i in bad), items)


def table_digests(db, exp):
    items = []
    for t, want in exp["table_digests"].items():
        try:
            got = table_digest(db, t)
        except sqlite3.Error as e:
            got = f"error: {e}"
        items.append({"what": t, "expected": want[:16], "got": got[:16], "pass": got == want})
    bad = [i for i in items if not i["pass"]]
    return _check("Table digests (sha256)", not bad, "every row of every table matches the manifest" if not bad else
                  "rows differ in " + ", ".join(i["what"] for i in bad), items)


def smoke(db, exp):
    try:
        got = list(db.execute(exp["smoke"]["sql"]).fetchone() or [])
    except sqlite3.Error as e:
        got = [f"error: {e}"]
    ok = got == exp["smoke"]["expect"]
    return _check("ERP smoke query", ok, "customer 42's invoices as the ERP reads them", [
        {"what": exp["smoke"]["sql"], "expected": exp["smoke"]["expect"], "got": got, "pass": ok}])


def run(path, expected):
    try:
        db = _connect(path)
        db.execute("SELECT name FROM sqlite_master").fetchall()
    except sqlite3.Error as e:
        return [_check("Open restored database", False, str(e), [])]
    try:
        checks = BOOKS + [row_counts, row_hashes] + ([table_digests] if "table_digests" in expected else []) + [schema, smoke]
        return [f(db, expected) for f in checks]
    finally:
        db.close()
