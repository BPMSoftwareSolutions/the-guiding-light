#!/usr/bin/env python
"""Stream a markdown document to speech and play it back while it is still
being synthesized.

A naive text-to-speech pass synthesizes *every* chunk before it writes or plays
anything -- it blocks until the whole document is done. This script instead
structures the work as a producer/consumer pipeline:

    generator thread : chunk -> Piper TTS (wav) -> temp file -> bounded queue
    player thread    : queue -> SoundPlayer.PlaySync() (blocks per chunk)

Because each chunk is an independently playable audio file, playback can begin
the moment the first chunk is ready and continue seamlessly while later chunks
are still being generated. The bounded queue (``--buffer``) makes the generator
run a few chunks ahead without running away unboundedly.

Playback backend is .NET ``System.Media.SoundPlayer`` driven by a single
persistent PowerShell process -- no extra Python audio dependency required.

Prose cleaning and chunking come from this repo's own ``prose_chunking`` module.
Run with any Python that has ``piper-tts`` installed, e.g.:

    python tools/stream_to_audio.py
"""
from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from piper_audio import (  # noqa: E402  (sibling module)
    DEFAULT_VOICE_DIR,
    DEFAULT_VOICE_NAME,
    load_voice,
    synthesize_wav_bytes,
)
from prose_chunking import to_speech_chunks  # noqa: E402  (sibling module)

DEFAULT_SOURCE = Path(
    r"C:\source\repos\bpm\internal\the-guiding-light\docs\the-immutable-truth.md"
)
DEFAULT_VOICE = DEFAULT_VOICE_NAME
DEFAULT_VOICES_DIR = DEFAULT_VOICE_DIR
DEFAULT_SPEED = 1.0
DEFAULT_BUFFER = 4  # chunks the generator may run ahead of playback

# Sentinel pushed onto the queue to tell the player thread no more chunks remain.
_DONE = object()

