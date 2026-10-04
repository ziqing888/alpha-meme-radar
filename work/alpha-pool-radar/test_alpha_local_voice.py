from __future__ import annotations

import alpha_local_voice as local_voice


def test_cached_local_voice_plays_without_model_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(local_voice, "VOICE_CACHE_DIR", tmp_path)
    message = "金狗来了，注意观察"
    asset = local_voice.voice_asset_path(message)
    asset.write_bytes(b"wav" + (b"0" * 2048))
    played = []

    monkeypatch.setattr(local_voice, "_play_wave", lambda path, repeat: played.append((path, repeat)) or {"ok": True})
    monkeypatch.setattr(local_voice, "synthesize_voice", lambda *args: (_ for _ in ()).throw(AssertionError("must use cache")))

    result = local_voice.play_voice_alert(message, repeat_count=3)

    assert result["ok"] is True
    assert result["backend"] == "kokoro_local"
    assert result["voice"] == "zf_001"
    assert result["repeat_count"] == 3
    assert played == [(asset, 3)]


def test_missing_local_voice_uses_system_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(local_voice, "VOICE_CACHE_DIR", tmp_path)
    fallback_calls = []
    monkeypatch.setattr(local_voice, "_play_system_voice", lambda message, repeat: fallback_calls.append((message, repeat)) or {"ok": True})

    result = local_voice.play_voice_alert("金狗来了，发现新机会。", repeat_count=2)

    assert result["ok"] is True
    assert fallback_calls == [("金狗来了，发现新机会。", 2)]
