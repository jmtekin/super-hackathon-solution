CREATE TABLE users (id serial PRIMARY KEY, email text UNIQUE NOT NULL, password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin', 'staff')), created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE customers (id serial PRIMARY KEY, name text NOT NULL, email text UNIQUE NOT NULL, city text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE products (id serial PRIMARY KEY, sku text UNIQUE NOT NULL, name text NOT NULL,
  price_cents integer NOT NULL, vat_rate numeric(4,2) NOT NULL, stock_qty integer NOT NULL DEFAULT 0);
CREATE TABLE orders (id serial PRIMARY KEY, customer_id integer NOT NULL REFERENCES customers,
  status text NOT NULL CHECK (status IN ('new', 'paid', 'shipped', 'refunded')), created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE order_lines (id serial PRIMARY KEY, order_id integer NOT NULL REFERENCES orders,
  product_id integer NOT NULL REFERENCES products, qty integer NOT NULL, unit_price_cents integer NOT NULL);
CREATE TABLE invoices (id serial PRIMARY KEY, order_id integer UNIQUE NOT NULL REFERENCES orders,
  total_cents bigint NOT NULL, issued_at timestamptz NOT NULL DEFAULT now(), paid_at timestamptz);
CREATE TABLE payments (id serial PRIMARY KEY, invoice_id integer NOT NULL REFERENCES invoices,
  amount_cents bigint NOT NULL, method text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE stock_moves (id serial PRIMARY KEY, product_id integer NOT NULL REFERENCES products,
  qty_delta integer NOT NULL, reason text NOT NULL CHECK (reason IN ('sale', 'restock', 'refund')),
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE schema_version (version integer NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_version (version) VALUES (1);
