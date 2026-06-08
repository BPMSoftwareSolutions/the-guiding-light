# The Guiding Light

A small project with two halves:

1. **A piece of wisdom literature** — [`docs/the-immutable-truth.md`](docs/the-immutable-truth.md), a formal treatise on the distinction between *true* and *Truth*, and the serpent as the mechanism of fragmentation.
2. **A streaming text-to-speech toolset** — read that document (or any markdown/text) aloud with **instant-play buffering**: audio begins playing within a second or two and keeps going while the rest is still being synthesized, instead of waiting for the whole file to render.

---

## Why "instant-play buffering"

The conventional way to turn a document into speech is synchronous: synthesize **every** chunk, then play or download the finished file. For anything longer than a paragraph you sit and wait.

This project restructures that single loop into a **producer/consumer pipeline**:

```
generator : chunk -> OpenAI TTS -> audio  ──push──▶  bounded buffer
player    : buffer ──▶ play (blocks per chunk)
```

Because each chunk is an independently playable audio segment, playback can start the moment the **first** chunk is ready and continue seamlessly while later chunks are still being generated. Time-to-first-audio drops from "the whole document" to ~1–3 seconds.

Two front-ends ship with the same idea:

| Tool | What it is | Playback |
| --- | --- | --- |
| [`tools/stream_to_audio.py`](tools/stream_to_audio.py) | CLI player | Generator thread + bounded queue; plays each chunk through the OS via .NET `SoundPlayer` (Windows, zero extra deps) |
| [`tools/serve_audio.py`](tools/serve_audio.py) + [`src/doc-to-audio.html`](src/doc-to-audio.html) | Local web app | Flask streams each chunk's MP3 to the browser the instant it's ready; an `<audio>` element plays progressively while the server keeps rendering |

---

## Repository layout

```
the-guiding-light/
├── docs/
│   └── the-immutable-truth.md   # the wisdom document
├── src/
│   └── doc-to-audio.html        # browser UI for the streaming server
├── tools/
│   ├── stream_to_audio.py       # CLI: stream a markdown file to your speakers
│   └── serve_audio.py           # Flask server behind doc-to-audio.html
└── README.md
```

---

## Requirements

- **Python 3.10+**
- Python packages: `openai`, and (for the web server) `flask`
- An **`OPENAI_API_KEY`** environment variable
- **Windows** for the CLI player's playback path (it uses .NET `System.Media.SoundPlayer` via PowerShell — no audio library to install). The web server is cross-platform; playback there is handled by the browser.

> **Note — shared chunking logic.** Both tools reuse the prose-cleaning and chunking
> helpers from the BPM `ai-engine` capability `convert_document_to_audio.py` so the spoken
> output (markdown stripping, heading pauses, verse handling) matches the existing workflow.
> The default path to that handler is set per-tool and can be overridden with `--handler`.
> If you are running outside that environment, point `--handler` at a copy of those helpers.

```bash
pip install openai flask
# Windows PowerShell:  $env:OPENAI_API_KEY = "sk-..."
# bash:                export OPENAI_API_KEY="sk-..."
```

---

## Usage

### CLI — stream a file to your speakers

```bash
python tools/stream_to_audio.py docs/the-immutable-truth.md
```

Useful flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--voice` | `onyx` | any OpenAI TTS voice (alloy, echo, fable, nova, shimmer, onyx) |
| `--speed` | `1.0` | 0.25–4.0× |
| `--buffer` | `4` | how many chunks to generate ahead of playback |
| `--max-chunks N` | — | only read the first N chunks (excerpt / quick test) |
| `--save out.wav` | — | also write the full audio to a file while you listen |
| `--handler PATH` | (ai-engine) | path to the prose/chunking helper module |

### Web — instant-play in the browser

```bash
python tools/serve_audio.py            # serves http://127.0.0.1:8765/
```

Open the URL, paste markdown or text, choose a voice and quality (**Fast** = `tts-1` for
lowest latency, **HD** = `tts-1-hd` for a richer voice), and press **Play**. Audio starts
streaming immediately.

Server flags: `--host`, `--port`, `--html`, `--handler`, `--cache-dir`.

### Render once, reuse forever (no wasted cycles)

Finished renders are saved as **physical MP3 files** in a content-addressed cache
(`.audio-cache/` by default, override with `--cache-dir`). The cache key is a hash of
`text + voice + speed + model`, so:

- Pressing **Play** again on the same text — even in a new browser tab, or **after a
  server restart** — streams the saved MP3 with **zero OpenAI calls**.
- **Replay** and **Download** never re-generate.
- Changing the voice, speed, or quality produces a different key and renders fresh (as it
  should), then caches that variant too.

The page shows whether a render was generated (with its time-to-first-audio) or **served
from the saved MP3**. The cache is content-addressed, so an evicted session token still
resolves to the same file on disk — nothing is ever generated twice for the same input.

---

## How it works under the hood

1. **Chunking** — markdown is converted to clean prose and split into structural units
   (paragraphs, then sentences) under the OpenAI TTS character limit.
2. **Generation** — each chunk is synthesized independently via the OpenAI Speech API.
3. **Streaming playback** — chunks are handed to the player as soon as they exist:
   - CLI: a bounded `queue.Queue` provides backpressure so the generator stays a few
     chunks ahead; a persistent PowerShell `SoundPlayer` plays each clip to completion.
   - Web: a Flask streaming `Response` flushes each chunk's MP3 bytes; the browser is the
     buffer and plays progressively.

---

## License

Internal project of BPM Software Solutions.
