import json
from pathlib import Path

import pytest
from PIL import Image

from codex_video import pipeline
from codex_video import subtitles


@pytest.fixture
def source(tmp_path, monkeypatch):
    video = tmp_path / "private-name.mp4"
    video.write_bytes(b"synthetic test media")
    monkeypatch.setattr(pipeline.media, "probe", lambda p: {"duration": 12.0, "has_audio": False,
                                                          "width": 20, "height": 20})
    calls = []
    def frame(p, t, out):
        calls.append(t)
        Image.new("RGB", (20, 20), (int(t)*10, 40, 60)).save(out)
        return {"path": str(out), "sha256": pipeline.digest(out), "requested_time": t,
                "actual_time": t, "width": 20, "height": 20}
    monkeypatch.setattr(pipeline.media, "frame", frame)
    return video, calls


def test_resume_and_tamper_detected(source, tmp_path):
    video, calls = source
    root = tmp_path / "run"
    first = pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2, max_segments=1)
    assert [s["status"] for s in first["segments"]] == ["prepared", "pending", "pending"]
    final = pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    assert calls == [0, 2, 4, 6, 8, 10]
    pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    assert calls == [0, 2, 4, 6, 8, 10]
    assert [s["attempts"] for s in final["segments"]] == [1, 1, 1]
    assert "private-name" not in (root / "manifest.json").read_text()
    artifact = pipeline.artifact(root, final["segments"][0]["frames"][0]["path"])
    artifact.write_bytes(b"changed evidence")
    with pytest.raises(ValueError, match="Evidence"):
        pipeline.read(root)


def test_source_and_settings_change_rejected(source, tmp_path):
    video, _ = source
    root = tmp_path / "run"
    pipeline.prepare(video, root, segment_seconds=4)
    with pytest.raises(ValueError, match="changed"):
        pipeline.prepare(video, root, segment_seconds=6)
    video.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="changed"):
        pipeline.prepare(video, root, segment_seconds=4)


def test_failed_segment_retry_and_no_false_coverage(source, tmp_path, monkeypatch):
    video, calls = source
    good = pipeline.media.frame
    def fail(p, t, out):
        if 4 <= t < 8:
            raise OSError("sensitive path must not be saved")
        return good(p, t, out)
    monkeypatch.setattr(pipeline.media, "frame", fail)
    root = tmp_path / "run"
    m = pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    assert [s["status"] for s in m["segments"]] == ["prepared", "failed", "prepared"]
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["unprepared_intervals"] == [{"start": 4, "end": 8, "status": "failed"}]
    assert coverage["analysis_result_segments"] == []
    assert "sensitive" not in (root / "manifest.json").read_text()
    monkeypatch.setattr(pipeline.media, "frame", good)
    m = pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    assert [s["attempts"] for s in m["segments"]] == [1, 2, 1]
    assert calls == [0, 2, 8, 10, 4, 6]


def test_analysis_resume_mock_not_semantic(source, tmp_path, monkeypatch):
    from codex_video import providers
    video, _ = source
    root = tmp_path / "run"
    pipeline.prepare(video, root, segment_seconds=4)
    original = providers.analyze
    calls = []
    def counted(segment, *a, **kw):
        calls.append(segment["id"])
        return original(segment, *a, **kw)
    monkeypatch.setattr(providers, "analyze", counted)
    pipeline.analyze_run(root, max_segments=1)
    pipeline.analyze_run(root)
    pipeline.analyze_run(root)
    assert calls == ["s0000", "s0001", "s0002"]
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["analysis_result_segments"] == []
    assert len(coverage["unanalyzed_intervals"]) == 3
    assert "MOCK" in (root / "report.md").read_text(encoding="utf-8")


def test_manual_review_cannot_cite_unviewed_frame(source, tmp_path):
    from codex_video.providers import ProviderError
    video, _ = source
    root = tmp_path / "run"
    pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    result = {"summary": "Declared review", "observations": [{"kind": "uncertain", "start": 2.0,
        "end": 2.0, "statement": "A sample only", "evidence_ids": ["s0000.f0001"], "confidence": "low"}],
              "limitations": ["Caller declaration, not independently verified"]}
    p = tmp_path / "review.json"
    p.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ProviderError):
        pipeline.review(root, "s0000", p, ["s0000.f0000"])
    m = pipeline.review(root, "s0000", p, ["s0000.f0001"])
    assert m["segments"][0]["analysis"]["submitted_evidence"][0]["id"] == "s0000.f0001"
    assert "not independently verified" in m["segments"][0]["analysis"]["review_declaration"]
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["analysis_result_segments"][0]["scope"] == "image_samples_and_text_only"
    assert len(coverage["submitted_visual_points"]) == 1
    assert coverage["native_submitted_intervals"] == []
    assert "原生视频提交了整段字节" not in (root / "report.md").read_text(encoding="utf-8")