# Persistent PowerShell playback loop. Reads one wav path per line from stdin,
# plays it to completion with SoundPlayer.PlaySync (which blocks), then prints
# "DONE" so the caller knows the chunk finished. This gives exact serialization
# and natural backpressure without guessing audio durations.
_PLAYER_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::Out.WriteLine('READY'); [Console]::Out.Flush()
while ($true) {
    $path = [Console]::In.ReadLine()
    if ($null -eq $path -or $path -eq 'QUIT') { break }
    if ([string]::IsNullOrWhiteSpace($path)) { continue }
    try {
        $sp = New-Object System.Media.SoundPlayer $path
        $sp.PlaySync()
        $sp.Dispose()
    } catch {
        [Console]::Error.WriteLine("play-error: $($_.Exception.Message)")
    }
    [Console]::Out.WriteLine('DONE'); [Console]::Out.Flush()
}
"""


class StreamingPlayer:
    """Owns the persistent PowerShell SoundPlayer process and the player thread."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
        self._queue: queue.Queue = queue.Queue(maxsize=DEFAULT_BUFFER)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._play_error: Exception | None = None

    def start(self, buffer: int) -> None:
        self._queue = queue.Queue(maxsize=buffer)
        player_script = self._work_dir / "_player.ps1"
        player_script.write_text(_PLAYER_PS, encoding="utf-8")

        self._proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(player_script),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Wait for the player to announce it is ready.
        ready = self._proc.stdout.readline().strip()
        if ready != "READY":
            raise RuntimeError(f"Playback backend failed to start (got: {ready!r})")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        played = 0
        while True:
            item = self._queue.get()
            if item is _DONE:
                break
            index, total, path = item
            print(f"  > playing    chunk {index}/{total}", flush=True)
            try:
                self._proc.stdin.write(f"{path}\n")
                self._proc.stdin.flush()
                ack = self._proc.stdout.readline().strip()
                if ack != "DONE":
                    raise RuntimeError(f"unexpected player ack: {ack!r}")
            except Exception as exc:  # noqa: BLE001
                self._play_error = exc
                break
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            played += 1

    def enqueue(self, index: int, total: int, path: str) -> None:
        self._queue.put((index, total, path))

    def finish(self) -> None:
        self._queue.put(_DONE)
        if self._thread is not None:
            self._thread.join()
        if self._proc is not None and self._proc.stdin:
            try:
                self._proc.stdin.write("QUIT\n")
                self._proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._play_error is not None:
            raise self._play_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE,
                        help="Markdown file to read aloud.")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help="Piper voice name, for example en_US-lessac-medium.")
    parser.add_argument("--voices-dir", type=Path, default=DEFAULT_VOICES_DIR,
                        help="Directory containing Piper voice files.")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    parser.add_argument("--buffer", type=int, default=DEFAULT_BUFFER,
                        help="How many chunks the generator may run ahead of playback.")
    parser.add_argument("--save", type=Path, default=None,
                        help="Optional: also write the full audio to this .wav path.")
    parser.add_argument("--max-chunks", type=int, default=None,
                        help="Only read the first N chunks (for excerpts / testing).")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"Source not found: {args.source}", file=sys.stderr)
        return 2
    try:
        voice = load_voice(args.voice, args.voices_dir)
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 2

    chunks = to_speech_chunks(args.source.read_text(encoding="utf-8"))
    if not chunks:
        print("No speakable content found.", file=sys.stderr)
        return 1
    if args.max_chunks is not None:
        chunks = chunks[: args.max_chunks]

    total = len(chunks)
    print(f"Streaming '{args.source.name}': {total} chunks, voice={args.voice}, "
          f"buffer={args.buffer}")
    print("Generating chunk 1 ... (playback starts as soon as it's ready)\n")

    saved_segments: list[bytes] = []
    start_time = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="tgl-audio-") as tmp:
        tmp_dir = Path(tmp)
        player = StreamingPlayer(tmp_dir)
        player.start(args.buffer)

        first_audio_at: float | None = None
        for index, chunk in enumerate(chunks, start=1):
            print(f"  + generating chunk {index}/{total} ({len(chunk)} chars)", flush=True)
            wav = synthesize_wav_bytes(voice, chunk, speed=args.speed)
            if first_audio_at is None:
                first_audio_at = time.monotonic() - start_time
            if args.save is not None:
                saved_segments.append(wav)
            seg_path = tmp_dir / f"seg-{index:03d}.wav"
            seg_path.write_bytes(wav)
            # Blocks here once the buffer is full -> generator stays just ahead.
            player.enqueue(index, total, str(seg_path))

        player.finish()

    if first_audio_at is not None:
        print(f"\nTime to first audio: {first_audio_at:.1f}s")
    print(f"Total: {time.monotonic() - start_time:.1f}s")

    if args.save is not None and saved_segments:
        _write_combined_wav(args.save, saved_segments)
        print(f"Saved full audio: {args.save}")

    return 0


def _write_combined_wav(out: Path, segments: list[bytes]) -> None:
    """Concatenate per-chunk WAV bytes into one WAV using ffmpeg if available,
    else fall back to stitching PCM frames with the stdlib wave module."""
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
    except ImportError:
        _stitch_wav_stdlib(out, segments)
        return

    ffmpeg = get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="tgl-save-") as tmp:
        tmp_dir = Path(tmp)
        listing = tmp_dir / "list.txt"
        paths = []
        for i, seg in enumerate(segments):
            p = tmp_dir / f"s-{i:03d}.wav"
            p.write_bytes(seg)
            paths.append(p)
        listing.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in paths), encoding="utf-8"
        )
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", str(out)],
            check=True, capture_output=True, text=True,
        )


def _stitch_wav_stdlib(out: Path, segments: list[bytes]) -> None:
    import io
    import wave

    with wave.open(str(out), "wb") as writer:
        initialized = False
        for seg in segments:
            with wave.open(io.BytesIO(seg), "rb") as reader:
                if not initialized:
                    writer.setparams(reader.getparams())
                    initialized = True
                writer.writeframes(reader.readframes(reader.getnframes()))


if __name__ == "__main__":
    raise SystemExit(main())
