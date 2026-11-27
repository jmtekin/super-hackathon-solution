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
