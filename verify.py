"""Deterministic checks of a restored database against the backup-time manifest.

No model is involved here on purpose: the verdict must not depend on anything
that can be talked into a different answer. The manifest comes from the control
plane, never from the backup. The restored file is untrusted, so it is opened
read-only with trusted_schema off (no view/trigger side effects).
"""
import sqlite3

from fixtures.build import row_hash, table_digest


def _connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _check(name, ok, detail, items):
    return {"check": name, "pass": ok, "detail": detail, "items": items}


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
    return _check("App smoke query", ok, "customer 42 order history as the app reads it", [
        {"what": exp["smoke"]["sql"], "expected": exp["smoke"]["expect"], "got": got, "pass": ok}])


def run(path, expected):
    try:
        db = _connect(path)
        db.execute("SELECT name FROM sqlite_master").fetchall()
    except sqlite3.Error as e:
        return [_check("Open restored database", False, str(e), [])]
    try:
        checks = (row_counts, row_hashes) + ((table_digests,) if "table_digests" in expected else ()) + (schema, smoke)
        return [f(db, expected) for f in checks]
    finally:
        db.close()
