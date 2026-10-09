"""Parse media-time subtitles without converting wall clocks into media time."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path


def number(value, label="time"):
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid {label}")
    return result


def timestamp(text: str) -> float:
    bits = text.strip().replace(",", ".").split(":")
    if len(bits) not in (2, 3):
        raise ValueError("Subtitle timestamp must be MM:SS or HH:MM:SS")
    values = [number(bit) for bit in bits]
    if values[-1] >= 60 or (len(values) == 3 and values[-2] >= 60):
        raise ValueError("Invalid subtitle timestamp")
    return sum(value * 60**i for i, value in enumerate(reversed(values)))


def normalize(items: list, duration: float, source="supplied_subtitle") -> list[dict]:
    if not isinstance(items, list):
        raise ValueError("Transcript must be a list")
    result = []
    for i, item in enumerate(items):
        start, end = number(item["start"]), number(item["end"])
        text = item["text"]
        if start >= end or end > duration + 0.001 or not isinstance(text, str) or not text.strip():
            raise ValueError("Transcript span/text is invalid or outside media duration")
        result.append({"id": f"t{i:06d}", "start": start, "end": min(end, duration),
                       "text": text.strip(), "source": source})
    return sorted(result, key=lambda x: (x["start"], x["end"], x["id"]))


def load(path: Path, duration: float) -> list[dict]:
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return normalize(json.loads(raw), duration)
    if path.suffix.lower() not in (".srt", ".vtt"):
        raise ValueError("Subtitles must be SRT, VTT, or JSON")
    items = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "").strip()):
        lines = block.splitlines()
        for i, line in enumerate(lines):
            if "-->" in line:
                left, right = line.split("-->", 1)
                items.append({"start": timestamp(left), "end": timestamp(right.strip().split()[0]),
                              "text": "\n".join(lines[i + 1:])})
                break
    if not items:
        raise ValueError("No timed subtitle cues found")
    return normalize(items, duration)
