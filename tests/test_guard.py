"""Failure cases of the parts that need no sandbox: config refusal, backup loading, the service queue."""
import asyncio
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "restoreguard"))
import guard  # noqa: E402
import api  # noqa: E402
from fastapi import HTTPException  # noqa: E402

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


class Service(unittest.TestCase):
    """The worker and rerun with the sandbox replaced; results go to a temp dir."""
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        api.RESULTS, api.backups, api.queue = self.d, {}, asyncio.Queue()

    def test_a_crashing_backup_does_not_stop_the_queue(self):
        async def crash(*_):
            raise RuntimeError("boom")

        async def go():
            for bid in ("a_v1", "b_v1"):
                api.backups[bid] = {"id": bid, "cells": {}}
                api.queue.put_nowait(bid)
            task = asyncio.create_task(api.worker())
            while api.backups["b_v1"].get("status") != "failed":
                await asyncio.sleep(0.01)
            task.cancel()
        with mock.patch.object(guard, "run_cell", crash):
            asyncio.run(asyncio.wait_for(go(), 5))
        for bid in ("a_v1", "b_v1"):
            self.assertEqual(api.backups[bid]["reason"], "runtime error: boom")
            self.assertEqual(json.loads((self.d / f"{bid}.json").read_text())["status"], "failed")

    def test_rerun_rereads_a_manifest_that_was_unreadable_when_found(self):
        dump = self.d / "20261127T120000Z_v1.dump"
        dump.write_bytes(b"PGDMP")
        dump.with_suffix(".json").write_text("{not js")
        api.backups[dump.stem] = {"id": dump.stem, "file": dump.name, "path": str(dump), "manifest": None,
                                  "status": "failed", "reason": "manifest unreadable", "cells": {}}
        with self.assertRaises(HTTPException) as e:
            api.rerun(dump.stem)
        self.assertEqual(e.exception.status_code, 409)
        self.assertIn("unreadable", e.exception.detail)
        dump.with_suffix(".json").write_text(json.dumps(
            {"file": dump.name, "sha256": "x", "schema_version": 1, "row_counts": {}, "invariants": {}}))
        self.assertEqual(api.rerun(dump.stem), {"queued": dump.stem})
        self.assertEqual(api.backups[dump.stem]["manifest"]["schema_version"], 1)
        self.assertEqual(api.queue.get_nowait(), dump.stem)


    def test_a_backup_whose_dump_is_gone_leaves_the_list(self):
        dump = self.d / "20261127T120000Z_v1.dump"
        dump.write_bytes(b"PGDMP")
        for bid, status in (("gone_v1", "dr_ready"), ("busy_v1", "running")):
            api.backups[bid] = {"id": bid, "path": str(self.d / "missing.dump"), "status": status}
        api.backups[dump.stem] = {"id": dump.stem, "path": str(dump), "status": "dr_ready"}
        with mock.patch.object(api, "DIRS", []), mock.patch.object(api, "FRAPPE", None):
            api.discover()
        self.assertEqual(sorted(api.backups), ["20261127T120000Z_v1", "busy_v1"])

    def test_orphans_removed_only_while_the_sandbox_lock_is_free(self):
        calls = []

        async def docker(*args, **_):
            calls.append(args[0])
            return 0, b"rg-aaa\nrg-bbb\n", b""
        with mock.patch.object(guard, "STATE", self.d), mock.patch.object(guard, "docker", docker):
            held = guard._sandbox_lock()
            self.assertEqual(asyncio.run(guard.remove_orphans()), [])
            held.close()
            self.assertEqual(asyncio.run(guard.remove_orphans()), ["rg-aaa", "rg-bbb"])
        self.assertEqual(calls, ["ps", "rm"])

if __name__ == "__main__":
    unittest.main()


