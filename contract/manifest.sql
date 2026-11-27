-- The manifest's data part: row counts and invariant sums, one JSON line.
-- The backup job runs it in the dump's own snapshot; RestoreGuard runs it on the
-- restored copy, so both sides are the same query by construction.
SELECT json_build_object(
  'schema_version', (SELECT max(version) FROM schema_version),
  'row_counts', json_build_object(
    'users', (SELECT count(*) FROM users), 'customers', (SELECT count(*) FROM customers),
    'products', (SELECT count(*) FROM products), 'orders', (SELECT count(*) FROM orders),
    'order_lines', (SELECT count(*) FROM order_lines), 'invoices', (SELECT count(*) FROM invoices),
    'payments', (SELECT count(*) FROM payments), 'stock_moves', (SELECT count(*) FROM stock_moves),
    'schema_version', (SELECT count(*) FROM schema_version)),
  'invariants', json_build_object(
    'invoice_total_sum_cents', (SELECT coalesce(sum(total_cents), 0) FROM invoices),
    'payments_sum_cents', (SELECT coalesce(sum(amount_cents), 0) FROM payments),
    'stock_qty_sum', (SELECT coalesce(sum(stock_qty), 0) FROM products)));
