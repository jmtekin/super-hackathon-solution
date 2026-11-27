ALTER TABLE customers ADD COLUMN first_name text, ADD COLUMN last_name text;
UPDATE customers SET first_name = split_part(name, ' ', 1), last_name = substr(name, length(split_part(name, ' ', 1)) + 2);
ALTER TABLE customers ALTER COLUMN first_name SET NOT NULL, ALTER COLUMN last_name SET NOT NULL, DROP COLUMN name;
INSERT INTO schema_version (version) VALUES (2);
