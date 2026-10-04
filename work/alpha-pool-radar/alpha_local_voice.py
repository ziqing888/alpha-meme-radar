"""Local, cached Kokoro voice alerts with a Windows speech fallback."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


VOICE_CACHE_DIR = Path(
    os.environ.get("ALPHA_LOCAL_VOICE_CACHE", str(Path.home() / ".cache" / "alpha-radar-voice"))
)
VOICE_REPO_ID = os.environ.get("ALPHA_LOCAL_VOICE_MODEL", "hexgrad/Kokoro-82M-v1.1-zh")
VOICE_NAME = os.environ.get("ALPHA_LOCAL_VOICE_NAME", "zf_001")
SAMPLE_RATE = 24000
_KOKORO_PIPELINE = None


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt" or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def voice_asset_path(message: str) -> Path:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return VOICE_CACHE_DIR / f"kokoro-{VOICE_NAME}-{digest}.wav"


def synthesize_voice(message: str, output_path: Path | None = None) -> Path:
    """Generate one cached Chinese alert using the female Kokoro voice."""
    import soundfile as sf
    import torch
    from kokoro import KModel, KPipeline

    destination = output_path or voice_asset_path(message)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 1024:
        return destination

    global _KOKORO_PIPELINE
    if _KOKORO_PIPELINE is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = KModel(repo_id=VOICE_REPO_ID).to(device).eval()
        _KOKORO_PIPELINE = KPipeline(lang_code="z", repo_id=VOICE_REPO_ID, model=model, device=device)
    pipeline = _KOKORO_PIPELINE
    generator = pipeline(message, voice=VOICE_NAME, speed=0.96, split_pattern=r"\n+")
    result = next(generator)
    sf.write(destination, result.audio, SAMPLE_RATE)
    return destination


def _play_wave(path: Path, repeat_count: int = 1) -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "reason": "local_voice_windows_only"}
    safe_path = str(path).replace("'", "''")
    safe_repeat = max(1, min(10, int(repeat_count)))
    command = (
        "Add-Type -AssemblyName System; "
        f"$player = New-Object System.Media.SoundPlayer('{safe_path}'); "
        f"for ($i = 0; $i -lt {safe_repeat}; $i++) {{ "
        "$player.PlaySync(); "
        f"if ($i -lt {safe_repeat - 1}) {{ Start-Sleep -Milliseconds 450 }} "
        "}; $player.Dispose()"
    )
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_hidden_subprocess_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "backend": "kokoro_local", "voice": VOICE_NAME, "path": str(path), "voice_file": path.name}


def _play_system_voice(message: str, repeat_count: int) -> dict[str, Any]:
    """Keep the existing Windows voice as a graceful fallback."""
    safe_message = message.replace("'", "''")
    safe_repeat = max(1, min(10, int(repeat_count)))
    command = (
        "Add-Type -AssemblyName System.Speech; "
        "$voice = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"for ($i = 0; $i -lt {safe_repeat}; $i++) {{ "
        f"$voice.Speak('{safe_message}'); "
        f"if ($i -lt {safe_repeat - 1}) {{ Start-Sleep -Milliseconds 450 }} "
        "}"
    )
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_hidden_subprocess_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)}
    return {"ok": True, "backend": "windows_system_speech", "message": message, "repeat_count": safe_repeat}


def play_voice_alert(
    message: str,
    repeat_count: int = 1,
    *,
    allow_generate: bool = False,
) -> dict[str, Any]:
    """Play cached local voice; generation is opt-in to keep refresh loops fast."""
    try:
        asset = voice_asset_path(message)
        if not asset.exists() and allow_generate:
            asset = synthesize_voice(message, asset)
        if asset.exists() and asset.stat().st_size > 1024:
            result = _play_wave(asset, repeat_count)
            if result.get("ok"):
                return {
                    "ok": True,
                    "backend": "kokoro_local",
                    "voice": VOICE_NAME,
                    "path": str(asset),
                    "voice_file": asset.name,
                    "repeat_count": max(1, min(10, int(repeat_count))),
                }
    except Exception:
        pass
    return _play_system_voice(message, repeat_count)
