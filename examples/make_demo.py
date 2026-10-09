#!/usr/bin/env python3
"""Generate an original, synthetic video fixture for local media testing.

The video contains three generated title cards, a synthetic sine-wave audio
track, and an SRT sidecar whose cues are explicitly not a transcription.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1280
HEIGHT = 720
FPS = 24
SEGMENT_SECONDS = 4
SAMPLE_RATE = 16000

SLIDES = [
    (
        (31, 75, 93),
        "SIMULATED APP",
        "Generated title card; no real app capture",
        "SIMULATED APP. Synthetic visual label. [NOT A TRANSCRIPTION]",
    ),
    (
        (82, 63, 110),
        "PRE-RECORDED SLIDE",
        "Generated card; no source recording",
        "PRE-RECORDED SLIDE. Synthetic visual label. [NOT A TRANSCRIPTION]",
    ),
    (
        (120, 67, 48),
        "SPEAKER CLAIM",
        "Illustrative claim; not verified speech",
        'SPEAKER CLAIM: "Processing completed successfully." Synthetic example. [NOT A TRANSCRIPTION]',
    ),
]


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        Path("DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _render_slide(index: int, output: Path) -> None:
    color, title, subtitle, _ = SLIDES[index]
    image = Image.new("RGB", (WIDTH, HEIGHT), color)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((58, 54, 1222, 666), radius=34, outline=(255, 255, 255), width=3)
    badge_font = _font(24)
    title_font = _font(66 if len(title) < 18 else 56)
    subtitle_font = _font(30)
    badge = "SYNTHETIC TEST FIXTURE"
    box = draw.textbbox((0, 0), badge, font=badge_font)
    draw.rounded_rectangle((94, 91, 94 + box[2] + 34, 151), radius=20, fill=(255, 255, 255, 35))
    draw.text((111, 104), badge, font=badge_font, fill=(236, 241, 247))
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((WIDTH - (title_box[2] - title_box[0])) / 2, 285), title, font=title_font, fill="white")
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    draw.text(((WIDTH - (subtitle_box[2] - subtitle_box[0])) / 2, 392), subtitle, font=subtitle_font, fill=(220, 229, 237))
    footer = f"Generated segment {index + 1} of 3 | illustrative media only"
    draw.text((98, 600), footer, font=_font(22), fill=(203, 216, 226))
    image.save(output, format="PNG", optimize=True)


def _write_tone(path: Path) -> None:
    frames = SEGMENT_SECONDS * len(SLIDES) * SAMPLE_RATE
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        for offset in range(0, frames, 8192):
            sample_bytes = bytearray()
            end = min(offset + 8192, frames)
            for index in range(offset, end):
                seconds = index / SAMPLE_RATE
                frequency = 440 if seconds < 4 else (523.25 if seconds < 8 else 659.25)
                value = int(0.16 * 32767 * math.sin(2 * math.pi * frequency * seconds))
                sample_bytes.extend(struct.pack("<h", value))
            output.writeframesraw(sample_bytes)


def _resolve_ffmpeg() -> str:
    for choice in (os.environ.get("FFMPEG_BIN"), "ffmpeg"):
        if not choice:
            continue
        found = shutil.which(choice)
        if found:
            return found
        if Path(choice).is_file():
            return str(Path(choice).resolve())
    try:
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(executable).is_file():
            return str(Path(executable).resolve())
    except Exception:
        pass
    raise RuntimeError("FFmpeg not found in FFMPEG_BIN, PATH, or imageio-ffmpeg")


def _concat_quote(path: Path) -> str:
    # The concat demuxer uses ffmpeg's single-quoted token format.
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def _run(command: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=180,
            close_fds=True,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg timed out while building the demo fixture") from exc
    except OSError as exc:
        raise RuntimeError("could not start FFmpeg to build the demo fixture") from exc
    if result.returncode:
        raise RuntimeError(f"FFmpeg failed to build the demo fixture (exit {result.returncode})")


def make_demo(output: Path) -> tuple[Path, Path]:
    target = output.expanduser().resolve()
    if target.suffix.lower() == ".mp4":
        video_path = target
        subtitles_path = target.with_suffix(".srt")
    else:
        target.mkdir(parents=True, exist_ok=True)
        video_path = target / "demo.mp4"
        subtitles_path = target / "demo.srt"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = _resolve_ffmpeg()

    with tempfile.TemporaryDirectory(prefix="codex_video_demo_") as temp_name:
        temp = Path(temp_name)
        for index in range(len(SLIDES)):
            _render_slide(index, temp / f"slide_{index + 1}.png")
        _write_tone(temp / "tone.wav")
        concat = temp / "slides.ffconcat"
        lines = ["ffconcat version 1.0"]
        for index in range(len(SLIDES)):
            lines.append(f"file '{_concat_quote(temp / f'slide_{index + 1}.png')}'")
            lines.append(f"duration {SEGMENT_SECONDS}")
        # Repeating the final still lets the concat demuxer preserve its duration.
        lines.append(f"file '{_concat_quote(temp / f'slide_{len(SLIDES)}.png')}'")
        concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp_video = temp / "demo.mp4"
        _run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-i",
                str(temp / "tone.wav"),
                "-vf",
                f"fps={FPS},format=yuv420p",
                "-frames:v",
                str(FPS * SEGMENT_SECONDS * len(SLIDES)),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "19",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-shortest",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(temp_video),
            ]
        )
        if not temp_video.is_file() or temp_video.stat().st_size == 0:
            raise RuntimeError("FFmpeg produced an empty demo video")
        os.replace(temp_video, video_path)

    cues = []
    for index, (_, _, _, text) in enumerate(SLIDES, 1):
        start_seconds = (index - 1) * SEGMENT_SECONDS
        end_seconds = index * SEGMENT_SECONDS
        cues.extend(
            [
                str(index),
                f"{_srt_time(start_seconds)} --> {_srt_time(end_seconds)}",
                text,
                "",
            ]
        )
    subtitles_path.write_text("\n".join(cues), encoding="utf-8")
    return video_path, subtitles_path


def _srt_time(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, whole_seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},000"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="output directory, or an .mp4 path (the .srt is saved beside it)",
    )
    args = parser.parse_args(argv)
    video, subtitles = make_demo(args.output)
    print(video)
    print(subtitles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
