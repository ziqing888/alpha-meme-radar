import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_live_refresh.py")
SPEC = importlib.util.spec_from_file_location("alpha_live_refresh", MODULE_PATH)
live_refresh = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = live_refresh
SPEC.loader.exec_module(live_refresh)


def test_refresh_command_defaults_to_data_refresh_without_deploy():
    command = live_refresh.refresh_command("python", Path("work/alpha-pool-radar"), deploy=False)

    assert command[:2] == ["python", str(Path("work/alpha-pool-radar") / "alpha_radar_report.py")]
    assert command[2:] == ["--out-dir", str(live_refresh.ROOT / "outputs")]
    assert "--deploy" not in command


def test_refresh_command_uses_legacy_deploy_only_when_requested():
    command = live_refresh.refresh_command("python", Path("work/alpha-pool-radar"), deploy=True)

    assert command == ["python", str(Path("work/alpha-pool-radar") / "update_vercel_site.py"), "--deploy"]


def test_subprocess_runner_marks_full_refresh_as_meme_cache_only(monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0
        stdout = "done"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(live_refresh.subprocess, "run", fake_run)

    result = live_refresh.subprocess_runner(["python", "update_vercel_site.py"], tmp_path)

    assert result["ok"] is True
    assert captured["command"] == ["python", "update_vercel_site.py"]
    assert captured["env"]["ALPHA_MEME_CACHE_ONLY"] == "1"
    assert captured["env"]["ALPHA_MEME_CHAINS"] == "*"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_run_refresh_once_writes_live_status(tmp_path):
    calls = []
    status_path = tmp_path / "alpha-live-refresh-status.json"

    def fake_runner(command, cwd):
        calls.append((command, cwd))
        return {"ok": True, "returncode": 0, "output_tail": "done"}

    result = live_refresh.run_refresh_once(
        ["python", "update_vercel_site.py"],
        tmp_path,
        status_path,
        runner=fake_runner,
    )

    assert result["ok"] is True
    assert calls == [(["python", "update_vercel_site.py"], tmp_path)]
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["ok"] is True
    assert status["command"] == ["python", "update_vercel_site.py"]


def test_run_refresh_once_skips_cloud_upload_without_explicit_allow(tmp_path):
    status_path = tmp_path / "alpha-live-refresh-status.json"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    uploads = []

    def fake_runner(command, cwd):
        return {"ok": True, "returncode": 0, "output_tail": "done"}

    def fake_cloud_upload(report, site_url, token, status):
        uploads.append((report, site_url, token, status))
        return {"ok": True, "cloud": True}

    result = live_refresh.run_refresh_once(
        ["python", "update_vercel_site.py"],
        tmp_path,
        status_path,
        runner=fake_runner,
        cloud_upload=True,
        report_path=report_path,
        cloud_site_url="https://example.vercel.app",
        cloud_token="secret",
        cloud_status_path=tmp_path / "alpha-cloud-report-status.json",
        cloud_uploader=fake_cloud_upload,
    )

    assert result["ok"] is True
    assert "cloud_upload" not in result
    assert uploads == []


def test_run_refresh_once_can_upload_report_to_cloud_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv(live_refresh.ALLOW_CLOUD_UPLOAD_ENV, "1")
    status_path = tmp_path / "alpha-live-refresh-status.json"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    uploads = []

    def fake_runner(command, cwd):
        return {"ok": True, "returncode": 0, "output_tail": "done"}

    def fake_cloud_upload(report, site_url, token, status):
        uploads.append((report, site_url, token, status))
        return {"ok": True, "cloud": True}

    result = live_refresh.run_refresh_once(
        ["python", "update_vercel_site.py"],
        tmp_path,
        status_path,
        runner=fake_runner,
        cloud_upload=True,
        report_path=report_path,
        cloud_site_url="https://example.vercel.app",
        cloud_token="secret",
        cloud_status_path=tmp_path / "alpha-cloud-report-status.json",
        cloud_uploader=fake_cloud_upload,
    )

    assert result["ok"] is True
    assert result["cloud_upload"]["ok"] is True
    assert uploads == [(report_path, "https://example.vercel.app", "secret", tmp_path / "alpha-cloud-report-status.json")]


def test_run_refresh_once_uses_local_env_token_for_cloud_upload(tmp_path, monkeypatch):
    monkeypatch.setenv(live_refresh.ALLOW_CLOUD_UPLOAD_ENV, "1")
    status_path = tmp_path / "alpha-live-refresh-status.json"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    env_file = tmp_path / ".env.local"
    env_file.write_text("ALPHA_REPORT_WRITE_TOKEN=from_env_file\n", encoding="utf-8")
    uploads = []

    def fake_runner(command, cwd):
        return {"ok": True, "returncode": 0, "output_tail": "done"}

    def fake_cloud_upload(report, site_url, token, status):
        uploads.append(token)
        return {"ok": True}

    monkeypatch.setattr(live_refresh.alpha_cloud_report, "ENV_FILES", [env_file])

    result = live_refresh.run_refresh_once(
        ["python", "update_vercel_site.py"],
        tmp_path,
        status_path,
        runner=fake_runner,
        cloud_upload=True,
        report_path=report_path,
        cloud_site_url="https://example.vercel.app",
        cloud_token="",
        cloud_status_path=tmp_path / "alpha-cloud-report-status.json",
        cloud_uploader=fake_cloud_upload,
    )

    assert result["ok"] is True
    assert uploads == ["from_env_file"]
