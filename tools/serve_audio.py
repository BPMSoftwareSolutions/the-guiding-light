#!/usr/bin/env python
"""Local streaming-TTS server for the Doc-to-Audio page.

This version is Piper-only. It loads a local Piper voice model at startup and
refuses to serve if Piper, the voice files, or ffmpeg are missing. There is no
cloud fallback path.

Endpoints
    GET  /                      -> serves src/doc-to-audio.html
    POST /api/prepare           -> {text,title,voice,speed} -> {token,...}
    GET  /api/stream/<token>    -> audio/mpeg, streamed progressively
    GET  /api/download/<token>  -> the fully-rendered MP3 (attachment), once ready
    GET  /healthz               -> ok

Prose cleaning and chunking come from this repo's own ``prose_chunking`` module.
Run with any Python that has ``piper-tts`` and ``flask`` installed:

    python tools/serve_audio.py
"""
from __future__ import annotations

import argparse
import hashlib
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
    DEFAULT_VOICE_NAME,
    load_voice,
    synthesize_wav_bytes,
    wav_to_mp3_bytes,
)
from prose_chunking import to_speech_chunks  # noqa: E402  (sibling module)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = REPO_ROOT / "src" / "doc-to-audio.html"
# Physical, content-addressed MP3 cache. A render of the same text + voice +
# speed is stored once and replayed from disk forever after, surviving server
# restarts.
DEFAULT_CACHE_DIR = REPO_ROOT / ".audio-cache"

DEFAULT_VOICE = DEFAULT_VOICE_NAME
DEFAULT_VOICE_DIR = DEFAULT_PIPER_VOICE_DIR
MAX_CACHED_RENDERS = 64  # bound the in-memory render cache

app = Flask(__name__)

_voice = None
_voice_name = DEFAULT_VOICE_NAME
_voices_dir: Path = DEFAULT_VOICE_DIR
_html_path = DEFAULT_HTML
_cache_dir: Path = DEFAULT_CACHE_DIR

# token -> {chunks, speed, title, cache_path, complete}
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()
_voice_lock = threading.Lock()


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


def _cache_key(text: str, voice: str, speed: float) -> str:
    """Content address for a render. Identical inputs -> identical key -> reuse."""
    h = hashlib.sha256()
    h.update(f"piper\x00{voice}\x00{speed:.3f}\x00".encode("utf-8"))
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


def _write_cache_atomic(path: Path, data: bytes) -> None:
    """Write the finished MP3 to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


def _synthesize_mp3(chunk: str, *, speed: float) -> bytes:
    assert _voice is not None
    with _voice_lock:
        wav_bytes = synthesize_wav_bytes(_voice, chunk, speed=speed)
    return wav_to_mp3_bytes(wav_bytes)


@app.get("/")
def index() -> Response:
    return send_file(_html_path)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.post("/api/prepare")
def prepare():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(message="No text provided."), 400

    voice = str(data.get("voice") or DEFAULT_VOICE).strip()
    if voice and voice != DEFAULT_VOICE:
        return (
            jsonify(
                message=(
                    f"Only the installed Piper voice '{DEFAULT_VOICE}' is available "
                    "right now. This app will not fall back to OpenAI."
                )
            ),
            400,
        )

    try:
        speed = float(data.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.25, min(4.0, speed))

    title = (data.get("title") or "Untitled").strip()

    chunks = to_speech_chunks(text)
    if not chunks:
        return jsonify(message="No speakable content found in the text."), 400

    key = _cache_key(text, DEFAULT_VOICE, speed)
    cache_path = _cache_dir / f"{key}.mp3"
    cached = cache_path.exists()

    token = token_urlsafe(9)
    _remember(
        token,
        {
            "chunks": chunks,
            "speed": speed,
            "title": title,
            "cache_path": str(cache_path),
            "complete": cached,
        },
    )
    return jsonify(
        token=token,
        chunk_count=len(chunks),
        char_count=sum(len(c) for c in chunks),
        voice=DEFAULT_VOICE,
        speed=speed,
        backend="piper",
        cached=cached,
        cached_kb=round(cache_path.stat().st_size / 1024, 1) if cached else None,
    )


@app.get("/api/stream/<token>")
def stream(token: str) -> Response:
    job = _get_job(token)
    if job is None:
        abort(404)
    cache_path = Path(job["cache_path"])

    # Cache hit: serve via send_file so Flask handles Range requests automatically.
    if cache_path.exists():
        return send_file(cache_path, mimetype="audio/mpeg", conditional=True, max_age=0)

    chunks = job["chunks"]
    speed = job["speed"]

    def generate():
        collected = bytearray()
        completed = False
        try:
            for chunk in chunks:
                audio = _synthesize_mp3(chunk, speed=speed)
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
    global _html_path, _cache_dir, _voice, _voice_name, _voices_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help="Installed Piper voice name.")
    parser.add_argument("--voices-dir", type=Path, default=DEFAULT_VOICE_DIR,
                        help="Directory containing Piper voice files.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="Where finished MP3s are stored for reuse.")
    args = parser.parse_args()

    if not args.html.exists():
        print(f"HTML not found: {args.html}")
        return 2
    if not shutil.which("ffmpeg"):
        print(
            "ffmpeg is required to convert Piper WAV output into browser-friendly MP3.",
            file=sys.stderr,
        )
        return 2

    _html_path = args.html.resolve()
    _cache_dir = args.cache_dir.resolve()
    _cache_dir.mkdir(parents=True, exist_ok=True)
    _voices_dir = args.voices_dir.resolve()
    _voice_name = args.voice.strip() or DEFAULT_VOICE

    try:
        _voice = load_voice(_voice_name, _voices_dir)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 2

    cached_count = len(list(_cache_dir.glob("*.mp3")))
    url = f"http://{args.host}:{args.port}/"
    print(f"Piper streaming server ready at {url}")
    print(f"Voice: {_voice_name}")
    print(f"Voice files: {_voices_dir}")
    print(f"Audio cache: {_cache_dir} ({cached_count} render(s) on disk)")
    print("Open it in your browser, paste text, and press Play.")
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
