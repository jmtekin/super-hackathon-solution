CREATE TABLE account_move (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  partner_id INTEGER REFERENCES res_partner(id),
  date TEXT NOT NULL,
  invoice_date TEXT,
  invoice_date_due TEXT,
  invoice_origin TEXT,
  state TEXT NOT NULL,
  currency TEXT NOT NULL,
  amount_untaxed INTEGER NOT NULL,
  amount_tax INTEGER NOT NULL,
  amount_total INTEGER NOT NULL,
  amount_residual INTEGER NOT NULL,
  invoice_payment_state TEXT NOT NULL
);
CREATE TABLE stock_move (
  id INTEGER PRIMARY KEY,
  reference TEXT NOT NULL,
  origin TEXT,
  product_id INTEGER NOT NULL REFERENCES product_product(id),
  location_id INTEGER NOT NULL REFERENCES stock_location(id),
  location_dest_id INTEGER NOT NULL REFERENCES stock_location(id),
  product_uom_qty INTEGER NOT NULL,
  quantity_done INTEGER NOT NULL,
  state TEXT NOT NULL,
  date TEXT NOT NULL
);

CREATE TABLE res_partner (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  is_company INTEGER NOT NULL,
  email TEXT,
  vat TEXT,
  city TEXT,
  country_code TEXT NOT NULL,
  customer_rank INTEGER NOT NULL DEFAULT 0,
  supplier_rank INTEGER NOT NULL DEFAULT 0,
  create_date TEXT NOT NULL
);
CREATE TABLE res_users (
  id INTEGER PRIMARY KEY,
  login TEXT NOT NULL UNIQUE,
  password TEXT NOT NULL, -- pbkdf2-sha512, as Odoo stores it
  partner_id INTEGER NOT NULL REFERENCES res_partner(id),
  active INTEGER NOT NULL
);
CREATE TABLE product_product (
  id INTEGER PRIMARY KEY,
  default_code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  list_price INTEGER NOT NULL,
  standard_price INTEGER NOT NULL
);
CREATE TABLE account_account (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  account_type TEXT NOT NULL
);
CREATE TABLE account_move_line (
  id INTEGER PRIMARY KEY,
  move_id INTEGER NOT NULL REFERENCES account_move(id),
  account_id INTEGER NOT NULL REFERENCES account_account(id),
  partner_id INTEGER REFERENCES res_partner(id),
  product_id INTEGER REFERENCES product_product(id),
  display_type TEXT NOT NULL,
  name TEXT,
  quantity INTEGER,
  price_unit INTEGER,
  price_subtotal INTEGER,
  price_total INTEGER,
  debit INTEGER NOT NULL,
  credit INTEGER NOT NULL
);
CREATE TABLE sale_order (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  partner_id INTEGER NOT NULL REFERENCES res_partner(id),
  date_order TEXT NOT NULL,
  state TEXT NOT NULL,
  invoice_status TEXT NOT NULL,
  amount_untaxed INTEGER NOT NULL,
  amount_tax INTEGER NOT NULL,
  amount_total INTEGER NOT NULL
);
CREATE TABLE sale_order_line (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES sale_order(id),
  product_id INTEGER NOT NULL REFERENCES product_product(id),
  name TEXT NOT NULL,
  product_uom_qty INTEGER NOT NULL,
  price_unit INTEGER NOT NULL,
  price_subtotal INTEGER NOT NULL,
  price_tax INTEGER NOT NULL,
  price_total INTEGER NOT NULL
);
CREATE TABLE stock_location (
  id INTEGER PRIMARY KEY,
  complete_name TEXT NOT NULL,
  usage TEXT NOT NULL
);
CREATE TABLE stock_quant (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES product_product(id),
  location_id INTEGER NOT NULL REFERENCES stock_location(id),
  quantity INTEGER NOT NULL
);
CREATE INDEX account_move_line_move ON account_move_line(move_id);
CREATE INDEX account_move_line_partner ON account_move_line(partner_id);
CREATE INDEX sale_order_line_order ON sale_order_line(order_id);
CREATE INDEX stock_move_product ON stock_move(product_id);
PRAGMA user_version = 1;
