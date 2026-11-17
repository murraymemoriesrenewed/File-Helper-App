"""Run with:  pip install -r requirements.txt pytest httpx  &&  pytest -q"""
import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

KEY = {"X-Api-Key": "test-key"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARE_ROOT", str(tmp_path))
    monkeypatch.setenv("HELPER_API_KEY", "test-key")
    monkeypatch.setenv("DISPLAY_ROOT", "Z:\\")
    import main
    importlib.reload(main)
    for d in main.STANDARD_DIRS:
        (tmp_path / d).mkdir()
    return TestClient(main.app), tmp_path, main


def create(client, **kw):
    body = {"order_date": "2026-10-21", "order_number": "21816", "last_name": "Ballenger",
            "first_name": "Noella", "description": "Slides"}
    body.update(kw)
    return client.post("/projects", json=body, headers=KEY)


def test_health_and_auth(env):
    client, root, _ = env
    assert client.get("/health").json()["ok"] is True
    assert client.get("/stages").status_code == 401
    assert client.get("/stages", headers={"X-Api-Key": "wrong"}).status_code == 401


def test_create_matches_spec(env):
    client, root, _ = env
    r = create(client)
    assert r.status_code == 200, r.text
    j = r.json()
    name = "2026-10-21 - 21816 - Ballenger, Noella - Slides"
    assert j["folder_name"] == name
    assert j["path"] == f"Z:\\_Stage 01 - To Be Digitized\\{name}"
    base = root / "_Stage 01 - To Be Digitized" / name
    assert sorted(p.name for p in base.iterdir()) == ["_FINAL", "_RAW", "_TRACKER"]
    stub = base / "_FINAL" / ".21816 - Ballenger"
    assert stub.is_file() and stub.stat().st_size == 0
    # calling again is safe (NocoBase retry)
    r2 = create(client)
    assert r2.status_code == 200 and r2.json()["created"] is False
    # same order number with a different description -> refuse duplicate
    assert create(client, description="Photos").status_code == 409


def test_create_sanitizes_and_dates(env):
    client, root, _ = env
    r = create(client, order_number=21817, last_name="O'Brien", first_name="Mary Ann",
               description='VHS / 8mm: "Wedding"?', order_date="2026-10-21T04:30:00.000Z", stage="3")
    j = r.json()
    # 04:30 UTC is still Oct 20 in Chicago
    assert j["folder_name"] == "2026-10-20 - 21817 - O'Brien, Mary Ann - VHS - 8mm Wedding"
    assert j["stage"] == "_Stage 03 - Assigned For Editing"
    assert (root / j["relative_path"]).is_dir()
    assert create(client, order_number=1, order_date="10/21/2026").status_code == 400
    assert create(client, order_number=2, last_name="  ").status_code == 400
    assert create(client, order_number=3, stage="Stage 99").status_code == 400


def test_move(env):
    client, root, _ = env
    name = create(client).json()["folder_name"]
    (root / "_Stage 01 - To Be Digitized" / name / "_RAW" / "scan1.jpg").write_bytes(b"x" * 10)
    r = client.post("/projects/move", headers=KEY,
                    json={"folder_name": name, "from_stage": "1", "to_stage": "_Stage 02 - Digitized"})
    assert r.status_code == 200, r.text
    assert (root / "_Stage 02 - Digitized" / name / "_RAW" / "scan1.jpg").exists()
    assert not (root / "_Stage 01 - To Be Digitized" / name).exists()
    # move by order number
    r = client.post("/projects/move", headers=KEY,
                    json={"order_number": "21816", "from_stage": "02", "to_stage": "outbox"})
    assert r.json()["path"] == f"Z:\\__Outbox - Done\\{name}"
    # wrong source stage -> 404
    r = client.post("/projects/move", headers=KEY,
                    json={"folder_name": name, "from_stage": "1", "to_stage": "2"})
    assert r.status_code == 404


def test_standard_folders_are_protected(env):
    client, root, main = env
    for bad in ["_Stage 02 - Digitized", "..", "../_Stage 02 - Digitized", "x/../../etc"]:
        r = client.post("/projects/move", headers=KEY,
                        json={"folder_name": bad, "from_stage": "1", "to_stage": "2"})
        assert r.status_code in (400, 404), bad
    assert all((root / d).is_dir() for d in main.STANDARD_DIRS)
    # destination collision is refused
    name = create(client).json()["folder_name"]
    (root / "_Stage 02 - Digitized" / name).mkdir()
    r = client.post("/projects/move", headers=KEY,
                    json={"folder_name": name, "from_stage": "1", "to_stage": "2"})
    assert r.status_code == 409


def test_properties_and_list(env):
    client, root, _ = env
    rel = create(client).json()["relative_path"]
    raw = root / rel / "_RAW"
    (raw / "a.jpg").write_bytes(b"a" * 1000)
    (raw / "sub").mkdir()
    (raw / "sub" / "b.jpg").write_bytes(b"b" * 2000)

    p = client.get("/properties", params={"path": "Z:\\" + rel.replace("/", "\\")}, headers=KEY).json()
    assert p["file_count"] == 3            # 2 jpgs + stub
    assert p["total_bytes"] == 3000
    raw_info = next(s for s in p["subfolders"] if s["name"] == "_RAW")
    assert raw_info["file_count"] == 2 and raw_info["total_bytes"] == 3000

    l = client.post("/list", json={"path": rel + "/_RAW"}, headers=KEY).json()
    assert [e["name"] for e in l["entries"]] == ["a.jpg", "sub"]
    assert l["entries"][0]["size_bytes"] == 1000 and l["entries"][0]["modified"]
    lr = client.get("/list", params={"path": rel, "recursive": "true"}, headers=KEY).json()
    assert lr["file_count"] == 3

    for bad in ["../", "/etc", "C:\\Windows", "Z:\\..\\x"]:
        assert client.get("/list", params={"path": bad}, headers=KEY).status_code in (400, 404), bad
