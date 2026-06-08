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

Run with the ai-engine virtualenv python (which has openai + flask):

    & "C:\\source\\repos\\bpm\\internal\\ai-engine\\.venv\\Scripts\\python.exe" `
      "C:\\source\\repos\\bpm\\internal\\the-guiding-light\\tools\\serve_audio.py"
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from secrets import token_urlsafe
from types import ModuleType

from flask import Flask, Response, abort, jsonify, request, send_file

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HTML = REPO_ROOT / "src" / "doc-to-audio.html"
DEFAULT_HANDLER = Path(
    r"C:\source\repos\bpm\internal\ai-engine\packages"
    r"\warehouse-intelligence-capabilities-registry\src\capabilities\handlers"
    r"\convert_document_to_audio.py"
)

ALLOWED_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
QUALITY_MODELS = {"hd": "tts-1-hd", "fast": "tts-1"}
DEFAULT_VOICE = "onyx"
DEFAULT_QUALITY = "hd"
MAX_CACHED_RENDERS = 64  # bound the in-memory render cache

app = Flask(__name__)

_handler: ModuleType | None = None
_openai_client = None
_html_path = DEFAULT_HTML

# token -> {text, voice, speed, model, title, bytes: bytes|None, complete: bool}
_jobs: "OrderedDict[str, dict]" = OrderedDict()
_jobs_lock = threading.Lock()


def _load_handler(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("cdta_handler", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load handler module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI()
    return _openai_client


def _chunks_for(text: str) -> list[str]:
    """Same prose + chunk preparation the synchronous handler uses."""
    assert _handler is not None
    prose = _handler._markdown_to_prose(text)
    chunks = _handler._split_into_chunks(prose, _handler._MAX_CHARS_PER_CHUNK)
    rendered = [_handler._strip_control_markers(c) for c in chunks]
    return [c for c in rendered if c.strip()]


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

    token = token_urlsafe(9)
    _remember(token, {
        "chunks": chunks,
        "voice": voice,
        "speed": speed,
        "model": model,
        "title": title,
        "bytes": None,
        "complete": False,
    })
    return jsonify(
        token=token,
        chunk_count=len(chunks),
        char_count=sum(len(c) for c in chunks),
        voice=voice,
        speed=speed,
        model=model,
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

    # Already fully rendered (e.g. a replay): stream the cached bytes instantly.
    if job["complete"] and job["bytes"]:
        cached = job["bytes"]

        def replay():
            view = memoryview(cached)
            step = 32 * 1024
            for offset in range(0, len(view), step):
                yield bytes(view[offset:offset + step])

        return Response(replay(), mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store"})

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
            # Only cache as a complete render if every chunk made it out; a
            # client that closes the tab mid-stream should not poison the cache.
            if completed:
                job["bytes"] = bytes(collected)
                job["complete"] = True

    return Response(generate(), mimetype="audio/mpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/download/<token>")
def download(token: str):
    job = _get_job(token)
    if job is None:
        abort(404)
    if not (job["complete"] and job["bytes"]):
        # Not finished rendering yet — the page enables this only after playback.
        return jsonify(message="Render not complete yet. Play it through first."), 425

    safe = "".join(c if c.isalnum() else "-" for c in job["title"]).strip("-").lower() or "audio"
    return Response(
        job["bytes"],
        mimetype="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{safe}.mp3"',
            "Content-Length": str(len(job["bytes"])),
        },
    )


def main() -> int:
    global _handler, _html_path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--handler", type=Path, default=DEFAULT_HANDLER)
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set in the environment.")
        return 2
    if not args.handler.exists():
        print(f"Handler not found: {args.handler}")
        return 2
    if not args.html.exists():
        print(f"HTML not found: {args.html}")
        return 2

    _handler = _load_handler(args.handler)
    _html_path = args.html.resolve()

    url = f"http://{args.host}:{args.port}/"
    print(f"Doc-to-Audio streaming server ready at {url}")
    print("Open it in your browser, paste text, and press Play.")
    # threaded=True so synthesis on one request never blocks another.
    app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