class Runs(unittest.TestCase):
    """POST /api/runs and /api/runs/reset: parameters, one run at a time, injected copies, reset."""
    M = {"sha256": "x", "schema_version": 1, "row_counts": {}, "invariants": {}, "created_at": "2026-11-27T12:00:00Z"}

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(api, "RESULTS", self.d / "results"), mock.patch.object(api, "DIRS", [self.d]),
                        mock.patch.object(api, "PRE_RUN", self.d / "results" / "pre-run"),
                        mock.patch.object(api, "FRAPPE_DIR", self.d / "frappe"), mock.patch.object(api, "FRAPPE", None)]
        for x in self.patches:
            x.start()
        api.backups, api.queue = {}, asyncio.Queue()
        self.dump = self.d / "20261127T120000Z_v1.dump"
        self.dump.write_bytes(b"PGDMP" + bytes(range(256)) * 40)
        self.dump.with_suffix(".json").write_text(json.dumps({**self.M, "file": self.dump.name}))
        b = guard.load_backup(self.dump)
        api.backups[b["id"]] = {**b, "status": "dr_ready", "cells": {"1.0.0": {"state": "native"}}}
        api._save(api.backups[b["id"]])

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def run_(self, **body):
        return asyncio.run(api.new_run(body))

    def code(self, **body):
        with self.assertRaises(HTTPException) as e:
            self.run_(**body)
        return e.exception.status_code, e.exception.detail

    def test_malformed_parameters_refused(self):
        for body in ({}, {"backups": "x"}, {"backups": [self.dump.stem], "heal": "no"},
                     {"backups": [self.dump.stem], "targets": "1.0.0"}, {"backups": [self.dump.stem], "fault": "rm -rf"}):
            self.assertEqual(self.code(**body)[0], 400, body)
        self.assertEqual(self.code(backups=["nope_v1"])[0], 404)
        self.assertIn("none of them picked", self.code(backups=[self.dump.stem], targets=["test.local"])[1])

    def test_one_run_at_a_time(self):
        api.backups["busy_v1"] = {"id": "busy_v1", "status": "running", "cells": {}}
        self.assertEqual(self.code(backups=[self.dump.stem])[0], 409)

    def test_rerun_with_targets_and_heal_off_keeps_the_previous_result(self):
        self.assertEqual(self.run_(backups=[self.dump.stem], targets=["2.0.0"], heal=False), {"queued": [self.dump.stem]})
        self.assertTrue((api.PRE_RUN / f"{self.dump.stem}.json").exists())
        seen = []

        async def cell(b, a, emit):
            seen.append(a)
            return {"app_version": a["version"], "state": "failed", "reason": "drift", "checks": [], "events": [],
                    "restored_into": {}, "signals": [], "static_scan": [], "bridges": []}

        async def go():
            task = asyncio.create_task(api.worker())
            while api.backups[self.dump.stem]["status"] in ("queued", "running"):
                await asyncio.sleep(0.01)
            task.cancel()
        with mock.patch.object(guard, "run_cell", cell), mock.patch.object(guard, "certificate",
                                                                            lambda *_: {"matrix": [{"status": "NOT RESTORABLE"}]}):
            asyncio.run(asyncio.wait_for(go(), 5))
        self.assertEqual([(a["version"], a["heal"]) for a in seen], [("2.0.0", False)])
        self.assertEqual(api.backups[self.dump.stem]["status"], "not_restorable")
        self.assertEqual(api.reset_runs(), {"restored": [self.dump.stem]})
        self.assertEqual(api.backups[self.dump.stem]["status"], "dr_ready")
        self.assertFalse(api.PRE_RUN.exists() and any(api.PRE_RUN.iterdir()))

    def test_injected_copy_is_a_new_resigned_backup_and_reset_removes_it(self):
        before = self.dump.read_bytes()
        stem = self.run_(backups=[self.dump.stem], fault="bitflip")["queued"][0]
        self.assertIn("-run-bitflip_v1", stem)
        copy = self.d / f"{stem}.dump"
        m = json.loads(copy.with_suffix(".json").read_text())
        self.assertEqual(m["sha256"], guard.sha256_file(copy))
        self.assertEqual(m["injected"], {"fault": "bitflip", "from": self.dump.stem})
        self.assertNotEqual(copy.read_bytes(), before)
        self.assertEqual(self.dump.read_bytes(), before)
        self.assertEqual(api.backups[stem]["run"]["targets"], ["1.0.0", "2.0.0"])
        self.assertEqual(self.code(backups=[self.dump.stem])[0], 409)  # the copy is queued
        api.backups[stem]["status"] = "not_restorable"
        api.reset_runs()
        self.assertNotIn(stem, api.backups)
        self.assertFalse(list(self.d.glob("*-run-*")))
        self.assertEqual(self.dump.read_bytes(), before)

    def test_fault_needing_a_rebuild_without_a_kit_copy_refused_cleanly(self):
        with mock.patch.object(api.faults, "HERE", self.d):
            status, detail = self.code(backups=[self.dump.stem], fault="poisoned")
        self.assertEqual(status, 409)
        self.assertIn("fault kit", detail)
        self.assertEqual(list(api.backups), [self.dump.stem])
        self.assertTrue(api.queue.empty())

    def test_heal_off_fails_a_drifted_cell_without_asking_the_model(self):
        cell = guard.Cell({"id": "b_v2", "manifest": {"schema_version": 2}}, {**GOOD, "heal": False}, lambda ev: None)

        async def probe(*_):
            return False, "column first_name does not exist"

        async def llm(*_):
            raise AssertionError("no model call with healing off")
        with mock.patch.object(guard, "_app_probe", probe), mock.patch.object(guard.airlock, "llm", llm):
            asyncio.run(guard._boot_and_bridge(cell, None))
        self.assertEqual(cell.state, "failed")
        self.assertIn("healing is off", cell.reason)
