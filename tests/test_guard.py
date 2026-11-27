"""Failure cases of the parts that need no sandbox: config refusal and backup loading."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "restoreguard"))
import guard  # noqa: E402

GOOD = {"version": "1.0.0", "image": "nordvik-standin:1.0.0", "schema": 1, "port": 8000, "migrations": "/app/migrations"}


class ProdRefusal(unittest.TestCase):
    def test_good_config_passes(self):
        self.assertEqual(guard.check_app(dict(GOOD)), GOOD)
        self.assertTrue(guard.apps())

    def test_prod_dsn_key_refused(self):
        for key in ("database_url", "dsn", "host"):
            with self.assertRaisesRegex(ValueError, "test instance"):
                guard.check_app({**GOOD, key: "postgresql://nordvik@prod-db.nordvik.se/nordvik"})

    def test_dsn_smuggled_into_a_known_key_refused(self):
        for value in ("postgresql://nordvik@10.0.0.5:5432/nordvik", "host=prod-db dbname=nordvik",
                      "x@prod-db.nordvik.se:5432/nordvik"):
            with self.assertRaisesRegex(ValueError, "database address"):
                guard.check_app({**GOOD, "label": value})

    def test_missing_key_refused(self):
        with self.assertRaisesRegex(ValueError, "missing image"):
            guard.check_app({k: v for k, v in GOOD.items() if k != "image"})

    def test_app_only_ever_gets_the_sandbox_dsn(self):
        self.assertTrue(guard.TEST_DSN.startswith("postgresql://postgres@127.0.0.1:"))
        src = Path(guard.__file__).read_text()
        self.assertEqual(src.count("DATABASE_URL="), 1)
        self.assertIn('f"DATABASE_URL={TEST_DSN}"', src)


class LoadBackup(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.dump = self.d / "20261127T120000Z_v1.dump"
        self.dump.write_bytes(b"PGDMP")

    def test_missing_manifest(self):
        with self.assertRaisesRegex(ValueError, "no manifest"):
            guard.load_backup(self.dump)

    def test_malformed_manifest(self):
        self.dump.with_suffix(".json").write_text("{not json")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            guard.load_backup(self.dump)

    def test_manifest_missing_fields(self):
        self.dump.with_suffix(".json").write_text(json.dumps({"file": "x", "sha256": "y"}))
        with self.assertRaisesRegex(ValueError, "lacks invariants, row_counts, schema_version"):
            guard.load_backup(self.dump)

    def test_unreadable_manifest(self):
        m = self.dump.with_suffix(".json")
        m.write_text("{}")
        m.chmod(0)
        with self.assertRaisesRegex(ValueError, "unreadable"):
            guard.load_backup(self.dump)


if __name__ == "__main__":
    unittest.main()
