-- Deterministic six months of Nordvik Handel history (psql -v scale=1.0). Every
-- business rule in contract/rules.sql holds when this finishes; ids, amounts and
-- dates depend only on the seed below and the scale.
\set ON_ERROR_STOP on
SELECT setseed(0.42);
BEGIN;
INSERT INTO customers (name, email, city, created_at)
SELECT a.f[1 + floor(random() * 12)::int] || ' ' || a.l[1 + floor(random() * 12)::int], 'kund' || g || '@example.se',
       a.c[1 + floor(random() * 8)::int], timestamptz '2026-05-27 08:00+00' + random() * interval '180 days'
FROM generate_series(1, (5000 * :scale)::int) g,
     (SELECT ARRAY['Anna','Erik','Maria','Lars','Karin','Johan','Sara','Anders','Emma','Per','Lena','Oskar'] f,
             ARRAY['Andersson','Johansson','Karlsson','Nilsson','Eriksson','Larsson','Olsson','Persson','Svensson',
                   'Gustafsson','Pettersson','Lindberg'] l,
             ARRAY['Stockholm','Göteborg','Malmö','Uppsala','Västerås','Örebro','Linköping','Umeå'] c) a;
INSERT INTO products (sku, name, price_cents, vat_rate)
SELECT 'NV-' || lpad(g::text, 5, '0'), (ARRAY['Hammare','Skruvdragare','Lampa','Kaffebryggare','Stol','Matta','Kastrull',
        'Hörlurar','Ryggsäck','Termos'])[1 + g % 10] || ' ' || g, 4900 + floor(random() * 190000)::int,
       (ARRAY[0.25, 0.25, 0.25, 0.12, 0.06])[1 + g % 5]
FROM generate_series(1, (300 * :scale)::int) g;
INSERT INTO stock_moves (product_id, qty_delta, reason, created_at)
SELECT id, 500 + floor(random() * 1500)::int, 'restock', timestamptz '2026-05-27 06:00+00' FROM products;
INSERT INTO orders (customer_id, status, created_at)
SELECT 1 + floor(random() * (SELECT count(*) FROM customers))::int,
       CASE WHEN g % 20 = 0 THEN 'refunded' WHEN g % 10 = 1 THEN 'new' WHEN g % 3 = 0 THEN 'paid' ELSE 'shipped' END,
       timestamptz '2026-05-27 09:00+00' + (g::float / (30000 * :scale)) * interval '180 days'
FROM generate_series(1, (30000 * :scale)::int) g;
INSERT INTO order_lines (order_id, product_id, qty, unit_price_cents)
SELECT o.id, p.id, 1 + (o.id + k) % 3, p.price_cents
FROM orders o CROSS JOIN LATERAL generate_series(1, 1 + o.id % 4) k
JOIN products p ON p.id = 1 + (o.id * 37 + k * 101) % (SELECT count(*) FROM products);
INSERT INTO stock_moves (product_id, qty_delta, reason, created_at)
SELECT l.product_id, -l.qty, 'sale', o.created_at FROM order_lines l JOIN orders o ON o.id = l.order_id;
INSERT INTO invoices (order_id, total_cents, issued_at, paid_at)
SELECT o.id, t.total, o.created_at, CASE WHEN o.status <> 'new' THEN o.created_at + interval '1 day' END
FROM orders o JOIN (SELECT l.order_id, round(sum(l.qty * l.unit_price_cents * (1 + p.vat_rate))) AS total
                    FROM order_lines l JOIN products p ON p.id = l.product_id GROUP BY l.order_id) t ON t.order_id = o.id;
INSERT INTO payments (invoice_id, amount_cents, method, created_at)
SELECT id, total_cents, (ARRAY['card', 'swish', 'invoice'])[1 + id % 3], paid_at FROM invoices WHERE paid_at IS NOT NULL;
INSERT INTO stock_moves (product_id, qty_delta, reason, created_at)
SELECT l.product_id, l.qty, 'refund', o.created_at + interval '10 days'
FROM order_lines l JOIN orders o ON o.id = l.order_id WHERE o.status = 'refunded';
UPDATE products p SET stock_qty = m.s FROM (SELECT product_id, sum(qty_delta) s FROM stock_moves GROUP BY product_id) m
WHERE m.product_id = p.id;
COMMIT;