def test_browser_import_gaps_drift_redaction(tmp_path):
    Image.new("RGB", (20, 20), "red").save(tmp_path / "capture.png")
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"duration": 12, "source_url": "https://example.org/secret-path?token=secret#fragment",
        "frames": [{"path": "capture.png", "requested_time": 2, "actual_time": 2.5}], "transcript": []}))
    root = tmp_path / "run"
    m = pipeline.browser_import(capture, root, segment_seconds=4)
    assert [s["status"] for s in m["segments"]] == ["prepared", "missing", "missing"]
    assert m["segments"][0]["frames"][0]["drift"] == 0.5
    manifest = (root / "manifest.json").read_text()
    assert "secret" not in manifest and "fragment" not in manifest
    pipeline.browser_import(capture, root, segment_seconds=4)
    assert pipeline.read(root)["segments"][0]["attempts"] == 1
    data = json.loads(capture.read_text())
    data["frames"][0]["path"] = "../outside.png"
    capture.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="within"):
        pipeline.browser_import(capture, tmp_path / "another")


def test_lock_and_path_escape(tmp_path):
    with pipeline.lock(tmp_path):
        with pytest.raises(ValueError, match="locked"):
            with pipeline.lock(tmp_path):
                pass
    assert not (tmp_path / ".run.lock").exists()
    with pytest.raises(ValueError):
        pipeline.artifact(tmp_path, "../escape")


def test_subtitle_media_time_parsing(tmp_path):
    p = tmp_path / "demo.vtt"
    p.write_text("WEBVTT\n\n00:01.500 --> 00:03.000 align:start\nClaim only\n", encoding="utf-8")
    spans = subtitles.load(p, 4)
    assert spans[0]["start"] == 1.5 and spans[0]["source"] == "supplied_subtitle"
    with pytest.raises(ValueError):
        subtitles.normalize([{"start": 0, "end": float("nan"), "text": "bad"}], 4)
    with pytest.raises(ValueError):
        subtitles.timestamp("00:60.000")


def test_manifest_plan_and_source_tamper(source, tmp_path):
    video, _ = source
    root = tmp_path / "run"
    pipeline.prepare(video, root, segment_seconds=4)
    p = root / "manifest.json"
    original = json.loads(p.read_text(encoding="utf-8"))
    m = json.loads(p.read_text(encoding="utf-8"))
    m["source"]["duration"] = 15
    pipeline.save(p, m)
    with pytest.raises(ValueError, match="metadata"):
        pipeline.read(root)
    pipeline.save(p, original)
    original["segments"].pop()
    pipeline.save(p, original)
    with pytest.raises(ValueError, match="plan"):
        pipeline.read(root)


def test_partial_sample_failure_recorded(source, tmp_path, monkeypatch):
    video, _ = source
    good = pipeline.media.frame
    def missing(p, t, out):
        if t == 2:
            return {"path": None, "actual_time": None}
        return good(p, t, out)
    monkeypatch.setattr(pipeline.media, "frame", missing)
    root = tmp_path / "run"
    m = pipeline.prepare(video, root, segment_seconds=4, sample_seconds=2)
    assert m["segments"][0]["status"] == "prepared"
    coverage = json.loads((root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["failed_visual_samples"] == [{"segment": "s0000", "requested_time": 2.0, "error": "NoFrameInInterval"}]
    assert 2 not in [s["time"] for s in coverage["visual_sample_points"]]


def test_report_untrusted_markdown_escaped(source, tmp_path):
    video, _ = source
    root = tmp_path / "run"
    pipeline.prepare(video, root, segment_seconds=4)
    result = {"summary": "![tracking](https://example.org/pixel)<script>alert(1)</script>",
              "observations": [], "limitations": ["<img src='https://example.org/pixel'>"]}
    p = tmp_path / "review.json"
    pipeline.save(p, result)
    pipeline.review(root, "s0000", p, ["s0000.f0000"])
    text = (root / "report.md").read_text(encoding="utf-8")
    assert "![tracking](" not in text and "<script>" not in text and "<img" not in text
    assert "&lt;script&gt;" in text
