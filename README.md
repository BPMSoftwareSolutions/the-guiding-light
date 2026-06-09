# The Guiding Light

A small project with two halves:

1. **A piece of wisdom literature** - [`docs/the-immutable-truth.md`](docs/the-immutable-truth.md), a formal treatise on the distinction between *true* and *Truth*, and the serpent as the mechanism of fragmentation.
2. **A streaming text-to-speech toolset** - read that document, or any markdown/text, aloud with either **Piper TTS** running locally or **OpenAI Speech** in the cloud. The backend is explicit, so you always know which one is active.

---

## What changed

The project now uses an explicit **multi-backend** pipeline:

```
generator : chunk -> backend -> audio -> local MP3 encode -> bounded buffer
player    : buffer -> play (blocks per chunk)
```

The browser app still streams chunk-by-chunk. Piper stays local, OpenAI stays explicit, and there is no silent fallback between them. If a selected backend is missing its requirements, the request fails loudly.

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
- Python packages: `piper-tts`, `flask`, and `openai`
- **ffmpeg** on your `PATH` for Piper mode in the browser server, because the web path encodes Piper's WAV output into MP3 for streaming playback
- A local Piper voice model, such as `en_US-lessac-medium`

Install the Python packages:

```bash
pip install piper-tts flask openai
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

- supports both Piper and OpenAI through an explicit backend selector
- shows a loud cloud warning when OpenAI is selected
- caches finished MP3 renders in `.audio-cache/`
- fails the selected backend loudly if its requirements are missing

### CLI player

```bash
python tools/stream_to_audio.py docs/the-immutable-truth.md
```

Useful flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--backend` | `piper` | Default backend for the CLI |
| `--voice` | backend-specific | Piper voice or OpenAI voice |
| `--voices-dir` | `.piper-voices` | Where the Piper voice files live |
| `--model` | `tts-1-hd` | OpenAI model for cloud mode |
| `--speed` | `1.0` | Speaking speed |
| `--buffer` | `4` | How many chunks the generator may run ahead |
| `--max-chunks N` | - | Only read the first N chunks |
| `--save out.wav` | - | Also write the full audio to a file |

---

## How it works

1. **Chunking** - markdown is converted to clean prose and split into TTS-sized chunks by [`tools/prose_chunking.py`](tools/prose_chunking.py).
2. **Generation** - each chunk is synthesized through the selected backend.
3. **Streaming playback** - chunks are handed to the player as soon as they are ready:
   - CLI: a bounded `queue.Queue` keeps the generator a few chunks ahead while PowerShell `SoundPlayer` plays each WAV chunk.
   - Web: the Flask server streams each chunk's MP3 output to the browser immediately.

---

## Safety note

There is no hidden fallback between backends.

If you choose Piper and it is unavailable, the app fails with an explicit error explaining what is missing and how to install or download it. The same is true for OpenAI if you choose the cloud backend without an API key.

---

## License

Internal project of BPM Software Solutions.
