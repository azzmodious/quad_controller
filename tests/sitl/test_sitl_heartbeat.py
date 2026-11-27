"""Runs only against a live SITL. CI starts one with scripts/sitl.sh; locally:

./scripts/sitl.sh --background && QC_SITL=1 pytest -m sitl
"""

import os

import pytest

from quad_controller.cli import main

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.skipif(os.environ.get("QC_SITL") != "1", reason="set QC_SITL=1 with SITL running"),
]


def test_heartbeat_within_5s(tmp_path, monkeypatch):
    monkeypatch.setenv("QC_CONFIG", str(_cfg(tmp_path)))
    assert main(["check", "--target", "sitl", "--timeout", "5"]) == 0


def test_run_sees_mode_and_battery(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("QC_CONFIG", str(_cfg(tmp_path)))
    assert main(["run", "--target", "sitl", "--duration", "6"]) == 0
    evs = [
        json.loads(x)
        for x in (tmp_path / "logs" / "quad_controller.jsonl").read_text().split("\n")
        if x
    ]
    status = [e for e in evs if e["event"] == "status"]
    assert status, "no status lines logged"
    assert status[-1]["mode"] == "STABILIZE"
    assert status[-1]["voltage_v"] and status[-1]["voltage_v"] > 10


def _cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"logging:\n  dir: {tmp_path / 'logs'}\n  console: false\n")
    return p
