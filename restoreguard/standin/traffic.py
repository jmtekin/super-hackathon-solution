"""STAND-IN traffic: a few business events a minute with bursts, every one a
single transaction that keeps the contract's rules true (invoice totals are
computed by the database with the rule's own expression)."""
import os
import random
import time

import psycopg

DSN = os.environ.get("DATABASE_URL", "postgresql://nordvik@db/nordvik")
RATE = float(os.environ.get("EVENTS_PER_MIN", "4"))
TOTAL = ("SELECT round(sum(l.qty * l.unit_price_cents * (1 + p.vat_rate))) FROM order_lines l "
         "JOIN products p ON p.id = l.product_id WHERE l.order_id = %s")


def signup(c):
    first, last = random.choice(["Ida", "Nils", "Elsa", "Hugo"]), random.choice(["Berg", "Holm", "Lund"])
    email, city = f"web{random.randrange(10**9)}@example.se", random.choice(["Stockholm", "Lund", "Umeå"])
    if c.execute("SELECT max(version) FROM schema_version").fetchone()[0] >= 2:
        c.execute("INSERT INTO customers (first_name, last_name, email, city) VALUES (%s, %s, %s, %s)",
                  (first, last, email, city))
    else:
        c.execute("INSERT INTO customers (name, email, city) VALUES (%s, %s, %s)", (f"{first} {last}", email, city))


def order(c):
    cid = c.execute("SELECT id FROM customers ORDER BY random() LIMIT 1").fetchone()[0]
    oid = c.execute("INSERT INTO orders (customer_id, status) VALUES (%s, 'new') RETURNING id", (cid,)).fetchone()[0]
    for pid, price in c.execute("SELECT id, price_cents FROM products ORDER BY random() LIMIT %s",
                                (random.randint(1, 4),)).fetchall():
        qty = random.randint(1, 3)
        c.execute("INSERT INTO order_lines (order_id, product_id, qty, unit_price_cents) VALUES (%s, %s, %s, %s)",
                  (oid, pid, qty, price))
        c.execute("INSERT INTO stock_moves (product_id, qty_delta, reason) VALUES (%s, %s, 'sale')", (pid, -qty))
        c.execute("UPDATE products SET stock_qty = stock_qty - %s WHERE id = %s", (qty, pid))
    c.execute(f"INSERT INTO invoices (order_id, total_cents) VALUES (%s, ({TOTAL}))", (oid, oid))


def pay(c):
    row = c.execute("SELECT i.id, i.total_cents, i.order_id FROM invoices i JOIN orders o ON o.id = i.order_id "
                    "WHERE o.status = 'new' ORDER BY random() LIMIT 1").fetchone()
    if row:
        c.execute("INSERT INTO payments (invoice_id, amount_cents, method) VALUES (%s, %s, %s)",
                  (row[0], row[1], random.choice(["card", "swish", "invoice"])))
        c.execute("UPDATE invoices SET paid_at = now() WHERE id = %s", (row[0],))
        c.execute("UPDATE orders SET status = 'paid' WHERE id = %s", (row[2],))


def ship(c):
    c.execute("UPDATE orders SET status = 'shipped' WHERE id = (SELECT id FROM orders WHERE status = 'paid' "
              "ORDER BY random() LIMIT 1)")


def refund(c):
    row = c.execute("SELECT id FROM orders WHERE status = 'shipped' ORDER BY random() LIMIT 1").fetchone()
    if row:
        c.execute("UPDATE orders SET status = 'refunded' WHERE id = %s", row)
        for pid, qty in c.execute("SELECT product_id, qty FROM order_lines WHERE order_id = %s", row).fetchall():
            c.execute("INSERT INTO stock_moves (product_id, qty_delta, reason) VALUES (%s, %s, 'refund')", (pid, qty))
            c.execute("UPDATE products SET stock_qty = stock_qty + %s WHERE id = %s", (qty, pid))


def restock(c):
    pid, qty = c.execute("SELECT id FROM products ORDER BY stock_qty LIMIT 1").fetchone()[0], random.randint(50, 300)
    c.execute("INSERT INTO stock_moves (product_id, qty_delta, reason) VALUES (%s, %s, 'restock')", (pid, qty))
    c.execute("UPDATE products SET stock_qty = stock_qty + %s WHERE id = %s", (qty, pid))


EVENTS = [(signup, 2), (order, 5), (pay, 4), (ship, 3), (refund, 1), (restock, 1)]

if __name__ == "__main__":
    while True:
        burst = random.randint(5, 15) if random.random() < 0.05 else 1
        try:
            with psycopg.connect(DSN) as c:
                for _ in range(burst):
                    with c.transaction():
                        random.choices([e for e, _ in EVENTS], [w for _, w in EVENTS])[0](c)
        except psycopg.Error as e:
            print("traffic:", e, flush=True)
        time.sleep(random.expovariate(RATE / 60))
