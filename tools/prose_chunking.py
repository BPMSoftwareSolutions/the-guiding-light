"""Markdown-to-speech prose cleaning and chunking — self-contained.

Turns a markdown/plain-text document into clean, speakable prose and splits it
into chunks under the OpenAI TTS character limit. This is The Guiding Light's
own copy of that logic; the audio tools depend on nothing outside this repo.

Public API:
    to_speech_chunks(text)   -> list[str]   full pipeline (prose -> split -> clean)
    markdown_to_prose(text)  -> str         markdown stripped to spoken prose
    strip_control_markers(text) -> str      remove internal pacing markers
    MAX_CHARS_PER_CHUNK      -> int         per-chunk character ceiling

The chunking heuristics (verse-citation detection, heading/stanza splitting,
pacing markers) are preserved verbatim from the prior workflow so narration is
byte-for-byte identical to what it produced.
"""
from __future__ import annotations

import re

MAX_CHARS_PER_CHUNK = 4000
_HEADING_DASH_PAUSE_MARKER = '[[heading-dash-pause-1200ms]]'


def _normalize_speech_line(text: str) -> str:
    return re.sub(r'^[^\w"\']+', '', text).strip()


def _is_verse_annotation_paragraph(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(re.match(r'^["].+["]\s+[-]\s+[1-3]?[A-Za-z][A-Za-z\s]+\d+:\d+', stripped))


def _should_split_line_group(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False

    brief_lines = 0
    rhythmic_lines = 0
    for line in lines:
        if len(line) > 90:
            return False
        if len(re.findall(r"\b[\w']+\b", line)) <= 10:
            brief_lines += 1
        if line.endswith(('', '...')) or '' in line or not re.search(r'[.!?](["])?$', line):
            rhythmic_lines += 1

    return brief_lines == len(lines) and rhythmic_lines >= len(lines) - 1


def _expand_structural_units(paragraph: str) -> list[str]:
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if _should_split_line_group(lines):
        return lines
    return [paragraph.strip()]


def _split_heading_dash_line(match: re.Match[str]) -> str:
    left, right = re.split(r'\s+\s+', match.group(0), maxsplit=1)
    left = _normalize_speech_line(left.strip())
    right = _normalize_speech_line(right.strip())
    return f'{left} {_HEADING_DASH_PAUSE_MARKER}\n\n{right}'


def _split_heading_dash_line_if_applicable(match: re.Match[str]) -> str:
    candidate = match.group(0).strip()
    if _is_verse_annotation_paragraph(candidate):
        return candidate
    return _split_heading_dash_line(match)


def _markdown_to_prose(text: str) -> str:
    # Remove fenced code blocks entirely  they don't read well as audio
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Remove image syntax
    text = re.sub(r'!\[[^\]]*\]\([^\)]*\)', '', text)
    # Convert links to just the link text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    # Remove table separator rows (---|---)
    text = re.sub(r'^\s*[\|]?[\s\-:]+[\|][\s\-:|]+[\|]?\s*$', '', text, flags=re.MULTILINE)
    # Strip table pipe formatting, keep cell content
    text = re.sub(r'\|', ' ', text)
    # Remove setext-style headings (underline with === or ---)
    text = re.sub(r'^[=\-]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Convert ATX headings to plain text (remove # markers)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Preserve strong emphasis in quoted speech form before stripping markdown markers.
    text = re.sub(r'\*\*([^\*]+)\*\*', lambda match: f'"{match.group(1).strip()}"', text)
    # Remove bold and italic markers
    text = re.sub(r'\*{1,3}([^\*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    # Remove blockquote markers
    text = re.sub(r'^\s*>\s?', '', text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Clean up list markers  convert to sentence lead-ins
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines to a single paragraph break
    text = re.sub(r'\n{3,}', '\n\n', text)
    # Strip leading/trailing whitespace per line and isolate verse annotations into their own paragraphs.
    normalized_lines = [_normalize_speech_line(line.strip()) for line in text.splitlines()]
    lines: list[str] = []
    for line in normalized_lines:
        if _is_verse_annotation_paragraph(line):
            if lines and lines[-1] != '':
                lines.append('')
            lines.append(line)
            lines.append('')
            continue
        lines.append(line)
    text = '\n'.join(lines)
    text = re.sub(r'^.{1,80}?\s+\s+.{1,120}$', _split_heading_dash_line_if_applicable, text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _split_into_chunks(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        paragraphs = [paragraph.strip() for paragraph in re.split(r'\n{2,}', text) if paragraph.strip()]
        structural_units: list[str] = []
        for paragraph in paragraphs:
            structural_units.extend(_expand_structural_units(paragraph))
        return structural_units or [text]

    # Split into structural units first, then sentences if a unit is too long.
    paragraphs = re.split(r'\n{2,}', text)
    chunks: list[str] = []

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        for unit in _expand_structural_units(paragraph):
            if _is_verse_annotation_paragraph(unit) or len(unit) <= max_chars:
                chunks.append(unit)
                continue

            current = ''
            sentences = re.split(r'(?<=[.!?])\s+', unit)
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                if len(current) + len(sentence) + 1 > max_chars:
                    if current:
                        chunks.append(current.strip())
                    current = sentence
                else:
                    current = f'{current} {sentence}'.strip() if current else sentence
            if current:
                chunks.append(current.strip())

    return [c for c in chunks if c]


def _strip_control_markers(text: str) -> str:
    cleaned = text.replace(_HEADING_DASH_PAUSE_MARKER, '')
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r' +\n', '\n', cleaned)
    return cleaned.strip()


def markdown_to_prose(text: str) -> str:
    """Convert markdown/plain text into clean, speakable prose."""
    return _markdown_to_prose(text)


def strip_control_markers(text: str) -> str:
    """Remove internal pacing markers, leaving text ready for synthesis."""
    return _strip_control_markers(text)


def to_speech_chunks(text: str) -> list[str]:
    """Full pipeline: clean prose, split into TTS-sized chunks, strip markers."""
    prose = _markdown_to_prose(text)
    chunks = _split_into_chunks(prose, MAX_CHARS_PER_CHUNK)
    rendered = [_strip_control_markers(c) for c in chunks]
    return [c for c in rendered if c.strip()]
