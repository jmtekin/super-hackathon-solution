"""End-to-end: the running restoreguard.service proves real backups dropped into its watched folder.

Everything goes through the service's own API, the way the UI sees it; nothing here calls guard.py's cells
directly. All backups are dropped at once at the start because the runtime serialises sandboxes box-wide
anyway, so the suite costs the sum of the cells (~3-4 min) and never runs two sandboxes. Run via ../e2e.sh,
which resets the e2e copies before and after.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rgctl  # noqa: E402

MARK = "e2e"
POLL_WAIT = 60  # the watcher polls every RESTOREGUARD_POLL s
FAULTS = {  # kind: (status, text in every cell's reason)
    "corrupted": ("not_restorable", "dump unreadable"),
    "truncated": ("not_restorable", "end of file"),
    "stale": ("not_restorable", "checksum mismatch"),
    "rows-deleted": ("not_restorable", "row counts differ"),
    "poisoned": ("quarantined", "containment: network"),
}


def http(path, method="GET"):
    """(status code, parsed JSON or text) without raising on 4xx."""
    try:
        with urllib.request.urlopen(urllib.request.Request(rgctl.URL + path, method=method), timeout=10) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if r.headers.get_content_type() == "application/json" else body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture(scope="session")
def events():
    """Every SSE event from before the first drop to the end of the session."""
    got, ready = [], threading.Event()

    def read():
        with urllib.request.urlopen(rgctl.URL + "/api/stream", timeout=60) as r:
            ready.set()
            for line in r:
                if line.startswith(b"data: "):
                    got.append(json.loads(line[6:]))
    threading.Thread(target=read, daemon=True).start()
    assert ready.wait(10), "SSE stream did not open"
    return got


@pytest.fixture(scope="session")
def ids(events):
    ids = {k: rgctl.drop(k, MARK) for k in ["good", "drift", *FAULTS]}
    ids["no-fields"] = rgctl.drop("good", MARK, lambda m: {"file": m["file"]}, label="no-fields")
    ids["garbage"] = rgctl.drop("good", MARK, lambda m: "{not json", label="garbage")
    return ids


@pytest.fixture(scope="session")
def done(ids):
    """Waits for every dropped backup; the deadline covers the whole serial queue."""
    deadline = time.monotonic() + 900
    return {k: rgctl.wait(bid, max(1, deadline - time.monotonic())) for k, bid in ids.items()}


def test_new_backup_is_listed(ids):
    t0 = time.monotonic()
    while time.monotonic() - t0 < POLL_WAIT:
        listed = {b["id"]: b for b in rgctl.api("/api/backups")}
        if ids["good"] in listed:
            break
        time.sleep(0.5)
    b = listed[ids["good"]]
    assert b["status"] in ("queued", "running", "dr_ready")
    manifest = json.loads((rgctl.DROP / f"{ids['good']}.json").read_text())
    assert b["sha256"] == manifest["sha256"] and b["schema_version"] == manifest["schema_version"]


def test_good_backup_is_dr_ready_in_test_instances(done):
    b = done["good"]
    assert b["status"] == "dr_ready", b["reason"]
    assert {v: c["state"] for v, c in b["cells"].items()} == {"1.0.0": "native", "2.0.0": "native"}
    assert b["checks_total"] > 20 and b["checks_passed"] == b["checks_total"]
    for c in b["cells"].values():
        into = c["restored_into"]
        assert into["instance"].startswith("test instance rg-")
        assert into["dsn"].startswith("postgresql://postgres@127.0.0.1:")
        assert "network none" in into["database"]


def test_sse_events_arrive_in_order(done, ids, events):
    bid = ids["good"]
    mine = [e for e in events if e.get("backup") == bid or e.get("id") == bid]
    statuses = [e["status"] for e in mine if e["type"] == "backup"]
    assert statuses == ["queued", "running", "dr_ready"], statuses
    assert mine[-1] == {"type": "backup", "id": bid, "status": "dr_ready", "reason": done["good"]["reason"]}
    cells = [e["app_version"] for e in mine if "app_version" in e]
    assert cells == sorted(cells), "cells interleaved: the runtime ran two sandboxes at once"
    for v in ("1.0.0", "2.0.0"):
        cell = [e for e in mine if e.get("app_version") == v]
        kinds = [e["type"] for e in cell]
        assert cell[0] == {**cell[0], "type": "check", "check": "sha256 matches manifest", "pass": True}
        assert kinds[-1] == "verdict" and cell[-1]["state"] == "native"
        started = [e for e in cell if e["type"] == "sandbox" and e["phase"] == "started"]
        destroyed = [e for e in cell if e["type"] == "sandbox" and e["phase"] == "destroyed"]
        assert [e["name"] for e in started] == [e["name"] for e in destroyed]
        assert done["good"]["cells"][v]["restored_into"]["instance"] == f"test instance {started[0]['name']}"
        assert kinds.index("sandbox") < kinds.index("verdict") and cell[-2]["type"] == "sandbox"
        assert [e["t"] for e in cell] == sorted(e["t"] for e in cell)
        assert all(e["pass"] for e in cell if e["type"] == "check")


def test_certificate_is_valid_and_sealed(done, ids):
    code, cert = http(f"/api/backups/{ids['good']}/certificate.json")
    assert code == 200
    proof = cert.pop("proof_sha256")
    assert proof == hashlib.sha256(json.dumps(cert, sort_keys=True).encode()).hexdigest()
    row, = cert["matrix"]
    assert cert["kind"] == "BackupAirlock DR Readiness Certificate" and row["status"] == "DR READY"
    assert row["sha256"] == done["good"]["manifest"]["sha256"] == cert["backup_sha256"][row["backup"]]
    assert [a["result"] for a in row["app_versions"]] == ["native", "native"]
    assert all(c["pass"] for a in row["app_versions"] for c in a["control_plane_checks"])
    assert http(f"/api/backups/{ids['good']}/incident.json")[0] == 409


def test_schema_drift_healed_by_bridge(done, ids):
    b = done["drift"]
    assert b["status"] == "dr_ready", b["reason"]
    old, new = b["cells"]["1.0.0"], b["cells"]["2.0.0"]
    assert old["state"] == "healed" and new["state"] == "native"
    assert old["llm_calls"] and all(c["pass"] for c in old["checks"][-4:])
    code, sql = http(f"/api/backups/{ids['drift']}/bridges/{old['bridges'][-1]['name']}")
    assert code == 200 and "customer" in sql.lower()
    assert http(f"/api/backups/{ids['drift']}/bridges/nope.sql")[0] == 404
    _, cert = http(f"/api/backups/{ids['drift']}/certificate.json")
    assert cert["matrix"][0]["app_versions"][0]["bridge_sha256"] == old["bridges"][-1]["sha256"]


@pytest.mark.parametrize("kind", FAULTS)
def test_fault_ends_in_the_right_status(done, ids, kind):
    status, why = FAULTS[kind]
    b = done[kind]
    assert b["status"] == status, b["reason"]
    assert all(why in c["reason"] for c in b["cells"].values()), b["reason"]
    assert all(c["restored_into"]["dsn"].startswith("postgresql://postgres@127.0.0.1:") for c in b["cells"].values())
    code, _ = http(f"/api/backups/{ids[kind]}/incident.json")
    assert code == (200 if status == "quarantined" else 409)


def test_poisoned_incident_names_what_was_stopped(done, ids):
    _, inc = http(f"/api/backups/{ids['poisoned']}/incident.json")
    assert inc["status"] == "quarantined" and len(inc["cells"]) == 2
    for c in inc["cells"]:
        assert {"network", "rootfs"} <= set(c["signals"]) and c["static_scan"]
        assert c["containment_events"]
    _, cert = http(f"/api/backups/{ids['poisoned']}/certificate.json")
    assert cert["matrix"][0]["status"].startswith("QUARANTINED")


@pytest.mark.parametrize("kind,why", [("no-fields", "manifest lacks"), ("garbage", "manifest unreadable")])
def test_malformed_manifest_fails_without_a_sandbox(done, ids, kind, why):
    b = done[kind]
    assert b["status"] == "failed" and why in b["reason"] and b["cells"] == {}
    assert http(f"/api/backups/{ids[kind]}/rerun", "POST")[0] == 409
    assert http(f"/api/backups/{ids[kind]}/certificate.json")[0] == 409


def test_matrix_has_every_dropped_backup(done, ids):
    m = rgctl.api("/api/matrix")
    state = {r["id"]: {c["app_version"]: c["state"] for c in r["cells"]} for r in m["backups"]}
    assert {"1.0.0", "2.0.0"} <= set(m["app_versions"])  # other targets (Frappe) may add columns
    assert state[ids["drift"]] == {"1.0.0": "healed", "2.0.0": "native"}
    assert state[ids["garbage"]] == {"1.0.0": "not_run", "2.0.0": "not_run"}


def test_rerun_after_the_manifest_is_fixed(done, ids):
    """A manifest read mid-write is not stuck: once it is whole, rerun proves the backup."""
    bid = ids["garbage"]
    good = json.loads((rgctl.DROP / f"{ids['good']}.json").read_text())
    (rgctl.DROP / f"{bid}.json").write_text(json.dumps(dict(good, file=f"{bid}.dump")))
    assert http(f"/api/backups/{bid}/rerun", "POST") == (200, {"queued": bid})
    assert rgctl.wait(bid, 300)["status"] == "dr_ready"


def test_unknown_ids_are_404():
    for path, method in [("/api/backups/nope_v1", "GET"), ("/api/backups/nope_v1/rerun", "POST"),
                         ("/api/backups/nope_v1/certificate.json", "GET")]:
        assert http(path, method)[0] == 404


def test_rerun_proves_the_backup_again(done, ids):
    bid, before = ids["good"], done["good"]
    code, body = http(f"/api/backups/{bid}/rerun", "POST")
    assert (code, body) == (200, {"queued": bid})
    assert http(f"/api/backups/{bid}/rerun", "POST")[0] == 409
    assert http(f"/api/backups/{bid}/certificate.json")[0] == 409
    after = rgctl.wait(bid, 300)
    assert after["status"] == "dr_ready" and after["started_at"] > before["finished_at"]
    assert {c["restored_into"]["instance"] for c in after["cells"].values()}.isdisjoint(
        c["restored_into"]["instance"] for c in before["cells"].values())


@pytest.mark.parametrize("entry", [{"dsn": "postgresql://erp:pw@prod-db:5432/nordvik"},
                                   {"image": "nordvik:2.0.0 host=prod-db"}])
def test_service_refuses_to_start_with_a_prod_target(tmp_path, entry):
    """A second copy of the service with an apps.json pointing at production must exit before it listens."""
    rg = tmp_path / "restoreguard"
    shutil.copytree(rgctl.RG, rg, ignore=shutil.ignore_patterns("backups", "state", "faults", "standin", "__pycache__"))
    apps = json.loads((rg / "apps.json").read_text())
    apps[1].update(entry)
    (rg / "apps.json").write_text(json.dumps(apps))
    (tmp_path / "empty").mkdir()
    env = dict(os.environ, RESTORER_DIR=str(rgctl.RG.parent / "restorer"), RESTOREGUARD_PORT="8699",
               RESTOREGUARD_BACKUPS=str(tmp_path / "empty"), RESTOREGUARD_STATE=str(tmp_path / "state"))
    p = subprocess.run([sys.executable, "api.py"], cwd=rg, env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "refused" in p.stderr, p.stderr[-500:]
    with pytest.raises(OSError):
        urllib.request.urlopen("http://127.0.0.1:8699/health", timeout=2)


def test_zz_no_sandbox_left_behind(done):
    left = subprocess.run(["sudo", "-n", "docker", "ps", "-a", "--filter", "name=^rg-", "--format", "{{.Names}}"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert left == []
