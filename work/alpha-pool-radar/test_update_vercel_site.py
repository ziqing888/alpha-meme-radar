import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("update_vercel_site.py")
SPEC = importlib.util.spec_from_file_location("update_vercel_site", MODULE_PATH)
updater = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = updater
SPEC.loader.exec_module(updater)


def test_update_site_uses_wider_holder_scan_and_chinese_status(monkeypatch, tmp_path):
    steps = []
    out_dir = tmp_path / "outputs"
    site_dir = out_dir / "vercel-site"

    monkeypatch.setattr(updater, "OUT_DIR", out_dir)
    monkeypatch.setattr(updater, "SITE_DIR", site_dir)
    monkeypatch.setattr(updater, "STATUS_PATH", out_dir / "alpha-radar-vercel-update-status.json")

    def fake_run_step(name, command, cwd):
        steps.append((name, command))
        if name == "\u751f\u6210\u4e2d\u6587\u7f51\u9875":
            site_dir.mkdir(parents=True)
            (site_dir / "index.html").write_text("ok", encoding="utf-8")
            (site_dir / "report.json").write_text("{}", encoding="utf-8")
            (site_dir / "vercel.json").write_text("{}", encoding="utf-8")
        return {"name": name, "ok": True, "returncode": 0}

    monkeypatch.setattr(updater, "run_step", fake_run_step)

    assert updater.update_site(deploy=False) == 0

    report_cmd = steps[0][1]
    assert report_cmd[report_cmd.index("--holder-top-tokens") + 1] == "30"
    assert report_cmd[report_cmd.index("--risk-top-tokens") + 1] == "15"
    assert [name for name, _ in steps] == ["\u5237\u65b0\u96f7\u8fbe\u6570\u636e", "\u751f\u6210\u4e2d\u6587\u7f51\u9875"]

    status = json.loads((out_dir / "alpha-radar-vercel-update-status.json").read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["steps"][0]["name"] == "\u5237\u65b0\u96f7\u8fbe\u6570\u636e"


def test_update_site_skips_vercel_deploy_when_recent_and_no_new_gold_alert(monkeypatch, tmp_path):
    steps = []
    out_dir = tmp_path / "outputs"
    site_dir = out_dir / "vercel-site"
    status_path = out_dir / "alpha-radar-vercel-update-status.json"
    gold_state_path = out_dir / "alpha-gold-watch-state.json"

    out_dir.mkdir()
    status_path.write_text(
        json.dumps({"ok": True, "last_deployed_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    gold_state_path.write_text(json.dumps({"alerts": [{"symbol": "DOG"}]}), encoding="utf-8")

    monkeypatch.setattr(updater, "OUT_DIR", out_dir)
    monkeypatch.setattr(updater, "SITE_DIR", site_dir)
    monkeypatch.setattr(updater, "STATUS_PATH", status_path)
    monkeypatch.setattr(updater, "GOLD_STATE_PATH", gold_state_path)

    def fake_run_step(name, command, cwd):
        steps.append((name, command))
        if name == "\u751f\u6210\u4e2d\u6587\u7f51\u9875":
            site_dir.mkdir(parents=True)
            (site_dir / "index.html").write_text("ok", encoding="utf-8")
            (site_dir / "report.json").write_text("{}", encoding="utf-8")
            (site_dir / "vercel.json").write_text("{}", encoding="utf-8")
        return {"name": name, "ok": True, "returncode": 0}

    monkeypatch.setattr(updater, "run_step", fake_run_step)

    assert updater.update_site(deploy=True, deploy_min_interval_seconds=900) == 0

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["deploy_skipped"] is True
    assert [name for name, _ in steps] == ["\u5237\u65b0\u96f7\u8fbe\u6570\u636e", "\u751f\u6210\u4e2d\u6587\u7f51\u9875"]
