#!/usr/bin/env python
"""Local streaming-TTS server for the Doc-to-Audio page.

This app supports two explicit backends:
    - Piper local TTS
    - OpenAI cloud TTS

There is no silent fallback. The browser tells the server which backend to use,
and the server either serves that backend or returns a loud, actionable error.

Endpoints
    GET  /                      -> serves src/doc-to-audio.html
    POST /api/prepare           -> {text,title,backend,voice,speed,quality} -> {token,...}
    GET  /api/stream/<token>    -> audio/mpeg, streamed progressively
    GET  /api/download/<token>  -> the fully-rendered MP3 (attachment), once ready
    GET  /healthz               -> backend availability summary
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from secrets import token_urlsafe

from flask import Flask, Response, abort, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).resolve().parent))
from piper_audio import (  # noqa: E402  (sibling module)
    DEFAULT_VOICE_DIR as DEFAULT_PIPER_VOICE_DIR,
    DEFAULT_VOICE_NAME as DEFAULT_PIPER_VOICE_NAME,
    load_voice,
    synthesize_wav_bytes,
    wav_to_mp3_bytes,
)
from prose_chunking import to_speech_chunks  # noqa: E402  (sibling module)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = REPO_ROOT / "src" / "doc-to-audio.html"
DEFAULT_CACHE_DIR = REPO_ROOT / ".audio-cache"
DEFAULT_BACKEND = "piper"
DEFAULT_PIPER_VOICE = DEFAULT_PIPER_VOICE_NAME
DEFAULT_PIPER_VOICES_DIR = DEFAULT_PIPER_VOICE_DIR
ALLOWED_BACKENDS = {"piper", "openai"}
ALLOWED_OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
OPENAI_QUALITY_MODELS = {"hd": "tts-1-hd", "fast": "tts-1"}
MAX_CACHED_RENDERS = 64

app = Flask(__name__)

_html_path = DEFAULT_HTML
_cache_dir: Path = DEFAULT_CACHE_DIR
_piper_voice_name = DEFAULT_PIPER_VOICE
_piper_voices_dir: Path = DEFAULT_PIPER_VOICES_DIR

_openai_client = None
_piper_voice = None
_piper_lock = threading.Lock()

# token -> job metadata
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()


def _remember(token: str, job: dict) -> None:
    with _jobs_lock:
        _jobs[token] = job
        _jobs.move_to_end(token)
        while len(_jobs) > MAX_CACHED_RENDERS:
            _jobs.popitem(last=False)


def _get_job(token: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(token)
        if job is not None:
            _jobs.move_to_end(token)
        return job


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _write_cache_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _openai_available() -> tuple[bool, str]:
    try:
        from openai import OpenAI  # noqa: F401
    except ImportError:
        return False, "openai package is not installed"
    if not os.environ.get("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY is not set"
    return True, "ready"


def _piper_available() -> tuple[bool, str]:
    model_path = _piper_voices_dir / f"{_piper_voice_name}.onnx"
    config_path = _piper_voices_dir / f"{_piper_voice_name}.onnx.json"
    if not model_path.exists() or not config_path.exists():
        return False, (
            f"missing voice files: {model_path.name} / {config_path.name}"
        )
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg is not on PATH"
    try:
        load_voice(_piper_voice_name, _piper_voices_dir)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    return True, "ready"


def _client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI()
    return _openai_client


def _load_piper_voice():
    global _piper_voice
    if _piper_voice is None:
        _piper_voice = load_voice(_piper_voice_name, _piper_voices_dir)
    return _piper_voice


def _synthesize_openai(chunk: str, *, model: str, voice: str, speed: float) -> bytes:
    response = _client().audio.speech.create(
        model=model,
        voice=voice,
        input=chunk,
        speed=speed,
        response_format="mp3",
    )
    return response.content


def _synthesize_piper(chunk: str, *, speed: float) -> bytes:
    with _piper_lock:
        wav_bytes = synthesize_wav_bytes(_load_piper_voice(), chunk, speed=speed)
    return wav_to_mp3_bytes(wav_bytes)


@app.get("/")
def index() -> Response:
    return send_file(_html_path)


@app.get("/healthz")
def healthz():
    piper_ok, piper_msg = _piper_available()
    openai_ok, openai_msg = _openai_available()
    return jsonify(
        ok=True,
        default_backend=DEFAULT_BACKEND,
        piper={"available": piper_ok, "message": piper_msg},
        openai={"available": openai_ok, "message": openai_msg},
    )


@app.post("/api/prepare")
def prepare():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(message="No text provided."), 400

    backend = str(data.get("backend") or DEFAULT_BACKEND).strip().lower()
    if backend not in ALLOWED_BACKENDS:
        backend = DEFAULT_BACKEND

    title = (data.get("title") or "Untitled").strip()
    try:
        speed = float(data.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.25, min(4.0, speed))

    chunks = to_speech_chunks(text)
    if not chunks:
        return jsonify(message="No speakable content found in the text."), 400

    if backend == "piper":
        voice = str(data.get("voice") or DEFAULT_PIPER_VOICE).strip()
        if voice != DEFAULT_PIPER_VOICE:
            return jsonify(
                message=(
                    f"Piper mode currently uses the installed voice "
                    f"'{DEFAULT_PIPER_VOICE}'."
                )
            ), 400
        piper_ok, piper_msg = _piper_available()
        if not piper_ok:
            return jsonify(
                message=(
                    "Piper is not available right now. "
                    f"{piper_msg}. This is not a fallback path."
                )
            ), 400
        key = _cache_key("piper", text, DEFAULT_PIPER_VOICE, f"{speed:.3f}")
        cache_path = _cache_dir / f"{key}.mp3"
        token = token_urlsafe(9)
        _remember(
            token,
            {
                "backend": "piper",
                "chunks": chunks,
                "speed": speed,
                "title": title,
                "cache_path": str(cache_path),
                "complete": cache_path.exists(),
            },
        )
        return jsonify(
            token=token,
            chunk_count=len(chunks),
            char_count=sum(len(c) for c in chunks),
            backend="piper",
            voice=DEFAULT_PIPER_VOICE,
            speed=speed,
            cached=cache_path.exists(),
            cached_kb=round(cache_path.stat().st_size / 1024, 1) if cache_path.exists() else None,
        )

    voice = str(data.get("voice") or "onyx").lower()
    if voice not in ALLOWED_OPENAI_VOICES:
        voice = "onyx"
    quality = str(data.get("quality") or "hd").lower()
    model = OPENAI_QUALITY_MODELS.get(quality, OPENAI_QUALITY_MODELS["hd"])
    openai_ok, openai_msg = _openai_available()
    if not openai_ok:
        return jsonify(
            message=(
                "OpenAI mode is not available right now. "
                f"{openai_msg}. This is not a fallback path."
            )
        ), 400

    key = _cache_key("openai", text, voice, f"{speed:.3f}", model)
    cache_path = _cache_dir / f"{key}.mp3"
    token = token_urlsafe(9)
    _remember(
        token,
        {
            "backend": "openai",
            "chunks": chunks,
            "voice": voice,
            "speed": speed,
            "model": model,
            "title": title,
            "cache_path": str(cache_path),
            "complete": cache_path.exists(),
        },
    )
    return jsonify(
        token=token,
        chunk_count=len(chunks),
        char_count=sum(len(c) for c in chunks),
        backend="openai",
        voice=voice,
        model=model,
        speed=speed,
        cached=cache_path.exists(),
        cached_kb=round(cache_path.stat().st_size / 1024, 1) if cache_path.exists() else None,
    )


@app.get("/api/stream/<token>")
def stream(token: str) -> Response:
    job = _get_job(token)
    if job is None:
        abort(404)
    cache_path = Path(job["cache_path"])

    if cache_path.exists():
        return send_file(cache_path, mimetype="audio/mpeg", conditional=True, max_age=0)

    backend = job["backend"]
    chunks = job["chunks"]
    speed = job["speed"]

    def generate():
        collected = bytearray()
        completed = False
        try:
            for chunk in chunks:
                if backend == "piper":
                    audio = _synthesize_piper(chunk, speed=speed)
                else:
                    audio = _synthesize_openai(
                        chunk,
                        model=job["model"],
                        voice=job["voice"],
                        speed=speed,
                    )
                collected.extend(audio)
                yield audio
            completed = True
        finally:
            if completed:
                _write_cache_atomic(cache_path, bytes(collected))
                job["complete"] = True

    return Response(generate(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/download/<token>")
def download(token: str):
    job = _get_job(token)
    if job is None:
        abort(404)
    cache_path = Path(job["cache_path"])
    if not cache_path.exists():
        return jsonify(message="Render not complete yet. Play it through first."), 425

    data = cache_path.read_bytes()
    safe = "".join(c if c.isalnum() else "-" for c in job["title"]).strip("-").lower() or "audio"
    return Response(
        data,
        mimetype="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.mp3"',
            "Content-Length": str(len(data)),
        },
    )


def main() -> int:
    global _html_path, _cache_dir, _piper_voice_name, _piper_voices_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--voice", default=DEFAULT_PIPER_VOICE,
                        help="Default Piper voice name.")
    parser.add_argument("--voices-dir", type=Path, default=DEFAULT_PIPER_VOICES_DIR,
                        help="Directory containing Piper voice files.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="Where finished MP3s are stored for reuse.")
    args = parser.parse_args()

    if not args.html.exists():
        print(f"HTML not found: {args.html}")
        return 2

    _html_path = args.html.resolve()
    _cache_dir = args.cache_dir.resolve()
    _cache_dir.mkdir(parents=True, exist_ok=True)
    _piper_voice_name = args.voice.strip() or DEFAULT_PIPER_VOICE
    _piper_voices_dir = args.voices_dir.resolve()

    piper_ok, piper_msg = _piper_available()
    openai_ok, openai_msg = _openai_available()
    cached_count = len(list(_cache_dir.glob("*.mp3")))

    url = f"http://{args.host}:{args.port}/"
    print(f"Doc-to-Audio server ready at {url}")
    print(f"Default Piper voice: {_piper_voice_name}")
    print(f"Piper: {'ready' if piper_ok else 'unavailable'} ({piper_msg})")
    print(f"OpenAI: {'ready' if openai_ok else 'unavailable'} ({openai_msg})")
    print(f"Audio cache: {_cache_dir} ({cached_count} render(s) on disk)")
    print("Open the page, pick a backend, paste text, and press Play.")
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
