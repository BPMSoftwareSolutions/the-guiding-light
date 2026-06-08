#!/usr/bin/env python
"""Local streaming-TTS server for the Doc-to-Audio page.

This is the browser-facing counterpart of tools/stream_to_audio.py. Instead of
the legacy "submit job -> poll -> download finished MP3" flow, it streams audio
to the browser chunk-by-chunk: each chunk is synthesized and flushed the moment
OpenAI returns it, so an <audio> element starts playing within a second or two
while the rest of the document is still being generated. The browser is the
playback buffer.

Endpoints
    GET  /                      -> serves src/doc-to-audio.html
    POST /api/prepare           -> {text,title,voice,speed,quality} -> {token,...}
    GET  /api/stream/<token>    -> audio/mpeg, streamed progressively
    GET  /api/download/<token>  -> the fully-rendered MP3 (attachment), once ready
    GET  /healthz               -> ok

Prose cleaning and chunking come from this repo's own ``prose_chunking`` module.
Run with any Python that has ``openai`` and ``flask`` installed:

    python tools/serve_audio.py
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from secrets import token_urlsafe

from flask import Flask, Response, abort, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prose_chunking import to_speech_chunks  # noqa: E402  (sibling module)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = REPO_ROOT / "src" / "doc-to-audio.html"
# Physical, content-addressed MP3 cache. A render of the same text + voice +
# speed + model is stored once and replayed from disk forever after — no
# re-generation, surviving server restarts.
DEFAULT_CACHE_DIR = REPO_ROOT / ".audio-cache"

ALLOWED_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
QUALITY_MODELS = {"hd": "tts-1-hd", "fast": "tts-1"}
DEFAULT_VOICE = "onyx"
DEFAULT_QUALITY = "hd"
MAX_CACHED_RENDERS = 64  # bound the in-memory render cache

app = Flask(__name__)

_openai_client = None
_html_path = DEFAULT_HTML
_cache_dir: Path = DEFAULT_CACHE_DIR

# token -> {chunks, voice, speed, model, title, cache_key, cache_path, complete}
# The finished audio itself lives on disk at cache_path (content-addressed), so a
# token that gets evicted here still resolves to the same cached MP3 on replay.
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()


def _client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI()
    return _openai_client


def _chunks_for(text: str) -> list[str]:
    """Clean prose + split into TTS-sized chunks (this repo's own logic)."""
    return to_speech_chunks(text)


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


def _cache_key(text: str, voice: str, speed: float, model: str) -> str:
    """Content address for a render. Identical inputs -> identical key -> reuse."""
    h = hashlib.sha256()
    h.update(f"{model}\x00{voice}\x00{speed:.3f}\x00".encode("utf-8"))
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


def _write_cache_atomic(path: Path, data: bytes) -> None:
    """Write the finished MP3 to disk atomically so a crashed/aborted render can
    never leave a half file that later looks like a complete cache hit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


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

    voice = str(data.get("voice") or DEFAULT_VOICE).lower()
    if voice not in ALLOWED_VOICES:
        voice = DEFAULT_VOICE
    try:
        speed = float(data.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.25, min(4.0, speed))
    quality = str(data.get("quality") or DEFAULT_QUALITY).lower()
    model = QUALITY_MODELS.get(quality, QUALITY_MODELS[DEFAULT_QUALITY])
    title = (data.get("title") or "Untitled").strip()

    chunks = _chunks_for(text)
    if not chunks:
        return jsonify(message="No speakable content found in the text."), 400

    key = _cache_key(text, voice, speed, model)
    cache_path = _cache_dir / f"{key}.mp3"
    cached = cache_path.exists()

    token = token_urlsafe(9)
    _remember(token, {
        "chunks": chunks,
        "voice": voice,
        "speed": speed,
        "model": model,
        "title": title,
        "cache_key": key,
        "cache_path": str(cache_path),
        "complete": cached,
    })
    return jsonify(
        token=token,
        chunk_count=len(chunks),
        char_count=sum(len(c) for c in chunks),
        voice=voice,
        speed=speed,
        model=model,
        cached=cached,
        cached_kb=round(cache_path.stat().st_size / 1024, 1) if cached else None,
    )


def _synthesize(chunk: str, *, model: str, voice: str, speed: float) -> bytes:
    response = _client().audio.speech.create(
        model=model,
        voice=voice,
        input=chunk,
        speed=speed,
        response_format="mp3",
    )
    return response.content


@app.get("/api/stream/<token>")
def stream(token: str) -> Response:
    job = _get_job(token)
    if job is None:
        abort(404)
    cache_path = Path(job["cache_path"])

    # Cache hit: stream the physical MP3 from disk — instant, zero API calls.
    # Covers both same-session replays and renders from a previous run.
    if cache_path.exists():
        def replay():
            with open(cache_path, "rb") as fh:
                while True:
                    block = fh.read(64 * 1024)
                    if not block:
                        break
                    yield block

        return Response(replay(), mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store"})

    # Cache miss: synthesize chunk-by-chunk, flush each as it lands, and persist
    # the assembled MP3 to disk once (and only if) the full render completes.
    chunks = job["chunks"]
    model, voice, speed = job["model"], job["voice"], job["speed"]

    def generate():
        collected = bytearray()
        completed = False
        try:
            for chunk in chunks:
                audio = _synthesize(chunk, model=model, voice=voice, speed=speed)
                collected.extend(audio)
                yield audio
            completed = True
        finally:
            # A client that closes the tab mid-stream must not poison the cache.
            if completed:
                _write_cache_atomic(cache_path, bytes(collected))
                job["complete"] = True

    return Response(generate(), mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/download/<token>")
def download(token: str):
    job = _get_job(token)
    if job is None:
        abort(404)
    cache_path = Path(job["cache_path"])
    if not cache_path.exists():
        # Not finished rendering yet — the page enables this only after playback.
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
    global _html_path, _cache_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="Where finished MP3s are stored for reuse.")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set in the environment.")
        return 2
    if not args.html.exists():
        print(f"HTML not found: {args.html}")
        return 2

    _html_path = args.html.resolve()
    _cache_dir = args.cache_dir.resolve()
    _cache_dir.mkdir(parents=True, exist_ok=True)
    cached_count = len(list(_cache_dir.glob("*.mp3")))

    url = f"http://{args.host}:{args.port}/"
    print(f"Doc-to-Audio streaming server ready at {url}")
    print(f"Audio cache: {_cache_dir} ({cached_count} render(s) on disk)")
    print("Open it in your browser, paste text, and press Play.")
    # threaded=True so synthesis on one request never blocks another.
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
