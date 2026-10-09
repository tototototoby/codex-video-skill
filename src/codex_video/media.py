"""Local media inspection and deterministic media operations.

All input media is opened as a local file. Video timestamps returned by
``frame`` are normalized so the first presentation timestamp in the video
stream is zero.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class MediaError(RuntimeError):
    """Raised when local media cannot be safely inspected or processed."""


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_TIME_EPSILON = 1e-7
_FORMAT_WHITELIST = "mov,matroska,webm,avi,mpegts,ogg,flv,asf,nut"
_LOCAL_FORMAT_OPTIONS = {
    "protocol_whitelist": "file,pipe",
    "format_whitelist": _FORMAT_WHITELIST,
}


def _is_uri(value: str) -> bool:
    return bool(_URI_SCHEME.match(value) and not _WINDOWS_DRIVE.match(value))


def _local_input(path: os.PathLike[str] | str) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or _is_uri(raw):
        raise ValueError("media input must be a local file path")
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError("media input file does not exist") from exc
    if not resolved.is_file():
        raise ValueError("media input must be a regular local file")
    return resolved


def _local_output(path: os.PathLike[str] | str) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or _is_uri(raw):
        raise ValueError("media output must be a local file path")
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("media output path is not writable") from exc
    if resolved.exists() and not resolved.is_file():
        raise ValueError("media output must name a file")
    return resolved


def _ensure_distinct(source: Path, destination: Path) -> None:
    try:
        same_file = destination.exists() and os.path.samefile(source, destination)
    except OSError:
        same_file = False
    if source == destination or same_file:
        raise ValueError("media output must not overwrite the input file")


def _pyav() -> Any:
    try:
        import av  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MediaError("PyAV is required for media inspection and frame extraction") from exc
    return av


def _video_stream(container: Any) -> Any:
    stream = next((s for s in container.streams if s.type == "video"), None)
    if stream is None:
        raise MediaError("media file contains no video stream")
    return stream


def _stream_origin(stream: Any, container: Any) -> float:
    time_base = float(stream.time_base) if stream.time_base is not None else 0.0
    if time_base <= 0 or not math.isfinite(time_base):
        raise MediaError("video stream has no usable time base")
    start_time = stream.start_time
    if start_time is not None:
        origin = float(start_time) * time_base
        if math.isfinite(origin):
            return origin
    try:
        first = next(container.decode(video=0))
    except StopIteration as exc:
        raise MediaError("media file contains no decodable video frames") from exc
    if first.pts is None:
        return 0.0
    origin = float(first.pts) * time_base
    if not math.isfinite(origin):
        raise MediaError("video stream has no usable presentation timestamps")
    return origin


def _stream_duration(stream: Any) -> float | None:
    try:
        duration = stream.duration
        time_base = stream.time_base
        if duration is not None and time_base is not None:
            seconds = float(duration) * float(time_base)
            if math.isfinite(seconds) and seconds > 0:
                return seconds
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _video_duration(container: Any, video_stream: Any, origin: float) -> float:
    seconds = _stream_duration(video_stream)
    if seconds is None:
        # A few demuxers omit stream duration metadata. Scan video frames only
        # in that case; duration remains relative to the first video PTS.
        try:
            seek_pts = int(origin / float(video_stream.time_base))
            container.seek(seek_pts, stream=video_stream, backward=True, any_frame=False)
        except Exception:
            try:
                container.seek(0)
            except Exception:
                pass
        last_end: float | None = None
        time_base = float(video_stream.time_base)
        for decoded in container.decode(video=0):
            if decoded.pts is None:
                continue
            end = float(decoded.pts) * time_base
            frame_duration = getattr(decoded, "duration", None)
            if frame_duration:
                end += float(frame_duration) * time_base
            elif video_stream.average_rate:
                end += 1.0 / float(video_stream.average_rate)
            last_end = end
        if last_end is None:
            raise MediaError("media file has no decodable video duration")
        seconds = last_end - origin
    if not math.isfinite(seconds) or seconds <= 0:
        raise MediaError("media file has an invalid or non-positive duration")
    return float(seconds)


def _container_origin(av: Any, container: Any, video_origin: float) -> float:
    start_time = container.start_time
    if start_time is not None:
        try:
            value = float(start_time) / float(av.time_base)
            if math.isfinite(value):
                return value
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            pass
    starts = []
    for stream in container.streams:
        if stream.type not in ("video", "audio") or stream.start_time is None or stream.time_base is None:
            continue
        try:
            value = float(stream.start_time) * float(stream.time_base)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            starts.append(value)
    return min(starts) if starts else video_origin


def _probe_details(source: Path) -> dict[str, Any]:
    av = _pyav()
    try:
        with av.open(
            str(source), mode="r", options=dict(_LOCAL_FORMAT_OPTIONS)
        ) as container:
            video = _video_stream(container)
            width = int(video.codec_context.width or video.width or 0)
            height = int(video.codec_context.height or video.height or 0)
            if width <= 0 or height <= 0:
                raise MediaError("video stream has invalid dimensions")
            origin = _stream_origin(video, container)
            duration = _video_duration(container, video, origin)
            container_origin = _container_origin(av, container, origin)
            has_audio = any(s.type == "audio" for s in container.streams)
            return {
                "duration": duration,
                "has_audio": has_audio,
                "width": width,
                "height": height,
                "time_origin": origin,
                "ffmpeg_seek_offset": max(0.0, origin - container_origin),
            }
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("could not read local media metadata") from exc


def probe(path: Path, ffmpeg: str | None = None) -> dict[str, Any]:
    """Return video metadata on a timeline whose first video PTS is zero.

    ``ffmpeg`` is accepted for API symmetry and future probing backends. PyAV
    reads the container directly, so no subprocess is started by this function.
    ``time_origin`` preserves the first video PTS in the container's timestamp
    units converted to seconds; ``duration`` is relative to that origin.
    """
    del ffmpeg
    source = _local_input(path)
    details = _probe_details(source)
    return {
        "duration": details["duration"],
        "has_audio": details["has_audio"],
        "width": details["width"],
        "height": details["height"],
        "time_origin": details["time_origin"],
    }


def _resolve_ffmpeg(ffmpeg: str | None = None) -> str:
    choices = [ffmpeg, os.environ.get("FFMPEG_BIN")]
    for choice in choices:
        if not choice:
            continue
        if _is_uri(choice):
            raise ValueError("FFmpeg executable must be a local path or command name")
        found = shutil.which(choice)
        if found:
            return found
        candidate = Path(choice).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).is_file():
            return str(Path(executable).resolve())
    except Exception:
        pass
    raise MediaError("FFmpeg was not found in FFMPEG_BIN, PATH, or imageio-ffmpeg")


def _run_ffmpeg(command: list[str], *, timeout: float, operation: str) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"FFmpeg timed out while {operation}") from exc
    except OSError as exc:
        raise MediaError(f"could not start FFmpeg while {operation}") from exc
    if result.returncode != 0:
        raise MediaError(f"FFmpeg failed while {operation} (exit {result.returncode})")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _temp_output(destination: Path) -> Path:
    fd, name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=destination.suffix, dir=str(destination.parent)
    )
    os.close(fd)
    return Path(name)


def _publish(temp: Path, destination: Path) -> None:
    if not temp.is_file() or temp.stat().st_size == 0:
        raise MediaError("media operation produced an empty output file")
    os.replace(temp, destination)


def _finite_seconds(value: float, name: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number of seconds") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"{name} must be a finite number of seconds")
    return seconds


def frame(path: Path, at_seconds: float, dest: Path) -> dict[str, Any]:
    """Save the earliest video frame whose normalized PTS is at/after a time.

    Returned ``actual_time`` is relative to the first video presentation PTS.
    """
    source = _local_input(path)
    destination = _local_output(dest)
    _ensure_distinct(source, destination)
    requested = _finite_seconds(at_seconds, "at_seconds")
    if requested < 0:
        raise ValueError("at_seconds must be zero or greater")
    metadata = probe(source)
    av = _pyav()
    selected = None
    selected_time: float | None = None
    try:
        with av.open(
            str(source), mode="r", options=dict(_LOCAL_FORMAT_OPTIONS)
        ) as container:
            video = _video_stream(container)
            time_base = float(video.time_base)
            origin = _stream_origin(video, container)
            if requested < metadata["duration"]:
                if requested > 0:
                    seek_pts = int((origin + requested) / time_base)
                    try:
                        container.seek(max(seek_pts, 0), stream=video, backward=True, any_frame=False)
                    except Exception:
                        # Some demuxers do not support seeking; decoding from the
                        # beginning still gives the same earliest-at-or-after rule.
                        try:
                            container.seek(0)
                        except Exception:
                            pass
                for decoded in container.decode(video=0):
                    if decoded.pts is None:
                        continue
                    timestamp = float(decoded.pts) * time_base - origin
                    if timestamp + _TIME_EPSILON >= requested:
                        selected = decoded
                        selected_time = max(0.0, timestamp) if abs(timestamp) < 1e-9 else timestamp
                        break
        if selected is None:
            return {
                "requested_time": requested,
                "actual_time": None,
                "path": None,
                "sha256": None,
                "width": metadata["width"],
                "height": metadata["height"],
            }
        temp = _temp_output(destination)
        try:
            selected.to_image().save(str(temp), format="PNG")
            _publish(temp, destination)
        finally:
            if temp.exists():
                temp.unlink()
        return {
            "requested_time": requested,
            "actual_time": float(selected_time),
            "path": str(destination),
            "sha256": _sha256(destination),
            "width": int(selected.width),
            "height": int(selected.height),
        }
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError("could not decode a frame from local media") from exc


def _validate_segment(
    path: Path, start: float, end: float
) -> tuple[float, float, float, float]:
    start_value = _finite_seconds(start, "start")
    end_value = _finite_seconds(end, "end")
    metadata = _probe_details(path)
    duration = metadata["duration"]
    if start_value < 0 or end_value <= start_value or end_value > duration + 1e-6:
        raise ValueError("segment must satisfy 0 <= start < end <= media duration")
    return start_value, end_value, duration, metadata["ffmpeg_seek_offset"]


def extract_audio(
    path: Path, start: float, end: float, dest: Path, ffmpeg: str | None = None
) -> dict[str, Any]:
    """Extract video-timeline seconds as mono 16 kHz signed 16-bit PCM WAV."""
    source = _local_input(path)
    destination = _local_output(dest)
    _ensure_distinct(source, destination)
    start_value, end_value, _, seek_offset = _validate_segment(source, start, end)
    executable = _resolve_ffmpeg(ffmpeg)
    temp = _temp_output(destination)
    try:
        _run_ffmpeg(
            [
                executable,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-format_whitelist",
                _FORMAT_WHITELIST,
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(source),
                "-ss",
                f"{seek_offset + start_value:.9f}",
                "-t",
                f"{end_value - start_value:.9f}",
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "aresample=async=1:first_pts=0",
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(temp),
            ],
            timeout=180,
            operation="extracting audio",
        )
        _publish(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()
    return {
        "start": start_value,
        "end": end_value,
        "path": str(destination),
        "sha256": _sha256(destination),
    }


def clip(
    path: Path, start: float, end: float, dest: Path, ffmpeg: str | None = None
) -> dict[str, Any]:
    """Transcode a local segment to H.264/AAC MP4 (video-only when needed)."""
    source = _local_input(path)
    destination = _local_output(dest)
    _ensure_distinct(source, destination)
    start_value, end_value, _, seek_offset = _validate_segment(source, start, end)
    executable = _resolve_ffmpeg(ffmpeg)
    temp = _temp_output(destination)
    try:
        _run_ffmpeg(
            [
                executable,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-format_whitelist",
                _FORMAT_WHITELIST,
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(source),
                "-ss",
                f"{seek_offset + start_value:.9f}",
                "-t",
                f"{end_value - start_value:.9f}",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                str(temp),
            ],
            timeout=600,
            operation="creating a video clip",
        )
        _publish(temp, destination)
    finally:
        if temp.exists():
            temp.unlink()
    return {
        "start": start_value,
        "end": end_value,
        "path": str(destination),
        "sha256": _sha256(destination),
    }
