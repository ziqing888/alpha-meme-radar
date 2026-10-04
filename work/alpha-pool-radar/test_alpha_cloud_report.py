import importlib.util
import gzip
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("alpha_cloud_report.py")
SPEC = importlib.util.spec_from_file_location("alpha_cloud_report", MODULE_PATH)
cloud_report = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = cloud_report
SPEC.loader.exec_module(cloud_report)


def test_cloud_report_upload_uses_fixed_api_endpoint(tmp_path):
    report = tmp_path / "report.json"
    status = tmp_path / "cloud-status.json"
    report.write_text(json.dumps({"meta": {"ok": True}}), encoding="utf-8")
    calls = []

    def fake_uploader(url, token, payload):
        calls.append((url, token, payload))
        return {"ok": True, "uploaded": True}

    result = cloud_report.upload_report(
        report,
        "https://example.vercel.app",
        "secret",
        status,
        uploader=fake_uploader,
    )

    assert result["ok"] is True
    assert calls[0][0] == "https://example.vercel.app/api/report"
    assert calls[0][1] == "secret"
    assert json.loads(calls[0][2])["meta"]["ok"] is True
    assert json.loads(status.read_text(encoding="utf-8"))["ok"] is True


def test_cloud_report_refuses_missing_token(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")

    result = cloud_report.upload_report(report, "https://example.vercel.app", "", tmp_path / "status.json")

    assert result["ok"] is False
    assert result["step"] == "config"


def test_load_env_file_reads_local_tokens(tmp_path):
    env_file = tmp_path / ".env.local"
    env_file.write_text("ALPHA_REPORT_WRITE_TOKEN=\"abc123\"\nBLOB_READ_WRITE_TOKEN=blob_token\n", encoding="utf-8")

    values = cloud_report.load_env_file(env_file)

    assert values["ALPHA_REPORT_WRITE_TOKEN"] == "abc123"
    assert values["BLOB_READ_WRITE_TOKEN"] == "blob_token"


def test_http_uploader_compresses_report(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(cloud_report.urllib.request, "urlopen", fake_urlopen)
    result = cloud_report.http_uploader(
        "https://example.test/api/report",
        "secret",
        '{"payload":"' + ("x" * 1000) + '"}',
    )

    assert result == {"ok": True}
    request = captured["request"]
    assert request.get_header("Content-encoding") == "gzip"
    body = json.loads(gzip.decompress(request.data).decode("utf-8"))
    assert body["payload"] == "x" * 1000
