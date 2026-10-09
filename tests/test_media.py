from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_video import media  # noqa: E402


@pytest.fixture(scope="session")
def demo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pytest.importorskip("av")
    pytest.importorskip("PIL")
    pytest.importorskip("imageio_ffmpeg")
    output = tmp_path_factory.mktemp("synthetic_video_demo")
    script = PROJECT_ROOT / "examples" / "make_demo.py"
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=240,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    video = output / "demo.mp4"
    subtitles = output / "demo.srt"
    assert video.is_file() and video.stat().st_size > 0
    text = subtitles.read_text(encoding="utf-8")
    assert "SIMULATED APP" in text
    assert "PRE-RECORDED SLIDE" in text
    assert "SPEAKER CLAIM" in text
    assert "NOT A TRANSCRIPTION" in text
    return video


def test_probe_reports_local_synthetic_video(demo: Path) -> None:
    result = media.probe(demo)
    assert result["duration"] == pytest.approx(12.0, abs=0.08)
    assert result["has_audio"] is True
    assert (result["width"], result["height"]) == (1280, 720)


def test_frame_uses_earliest_presentation_frame_at_or_after_target(demo: Path, tmp_path: Path) -> None:
    from PIL import Image

    first = media.frame(demo, 3.95, tmp_path / "first.png")
    second = media.frame(demo, 4.05, tmp_path / "second.png")
    third = media.frame(demo, 8.05, tmp_path / "third.png")

    for result, requested in ((first, 3.95), (second, 4.05), (third, 8.05)):
        assert result["requested_time"] == requested
        assert result["actual_time"] >= requested
        assert result["path"] is not None
        assert len(result["sha256"]) == 64
        with Image.open(result["path"]) as image:
            assert image.size == (1280, 720)

    assert first["actual_time"] < 4.0
    assert 4.0 <= second["actual_time"] < 8.0
    assert third["actual_time"] >= 8.0
    assert len({first["sha256"], second["sha256"], third["sha256"]}) == 3


def test_frame_past_duration_returns_no_frame(demo: Path, tmp_path: Path) -> None:
    duration = media.probe(demo)["duration"]
    destination = tmp_path / "missing.png"
    result = media.frame(demo, duration + 1, destination)
    assert result["requested_time"] == pytest.approx(duration + 1)
    assert result["actual_time"] is None
    assert result["path"] is None
    assert result["sha256"] is None
    assert result["width"] == 1280
    assert not destination.exists()


def test_vfr_frame_uses_presentation_timestamp(demo: Path, tmp_path: Path) -> None:
    import imageio_ffmpeg

    vfr = tmp_path / "vfr.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(demo),
            "-vf",
            "select='eq(n,0)+eq(n,96)+eq(n,127)+eq(n,240)',setpts=PTS-STARTPTS",
            "-fps_mode",
            "vfr",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            str(vfr),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0 and vfr.is_file()
    frame = media.frame(vfr, 5.0, tmp_path / "vfr_frame.png")
    assert frame["actual_time"] >= 5.0
    assert frame["actual_time"] == pytest.approx(127 / 24, abs=0.02)


def test_nonzero_video_origin_keeps_segments_aligned(demo: Path, tmp_path: Path) -> None:
    import av
    import imageio_ffmpeg
    from PIL import Image

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    offset_video = tmp_path / "offset.mkv"
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-itsoffset",
            "2",
            "-i",
            str(demo),
            "-i",
            str(demo),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-copyts",
            "-fps_mode",
            "passthrough",
            "-t",
            "14",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-c:a",
            "copy",
            "-f",
            "matroska",
            str(offset_video),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0 and offset_video.is_file()
    with av.open(str(offset_video)) as container:
        video = next(stream for stream in container.streams if stream.type == "video")
        audio = next(stream for stream in container.streams if stream.type == "audio")
        video_origin = float(video.start_time * video.time_base)
        assert video_origin > 2.0
        assert float(audio.start_time * audio.time_base) == pytest.approx(0.0, abs=0.02)

    metadata = media.probe(offset_video)
    assert metadata["duration"] == pytest.approx(12.0, abs=0.08)
    assert metadata["time_origin"] == pytest.approx(video_origin, abs=0.001)

    original_at_42 = media.frame(demo, 4.2, tmp_path / "original_42.png")
    clipped = media.clip(offset_video, 4.1, 6.1, tmp_path / "offset_clip.mp4")
    clipped_at_01 = media.frame(clipped["path"], 0.1, tmp_path / "clip_01.png")
    with Image.open(original_at_42["path"]) as original, Image.open(clipped_at_01["path"]) as cut:
        expected = original.convert("RGB").getpixel((640, 360))
        observed = cut.convert("RGB").getpixel((640, 360))
        assert sum(abs(a - b) for a, b in zip(expected, observed)) < 28

    audio_path = tmp_path / "offset_audio.wav"
    media.extract_audio(offset_video, 3, 5, audio_path)
    with wave.open(str(audio_path), "rb") as audio_file:
        samples = audio_file.readframes(8000)
        assert audio_file.getnframes() == pytest.approx(32000, abs=160)
    values = [int.from_bytes(samples[index : index + 2], "little", signed=True) for index in range(0, len(samples), 2)]
    crossings = sum((left <= 0 < right) or (right <= 0 < left) for left, right in zip(values, values[1:]))
    observed_frequency = crossings * 16000 / (2 * max(1, len(values)))
    assert observed_frequency == pytest.approx(523.25, abs=35)


