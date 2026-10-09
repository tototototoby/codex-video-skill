"""Durable local evidence runs. A planned interval is never visual coverage."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import html
import io
import json
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import media, subtitles


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def save(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def atomic_text(path, text):
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lockfile = root / ".run.lock"
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("Run is locked. Only remove .run.lock after verifying no run is active.") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lockfile.unlink(missing_ok=True)


def artifact(root: Path, relative: str) -> Path:
    p = (root / relative).resolve()
    if Path(relative).is_absolute() or not p.is_relative_to(root.resolve()):
        raise ValueError("Artifact must stay within run directory")
    return p


def artifacts(segment):
    return [*segment.get("frames", []), *[segment[k] for k in ("audio", "clip") if segment.get(k)]]


def verify(root: Path, segment):
    for item in artifacts(segment):
        p = artifact(root, item["path"])
        if not p.is_file() or digest(p) != item["sha256"]:
            raise ValueError("Evidence is missing or changed; use a new run directory")
    if segment.get("input_hash") != fingerprint({k: segment.get(k) for k in
                                                  ("id", "start", "end", "frames", "sample_failures", "audio", "clip", "transcript")}):
        raise ValueError("Segment metadata changed; use a new run directory")


def seal(segment):
    segment["input_hash"] = fingerprint({k: segment.get(k) for k in
                                         ("id", "start", "end", "frames", "sample_failures", "audio", "clip", "transcript")})


def plan(source, settings, transcript):
    seconds = subtitles.number(settings["segment_seconds"])
    if seconds <= 0:
        raise ValueError("segment_seconds must be positive")
    duration = source["duration"]
    count = math.ceil(duration / seconds)
    if count > 100000:
        raise ValueError("Too many segments")
    segments = []
    for i in range(count):
        start, end = i * seconds, min((i + 1) * seconds, duration)
        segments.append({"id": f"s{i:04d}", "start": start, "end": end, "status": "pending",
                         "attempts": 0, "frames": [],
                         "transcript": [t for t in transcript if t["end"] > start and t["start"] < end]})
    return {"schema_version": 1, "source": source, "settings": settings, "initial_transcript": transcript,
            "run_fingerprint": fingerprint({"source": source, "settings": settings, "transcript": transcript}),
            "segments": segments}


def existing(root, candidate):
    p = root / "manifest.json"
    if p.exists():
        result = read(root)
        if result.get("run_fingerprint") != candidate["run_fingerprint"]:
            raise ValueError("Source/settings changed; use a new run directory")
        return result
    if any(p.name != ".run.lock" for p in root.iterdir()):
        raise ValueError("New run directory must be empty")
    save(p, candidate)
    return candidate


def relative(root, item):
    item = dict(item)
    item["path"] = str(Path(item["path"]).resolve().relative_to(root.resolve())).replace("\\", "/")
    return item


def prepare(video: Path, root: Path, *, segment_seconds=60.0, sample_seconds=10.0,
            subtitle_path=None, audio=False, transcribe=False, whisper_model="small",
            device="cpu", native_clips=False, max_segments=None):
    interval = subtitles.number(sample_seconds)
    if interval <= 0:
        raise ValueError("sample_seconds must be positive")
    info = media.probe(video)
    transcript = subtitles.load(subtitle_path, info["duration"]) if subtitle_path else []
    if transcribe and subtitle_path:
        raise ValueError("Use either supplied subtitles or ASR per run")
    if transcribe and not info["has_audio"]:
        raise ValueError("Source has no audio")
    settings = {"segment_seconds": segment_seconds, "sample_seconds": interval,
                "audio": bool(audio or transcribe), "transcribe": transcribe,
                "whisper_model": whisper_model if transcribe else None,
                "device": device if transcribe else None, "native_clips": native_clips,
                "subtitle_sha256": digest(subtitle_path) if subtitle_path else None}
    source = {"kind": "local", "sha256": digest(video), **info}
    # Persist no original absolute path, filenames, browser credentials, or keys.
    source = {k: v for k, v in source.items() if k in ("kind", "sha256", "duration", "has_audio", "width", "height", "time_origin")}
    asr_model = None
    with lock(root):
        manifest = existing(root, plan(source, settings, transcript))
        processed = 0
        for segment in manifest["segments"]:
            if segment["status"] == "prepared":
                verify(root, segment)
                continue
            if max_segments is not None and processed >= max_segments:
                break
            processed += 1
            segment["attempts"] += 1
            segment["status"] = "preparing"
            save(root / "manifest.json", manifest)
            try:
                folder = artifact(root, f"segments/{segment['id']}")
                folder.mkdir(parents=True, exist_ok=True)
                segment["frames"] = []
                segment["sample_failures"] = []
                n = math.ceil((segment["end"] - segment["start"]) / interval)
                if n > 10000:
                    raise ValueError("Too many samples per segment")
                for i in range(n):
                    at = segment["start"] + i * interval
                    try:
                        raw = media.frame(video, at, artifact(root, f"segments/{segment['id']}/frame_{i:04d}.png"))
                        if not raw.get("path") or raw.get("actual_time") is None or raw["actual_time"] >= segment["end"]:
                            segment["sample_failures"].append({"requested_time": at, "error": "NoFrameInInterval"})
                            continue
                        item = relative(root, raw)
                    except Exception as exc:
                        segment["sample_failures"].append({"requested_time": at, "error": type(exc).__name__})
                        continue
                    item["id"] = f"{segment['id']}.f{i:04d}"
                    segment["frames"].append(item)
                if not segment["frames"]:
                    raise ValueError("No frame available in segment")
                if settings["audio"] and info["has_audio"]:
                    segment["audio"] = relative(root, media.extract_audio(video, segment["start"], segment["end"], artifact(root, f"segments/{segment['id']}/audio.wav")))
                if native_clips:
                    segment["clip"] = relative(root, media.clip(video, segment["start"], segment["end"], artifact(root, f"segments/{segment['id']}/clip.mp4")))
                    segment["clip"]["id"] = f"{segment['id']}.clip"
                if transcribe:
                    if asr_model is None:
                        from faster_whisper import WhisperModel
                        # This explicit operation may download the selected public model.
                        asr_model = WhisperModel(whisper_model, device=device,
                                                 compute_type="int8" if device == "cpu" else "float16")
                    spans, _ = asr_model.transcribe(str(artifact(root, segment["audio"]["path"])), vad_filter=True)
                    items = [{"start": segment["start"] + s.start,
                              "end": min(segment["end"], segment["start"] + s.end), "text": s.text}
                             for s in spans if s.text.strip() and s.end > s.start]
                    segment["transcript"] = subtitles.normalize(items, info["duration"], "local_asr")
                    for item in segment["transcript"]:
                        item["id"] = segment["id"] + "." + item["id"]
                    segment["asr_processed"] = True
                seal(segment)
                segment["status"] = "prepared"
                segment.pop("error", None)
            except Exception as exc:
                segment["status"] = "failed"
                segment["error"] = type(exc).__name__  # No raw media paths, provider bodies or secrets.
            save(root / "manifest.json", manifest)
        write_report(root, manifest)
        return manifest


def read(root):
    m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if m.get("schema_version") != 1:
        raise ValueError("Unsupported manifest version")
    identity = fingerprint({"source": m["source"], "settings": m["settings"],
                            "transcript": m["initial_transcript"]})
    if m.get("run_fingerprint") != identity:
        raise ValueError("Run metadata changed")
    expected = plan(m["source"], m["settings"], m["initial_transcript"])["segments"]
    if [(s["id"], s["start"], s["end"]) for s in m["segments"]] != [(s["id"], s["start"], s["end"]) for s in expected]:
        raise ValueError("Run segment plan changed")
    for s in m["segments"]:
        if s["status"] == "prepared":
            verify(root, s)
    return m


def analyze_run(root: Path, *, provider="mock", model=None, base_url=None, key_env="VIDEO_API_KEY",
                allow_upload=False, question="Review this video with source evidence.", max_segments=None):
    from .providers import analyze
    with lock(root):
        manifest = read(root)
        processed = 0
        for segment in manifest["segments"]:
            if segment["status"] != "prepared":
                continue
            settings = {"provider": provider, "model": model, "base_url": base_url,
                        "key_env": key_env, "question": question, "input_hash": segment["input_hash"]}
            identity = fingerprint(settings)
            previous = segment.get("analysis", {})
            if previous.get("status") == "done":
                if previous.get("fingerprint") != identity:
                    raise ValueError("Analysis settings changed; use another run or an explicit manual review")
                check_result(root, segment)
                continue
            if max_segments is not None and processed >= max_segments:
                break
            processed += 1
            segment["analysis"] = {"status": "running", "fingerprint": identity,
                                   "attempts": previous.get("attempts", 0) + 1, "provider": provider,
                                   "model": model, "question": question}
            save(root / "manifest.json", manifest)
            try:
                result = analyze(segment, root, provider=provider, model=model, base_url=base_url,
                                 key_env=key_env, allow_upload=allow_upload, question=question)
                submitted = ([segment["clip"]] if provider == "gemini" else segment["frames"]) if provider != "mock" else []
                finish(root, segment, result, submitted, provider != "mock")
            except Exception as exc:
                segment["analysis"]["status"] = "failed"
                segment["analysis"]["error"] = type(exc).__name__
            save(root / "manifest.json", manifest)
        write_report(root, manifest)
        return manifest


def finish(root, segment, result, submitted, semantic):
    p = artifact(root, f"segments/{segment['id']}/analysis.json")
    save(p, result)
    segment["analysis"].update({"status": "done", "semantic_analysis": semantic,
        "path": p.relative_to(root.resolve()).as_posix(), "sha256": digest(p),
        "submitted_evidence": [{"id": x["id"], "sha256": x["sha256"]} for x in submitted],
        "transcript_ids": [x["id"] for x in segment["transcript"] if segment["analysis"].get("provider") != "codex-manual"
                           or any(x["id"] in c["evidence_ids"] for c in result["observations"])]})


def check_result(root, segment):
    a = segment["analysis"]
    p = artifact(root, a["path"])
    if not p.is_file() or digest(p) != a["sha256"]:
        raise ValueError("Analysis artifact is missing or changed")
    return json.loads(p.read_text(encoding="utf-8"))


def review(root, segment_id, result_path, viewed, reviewer="codex"):
    from .providers import validate_result
    with lock(root):
        m = read(root)
        segment = next((s for s in m["segments"] if s["id"] == segment_id), None)
        if segment is None or segment["status"] != "prepared":
            raise ValueError("Segment is not prepared")
        frames = [f for f in segment["frames"] if f["id"] in viewed]
        if set(viewed) != {f["id"] for f in frames}:
            raise ValueError("Viewed IDs must be real frame evidence IDs")
        permitted = dict(segment, frames=frames)
        permitted.pop("clip", None)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        # A declaration is an audit record; this command cannot verify a human/model actually viewed images.
        result = validate_result(result, permitted)
        segment["analysis"] = {"status": "running", "provider": "codex-manual", "model": reviewer,
                               "fingerprint": fingerprint({"input": segment["input_hash"], "result": result}),
                               "review_declaration": "Caller declares listed images were viewed; not independently verified"}
        finish(root, segment, result, frames, True)
        save(root / "manifest.json", m)
        write_report(root, m)
        return m


def browser_import(capture: Path, root: Path, *, segment_seconds=60):
    """Consume authorized externally captured evidence, never drive a browser."""
    from PIL import Image
    data = json.loads(capture.read_text(encoding="utf-8"))
    duration = subtitles.number(data["duration"])
    if duration <= 0:
        raise ValueError("Browser duration must be positive")
    entries = data["frames"]
    if not entries:
        raise ValueError("No browser frames")
    inputs = []
    for i, entry in enumerate(entries):
        p = artifact(capture.parent, entry["path"])
        actual = subtitles.number(entry["actual_time"])
        requested = subtitles.number(entry["requested_time"])
        if actual >= duration or requested >= duration:
            raise ValueError("Browser sample time exceeds duration")
        inputs.append({"id": f"b.f{i:06d}", "actual_time": actual, "requested_time": requested,
                       "drift": actual - requested, "sha256": digest(p), "path": entry["path"]})
    transcript = subtitles.normalize(data.get("transcript", []), duration, "browser_subtitle")
    source = {"kind": "browser_import", "duration": duration, "has_audio": False,
              "capture_fingerprint": fingerprint(inputs),
              "time_origin": "externally observed media time; not independently verified"}
    if data.get("source_url"):
        url = urlsplit(data["source_url"])
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
            raise ValueError("Invalid browser source URL")
        # Even URL paths can embed tokens. Persist only the origin.
        source["origin"] = f"{url.scheme}://{url.hostname}"
    with lock(root):
        m = existing(root, plan(source, {"segment_seconds": segment_seconds, "mode": "browser_import"}, transcript))
        for s in m["segments"]:
            if s["status"] == "prepared":
                verify(root, s)
                continue
            s["attempts"] += 1
            folder = artifact(root, f"segments/{s['id']}")
            folder.mkdir(parents=True, exist_ok=True)
            s["frames"] = []
            for item in inputs:
                if s["start"] <= item["actual_time"] < s["end"]:
                    dest = artifact(root, f"segments/{s['id']}/{item['id']}.png")
                    snapshot = artifact(capture.parent, item["path"]).read_bytes()
                    if hashlib.sha256(snapshot).hexdigest() != item["sha256"]:
                        raise ValueError("Browser source image changed during import")
                    with Image.open(io.BytesIO(snapshot)) as image:
                        image.convert("RGB").save(dest)
                    s["frames"].append({**item, "source_sha256": item["sha256"],
                                        "path": dest.relative_to(root.resolve()).as_posix(), "sha256": digest(dest)})
            if s["frames"]:
                s["status"] = "prepared"
                seal(s)
            else:
                s["status"] = "missing"
                s["error"] = "NoCaptureInInterval"
        save(root / "manifest.json", m)
        write_report(root, m)
        return m


def timecode(t):
    ms = round(t * 1000)
    return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d}.{ms%1000:03d}"


def safe_text(value):
    """Keep untrusted model/media text from becoming HTML or remote Markdown assets."""
    text = html.escape(str(value), quote=True)
    for symbol in ("\\", "`", "*", "_", "[", "]", "!", "#", "(", ")"):
        text = text.replace(symbol, "\\" + symbol)
    return text


def write_report(root, m):
    segments = m["segments"]
    coverage = {"duration": m["source"]["duration"],
        "prepared_intervals": [[s["start"], s["end"]] for s in segments if s["status"] == "prepared"],
        "visual_sample_points": [{"id": f["id"], "time": f["actual_time"]} for s in segments if s["status"] == "prepared" for f in s["frames"]],
        "failed_visual_samples": [{"segment": s["id"], **f} for s in segments for f in s.get("sample_failures", [])],
        "analysis_result_segments": [{"id": s["id"], "source_start": s["start"], "source_end": s["end"],
                                      "provider": s["analysis"]["provider"], "scope": "native_provider_sampling_unknown" if s["analysis"]["provider"] == "gemini" else "image_samples_and_text_only"}
                                     for s in segments if s.get("analysis", {}).get("status") == "done" and s["analysis"].get("semantic_analysis")],
        "submitted_visual_points": [{"id": f["id"], "time": f["actual_time"], "provider": s["analysis"]["provider"]}
            for s in segments if s.get("analysis", {}).get("status") == "done"
            for f in s["frames"] if f["id"] in {e["id"] for e in s["analysis"].get("submitted_evidence", [])}],
        "native_submitted_intervals": [[s["start"], s["end"]] for s in segments if s.get("analysis", {}).get("status") == "done" and s["analysis"].get("provider") == "gemini"],
        "asr_processed_intervals": [[s["start"], s["end"]] for s in segments if s["status"] == "prepared" and s.get("asr_processed")],
        "subtitle_spans": list({(t["id"], t["start"], t["end"]): {"id": t["id"], "start": t["start"], "end": t["end"], "source": t["source"]} for s in segments for t in s["transcript"]}.values()),
        "unprepared_intervals": [{"start": s["start"], "end": s["end"], "status": s["status"]} for s in segments if s["status"] != "prepared"],
        "unanalyzed_intervals": [{"start": s["start"], "end": s["end"], "status": s.get("analysis", {}).get("status", "pending")} for s in segments if not (s.get("analysis", {}).get("status") == "done" and s["analysis"].get("semantic_analysis"))],
        "limitations": ["画面覆盖是离散采样点，采样之间可能遗漏事件；处理整个分段不代表看过每帧。",
                         "字幕存在不代表音轨已核实；ASR 可能漏词或错字。",
                         "若使用原生视频接口，只能确认字节提交；供应商内部采样/音轨处理无法由本工具验证。",
                         "口述、幻灯片、预录和模拟数据不能作为应用实时行为的充分证据。"]}
    save(root / "coverage.json", coverage)
    lines = ["# 视频证据报告", "", f"源类型：{m['source']['kind']}；时长：{timecode(coverage['duration'])}。", "",
             f"已提取分段：{len(coverage['prepared_intervals'])}/{len(segments)}；画面采样点：{len(coverage['visual_sample_points'])}；有审阅结果的来源分段：{len(coverage['analysis_result_segments'])}；已提交/声明查看的画面点：{len(coverage['submitted_visual_points'])}。", "",
             "来源分段范围仅用于定位分析结果，不能当作连续画面覆盖。", "",
             "完整记录见 [manifest.json](manifest.json) 与 [coverage.json](coverage.json)。", "", *coverage["limitations"], ""]
    for s in segments:
        lines += [f"## {s['id']} · {timecode(s['start'])}–{timecode(s['end'])}", "", f"提取状态：{s['status']}；分析状态：{s.get('analysis', {}).get('status', 'pending')}。", ""]
        for f in s["frames"]:
            lines += [f"- [{f['id']} · {timecode(f['actual_time'])}]({f['path']})"]
        lines.append("")
        if s.get("analysis", {}).get("status") == "done":
            result = check_result(root, s)
            if not s["analysis"].get("semantic_analysis"):
                lines += ["**MOCK：仅验证接口与续跑，没有实际内容分析。**", ""]
            lines += ["摘要（模型/审阅者文字，不能作为独立证据）：", "", safe_text(result["summary"]), ""]
            for claim in result["observations"]:
                lines += [f"- `{claim['kind']}` {timecode(claim['start'])}–{timecode(claim['end'])}：{safe_text(claim['statement'])}（证据：{safe_text(', '.join(claim['evidence_ids']))}；置信度：{claim['confidence']}）"]
            lines += ["", *[safe_text(t) for t in result["limitations"]], ""]
    atomic_text(root / "report.md", "\n".join(lines))
    return coverage
