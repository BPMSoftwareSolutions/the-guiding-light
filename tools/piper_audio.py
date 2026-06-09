from __future__ import annotations

import io
import shutil
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOICE_NAME = "en_US-lessac-medium"
DEFAULT_VOICE_DIR = REPO_ROOT / ".piper-voices"


def _missing_voice_message(voice_name: str, voices_dir: Path) -> str:
    return (
        "Piper voice files are missing.\n"
        f"Expected model: {voices_dir / f'{voice_name}.onnx'}\n"
        f"Expected config: {voices_dir / f'{voice_name}.onnx.json'}\n"
        "Download them with:\n"
        f"  python -m piper.download_voices {voice_name} --data-dir {voices_dir}\n"
        "This app will not fall back to OpenAI."
    )


@lru_cache(maxsize=8)
def load_voice(
    voice_name: str = DEFAULT_VOICE_NAME,
    voices_dir: str | Path = DEFAULT_VOICE_DIR,
):
    try:
        from piper import PiperVoice
    except ImportError as exc:  # pragma: no cover - startup guard
        raise RuntimeError(
            "Piper is not installed. Install it with: pip install piper-tts"
        ) from exc

    voices_dir = Path(voices_dir)
    model_path = voices_dir / f"{voice_name}.onnx"
    config_path = voices_dir / f"{voice_name}.onnx.json"
    if not model_path.exists() or not config_path.exists():
        raise FileNotFoundError(_missing_voice_message(voice_name, voices_dir))

    return PiperVoice.load(model_path, config_path=config_path)


def synthesize_wav_bytes(voice, text: str, *, speed: float = 1.0) -> bytes:
    from piper.config import SynthesisConfig

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        voice.synthesize_wav(
            text,
            wav_file,
            syn_config=SynthesisConfig(length_scale=speed),
        )
    return buffer.getvalue()


def wav_to_mp3_bytes(wav_bytes: bytes) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is required to stream Piper audio to the browser as MP3."
        )

    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "pipe:1",
        ],
        input=wav_bytes,
        capture_output=True,
        check=True,
    )
    return proc.stdout
