-- The contract's business rules, one JSON line: [{rule, bad, example}], bad = rows breaking it.
WITH lines AS (
  SELECT ol.order_id, round(sum(ol.qty * ol.unit_price_cents * (1 + p.vat_rate))) AS total
  FROM order_lines ol JOIN products p ON p.id = ol.product_id GROUP BY ol.order_id),
paid AS (SELECT invoice_id, sum(amount_cents) AS s FROM payments GROUP BY invoice_id),
moves AS (SELECT product_id, sum(qty_delta) AS s FROM stock_moves GROUP BY product_id),
r AS (
  SELECT 'invoice total = lines x (1 + VAT)' AS rule, i.id AS bad_id
    FROM invoices i LEFT JOIN lines l ON l.order_id = i.order_id WHERE l.total IS DISTINCT FROM i.total_cents
  UNION ALL SELECT 'paid invoice = sum of payments', i.id
    FROM invoices i LEFT JOIN paid p ON p.invoice_id = i.id WHERE i.paid_at IS NOT NULL AND coalesce(p.s, 0) <> i.total_cents
  UNION ALL SELECT 'stock_qty = sum of stock moves', p.id
    FROM products p LEFT JOIN moves m ON m.product_id = p.id WHERE p.stock_qty <> coalesce(m.s, 0)
  UNION ALL SELECT 'FK orders.customer_id', o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = o.customer_id)
  UNION ALL SELECT 'FK order_lines.order_id', x.id FROM order_lines x WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = x.order_id)
  UNION ALL SELECT 'FK order_lines.product_id', x.id FROM order_lines x WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = x.product_id)
  UNION ALL SELECT 'FK invoices.order_id', x.id FROM invoices x WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id = x.order_id)
  UNION ALL SELECT 'FK payments.invoice_id', x.id FROM payments x WHERE NOT EXISTS (SELECT 1 FROM invoices i WHERE i.id = x.invoice_id)
  UNION ALL SELECT 'FK stock_moves.product_id', x.id FROM stock_moves x WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.id = x.product_id)
  UNION ALL SELECT 'seeded admin has a bcrypt hash', NULL WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE email = 'admin@nordvik.test' AND role = 'admin' AND password_hash LIKE '$2%'))
SELECT json_agg(json_build_object('rule', n.rule, 'bad', coalesce(c.bad, 0), 'example', c.example) ORDER BY n.ord)
FROM (VALUES (1, 'invoice total = lines x (1 + VAT)'), (2, 'paid invoice = sum of payments'),
             (3, 'stock_qty = sum of stock moves'), (4, 'FK orders.customer_id'), (5, 'FK order_lines.order_id'),
             (6, 'FK order_lines.product_id'), (7, 'FK invoices.order_id'), (8, 'FK payments.invoice_id'),
             (9, 'FK stock_moves.product_id'), (10, 'seeded admin has a bcrypt hash')) AS n(ord, rule)
LEFT JOIN (SELECT rule, count(*) AS bad, min(bad_id) AS example FROM r GROUP BY rule) c ON c.rule = n.rule;
