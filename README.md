# The Guiding Light

A small project with two halves:

1. **A piece of wisdom literature** - [`docs/the-immutable-truth.md`](docs/the-immutable-truth.md), a formal treatise on the distinction between *true* and *Truth*, and the serpent as the mechanism of fragmentation.
2. **A local text-to-speech toolset** - read that document, or any markdown/text, aloud with **Piper TTS** running on this machine. No OpenAI API key is used anywhere in the current code path.

---

## What changed

The project now uses a **Piper-only** pipeline:

```
generator : chunk -> Piper WAV -> local MP3 encode -> bounded buffer
player    : buffer -> play (blocks per chunk)
```

The browser app still streams chunk-by-chunk, but all synthesis happens locally. If Piper, the voice files, or `ffmpeg` are missing, the server fails loudly at startup instead of silently falling back to anything else.

---

## Repository layout

```text
the-guiding-light/
|-- docs/
|   `-- the-immutable-truth.md
|-- src/
|   `-- doc-to-audio.html
|-- tools/
|   |-- piper_audio.py
|   |-- prose_chunking.py
|   |-- stream_to_audio.py
|   `-- serve_audio.py
`-- README.md
```

---

## Requirements

- **Python 3.12+**
- Python packages: `piper-tts` and `flask`
- **ffmpeg** on your `PATH` for the browser server, because the web path encodes Piper's WAV output into MP3 for streaming playback
- A local Piper voice model, such as `en_US-lessac-medium`

Install the Python packages:

```bash
pip install piper-tts flask
```

Download a Piper voice:

```bash
python -m piper.download_voices en_US-lessac-medium --data-dir .piper-voices
```

---

## Usage

### Web app

```bash
python tools/serve_audio.py --host 127.0.0.1 --port 5000
```

Open `http://127.0.0.1:5000/`, paste text, and press **Play**.

The server:

- loads the Piper voice locally at startup
- refuses to start if the voice files are missing
- refuses to start if `ffmpeg` is missing
- caches finished MP3 renders in `.audio-cache/`

### CLI player

```bash
python tools/stream_to_audio.py docs/the-immutable-truth.md
```

Useful flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--voice` | `en_US-lessac-medium` | Piper voice name |
| `--voices-dir` | `.piper-voices` | Where the voice model files live |
| `--speed` | `1.0` | Speaking speed |
| `--buffer` | `4` | How many chunks the generator may run ahead |
| `--max-chunks N` | - | Only read the first N chunks |
| `--save out.wav` | - | Also write the full audio to a file |

---

## How it works

1. **Chunking** - markdown is converted to clean prose and split into TTS-sized chunks by [`tools/prose_chunking.py`](tools/prose_chunking.py).
2. **Generation** - each chunk is synthesized locally through Piper.
3. **Streaming playback** - chunks are handed to the player as soon as they are ready:
   - CLI: a bounded `queue.Queue` keeps the generator a few chunks ahead while PowerShell `SoundPlayer` plays each WAV chunk.
   - Web: the Flask server converts each chunk's WAV output to MP3 and flushes it to the browser immediately.

---

## Safety note

There is no hidden OpenAI fallback in the current implementation.

If Piper is unavailable, the app stops with an explicit error explaining what is missing and how to install or download it.

---

## License

Internal project of BPM Software Solutions.