def test_audio_first_container_still_selects_first_video_stream(demo: Path, tmp_path: Path) -> None:
    import av
    import imageio_ffmpeg
    from PIL import Image

    output = tmp_path / "audio_first.mkv"
    result = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(demo),
            "-map",
            "0:a:0",
            "-map",
            "0:v:0",
            "-t",
            "2",
            "-c",
            "copy",
            "-f",
            "matroska",
            str(output),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0 and output.is_file()
    with av.open(str(output)) as container:
        assert [stream.type for stream in container.streams][:2] == ["audio", "video"]
        video = next(stream for stream in container.streams if stream.type == "video")
        assert video.index == 1

    metadata = media.probe(output)
    assert 1.9 <= metadata["duration"] <= 2.3
    extracted = media.frame(output, 0.5, tmp_path / "audio_first_frame.png")
    expected = media.frame(demo, 0.5, tmp_path / "regular_frame.png")
    assert extracted["actual_time"] == pytest.approx(expected["actual_time"], abs=0.02)
    with Image.open(extracted["path"]) as actual, Image.open(expected["path"]) as reference:
        a = actual.convert("RGB").getpixel((640, 360))
        b = reference.convert("RGB").getpixel((640, 360))
        assert sum(abs(x - y) for x, y in zip(a, b)) < 12


def test_output_cannot_overwrite_source(demo: Path) -> None:
    with pytest.raises(ValueError, match="overwrite"):
        media.frame(demo, 0, demo)


def test_extract_audio_writes_mono_16khz_pcm(demo: Path, tmp_path: Path) -> None:
    destination = tmp_path / "sample.wav"
    result = media.extract_audio(demo, 2, 5, destination)
    assert result["start"] == 2
    assert result["end"] == 5
    assert result["path"] == str(destination.resolve())
    assert len(result["sha256"]) == 64
    with wave.open(str(destination), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getframerate() == 16000
        assert audio.getsampwidth() == 2
        assert audio.getnframes() / audio.getframerate() == pytest.approx(3.0, abs=0.04)


def test_clip_writes_playable_h264_mp4(demo: Path, tmp_path: Path) -> None:
    destination = tmp_path / "clip.mp4"
    result = media.clip(demo, 6, 9, destination)
    assert result["start"] == 6
    assert result["end"] == 9
    assert result["path"] == str(destination.resolve())
    assert len(result["sha256"]) == 64
    metadata = media.probe(destination)
    assert metadata["duration"] == pytest.approx(3.0, abs=0.08)
    assert metadata["has_audio"] is True
    assert (metadata["width"], metadata["height"]) == (1280, 720)


@pytest.mark.parametrize("value", [-0.01, float("nan"), float("inf")])
def test_frame_rejects_invalid_times(demo: Path, tmp_path: Path, value: float) -> None:
    with pytest.raises(ValueError):
        media.frame(demo, value, tmp_path / "invalid.png")


def test_media_inputs_must_be_local_files() -> None:
    with pytest.raises(ValueError, match="local file"):
        media.probe("https://example.invalid/video.mp4")


def test_local_playlist_demuxers_cannot_open_nested_files(demo: Path, tmp_path: Path) -> None:
    import av
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    segment = tmp_path / "segment.ts"
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(demo),
            "-c",
            "copy",
            "-f",
            "mpegts",
            str(segment),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    assert result.returncode == 0 and segment.is_file()

    nested_video = tmp_path / "nested.mp4"
    nested_video.write_bytes(demo.read_bytes())
    concat = tmp_path / "local.ffconcat"
    concat.write_text(
        "ffconcat version 1.0\nfile 'nested.mp4'\n",
        encoding="utf-8",
    )
    hls = tmp_path / "local.m3u8"
    hls.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:12\n"
        "#EXTINF:12.000,\nsegment.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )

    # Confirm the manifests are otherwise valid and would expose their
    # generated nested media without the production demuxer allowlist.
    for playlist in (concat, hls):
        with av.open(str(playlist), mode="r") as container:
            assert any(stream.type == "video" for stream in container.streams)

    # Both manifests point only at test-generated media. The format allowlist
    # must reject their demuxers before either referenced file is opened.
    with pytest.raises(media.MediaError):
        media.probe(concat)
    with pytest.raises(media.MediaError):
        media.probe(hls)


def test_segment_bounds_are_checked_before_ffmpeg(demo: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="segment"):
        media.extract_audio(demo, -1, 2, tmp_path / "invalid.wav")
    with pytest.raises(ValueError, match="segment"):
        media.clip(demo, 4, 4, tmp_path / "invalid.mp4")
