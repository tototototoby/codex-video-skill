import base64
import copy
import hashlib
import json
import math
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_video.providers import ProviderError, analyze, validate_result  # noqa: E402


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
    "vWQAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


@contextmanager
def local_server(response_factory):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, headers, payload = response_factory(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def provider_result(*, evidence="frame-1", start=10.0, end=11.0):
    return {
        "summary": "A person describes the displayed result.",
        "observations": [
            {
                "kind": "spoken_claim",
                "start": start,
                "end": end,
                "statement": "The speaker says the value increased.",
                "evidence_ids": [evidence],
                "confidence": "medium",
            }
        ],
        "limitations": [],
    }


def openai_response(result):
    return json.dumps(
        {"choices": [{"message": {"content": json.dumps(result)}}]}
    ).encode("utf-8")


def gemini_response(result):
    return json.dumps(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(result)}]}}]}
    ).encode("utf-8")


def make_segment(root):
    (root / "frame.png").write_bytes(PNG)
    (root / "segment.mp4").write_bytes(MP4)
    return {
        "id": "seg-1",
        "start": 10.0,
        "end": 15.0,
        "frames": [
            {
                "id": "frame-1",
                "path": "frame.png",
                "actual_time": 10.5,
                "sha256": hashlib.sha256(PNG).hexdigest(),
            }
        ],
        "transcript": [
            {
                "id": "transcript-1",
                "start": 10.1,
                "end": 10.8,
                "text": "Revenue rose from the prior quarter.",
                "source": "asr",
            }
        ],
        "clip": {
            "id": "native-clip-1",
            "path": "segment.mp4",
            "sha256": hashlib.sha256(MP4).hexdigest(),
            "start": 10.0,
            "end": 15.0,
        },
    }


def test_openai_image_mode_sends_real_png_and_transcript_only(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    result_body = openai_response(provider_result())
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, result_body)) as (base, requests):
        monkeypatch.setenv("TEST_PROVIDER_KEY", "local-secret")
        result = analyze(
            segment,
            tmp_path,
            provider="openai",
            model="gpt-test-vision",
            base_url=base + "/v1",
            key_env="TEST_PROVIDER_KEY",
            allow_upload=True,
            question="What does the presenter claim?",
        )

    assert result["summary"] == "A person describes the displayed result."
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer local-secret"
    body = json.loads(request["body"])
    content = body["messages"][0]["content"]
    text = "\n".join(part["text"] for part in content if part["type"] == "text")
    assert "Question: What does the presenter claim?" in text
    assert "transcript-1" in text and "Revenue rose from the prior quarter." in text
    image_part = next(part for part in content if part["type"] == "image_url")
    data_url = image_part["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.partition(",")[2]) == PNG
    assert not any("inline_data" in part for part in content)
    assert "segment.mp4" not in request["body"].decode("utf-8")


def test_gemini_native_mode_sends_real_mp4_and_only_cites_sent_evidence(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    result_body = gemini_response(provider_result(evidence="native-clip-1", start=10.2, end=10.9))
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, result_body)) as (base, requests):
        monkeypatch.setenv("TEST_GEMINI_KEY", "gemini-local-secret")
        result = analyze(
            segment,
            tmp_path,
            provider="gemini",
            model="gemini-test",
            base_url=base + "/v1beta",
            key_env="TEST_GEMINI_KEY",
            allow_upload=True,
            question="Summarize the segment.",
        )

    assert result["observations"][0]["evidence_ids"] == ["native-clip-1"]
    assert len(requests) == 1
    request = requests[0]
    assert request["path"] == "/v1beta/models/gemini-test:generateContent"
    headers = {name.lower(): value for name, value in request["headers"].items()}
    assert headers["x-goog-api-key"] == "gemini-local-secret"
    body = json.loads(request["body"])
    parts = body["contents"][0]["parts"]
    prompt = parts[0]["text"]
    assert "adding 10.000" in prompt
    assert "transcript-1" in prompt
    assert "frame-1" not in prompt
    video = next(part["inline_data"] for part in parts if "inline_data" in part)
    assert video["mime_type"] == "video/mp4"
    assert base64.b64decode(video["data"]) == MP4


def test_mock_is_explicit_and_does_not_need_upload_or_media(tmp_path):
    result = analyze(
        {"start": 0, "end": 1},
        tmp_path,
        provider="mock",
        model=None,
        base_url=None,
        key_env="IGNORED",
        allow_upload=False,
        question="Analyze this.",
    )

    assert result["summary"].startswith("MOCK")
    assert result["observations"] == []
    assert result["limitations"]


def test_upload_disabled_fails_before_any_request(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, b"{}")) as (base, requests):
        monkeypatch.setenv("TEST_PROVIDER_KEY", "local-secret")
        with pytest.raises(ProviderError, match="upload is disabled"):
            analyze(
                segment,
                tmp_path,
                provider="openai",
                model="gpt-test-vision",
                base_url=base,
                key_env="TEST_PROVIDER_KEY",
                allow_upload=False,
                question="Analyze this.",
            )
    assert requests == []


def test_provider_redirect_is_rejected_without_following_location(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)

    def redirect(_path):
        return 302, {"Location": "/elsewhere"}, b"redirect body must stay private"

    with local_server(redirect) as (base, requests):
        monkeypatch.setenv("TEST_PROVIDER_KEY", "local-secret")
        with pytest.raises(ProviderError, match="request failed") as error:
            analyze(
                segment,
                tmp_path,
                provider="openai",
                model="gpt-test-vision",
                base_url=base,
                key_env="TEST_PROVIDER_KEY",
                allow_upload=True,
                question="Analyze this.",
            )
    assert len(requests) == 1
    assert requests[0]["path"] == "/chat/completions"
    assert "redirect body" not in str(error.value)


@pytest.mark.parametrize(
    "bad_result",
    [
        provider_result(evidence="not-sent"),
        provider_result(start=9.99, end=10.5),
    ],
    ids=["unknown-evidence", "time-outside-segment"],
)
def test_invalid_model_citations_and_times_are_rejected(tmp_path, monkeypatch, bad_result):
    segment = make_segment(tmp_path)
    response_body = openai_response(bad_result)
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, response_body)) as (base, _requests):
        monkeypatch.setenv("TEST_PROVIDER_KEY", "local-secret")
        with pytest.raises(ProviderError):
            analyze(
                segment,
                tmp_path,
                provider="openai",
                model="gpt-test-vision",
                base_url=base,
                key_env="TEST_PROVIDER_KEY",
                allow_upload=True,
                question="Analyze this.",
            )


def test_gemini_rejects_frame_citations_because_frames_are_not_uploaded(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    response_body = gemini_response(provider_result(evidence="frame-1", start=10.2, end=10.9))
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, response_body)) as (base, _requests):
        monkeypatch.setenv("TEST_GEMINI_KEY", "gemini-local-secret")
        with pytest.raises(ProviderError, match="unknown evidence"):
            analyze(
                segment,
                tmp_path,
                provider="gemini",
                model="gemini-test",
                base_url=base,
                key_env="TEST_GEMINI_KEY",
                allow_upload=True,
                question="Analyze this.",
            )


def test_gemini_rejects_video_over_20_mib_before_network(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    with (tmp_path / "segment.mp4").open("wb") as video:
        video.truncate(20 * 1024 * 1024 + 1)
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, b"{}")) as (base, requests):
        monkeypatch.setenv("TEST_GEMINI_KEY", "gemini-local-secret")
        with pytest.raises(ProviderError, match="20 MiB"):
            analyze(
                segment,
                tmp_path,
                provider="gemini",
                model="gemini-test",
                base_url=base,
                key_env="TEST_GEMINI_KEY",
                allow_upload=True,
                question="Analyze this.",
            )
    assert requests == []


@pytest.mark.parametrize(
    ("provider", "path", "key_name"),
    [
        ("openai", "frame.png", "TEST_PROVIDER_KEY"),
        ("gemini", "segment.mp4", "TEST_GEMINI_KEY"),
    ],
)
def test_provider_rejects_media_hash_mismatch_before_network(tmp_path, monkeypatch, provider, path, key_name):
    segment = make_segment(tmp_path)
    (tmp_path / path).write_bytes((tmp_path / path).read_bytes() + b"changed")
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, b"{}")) as (base, requests):
        monkeypatch.setenv(key_name, "local-secret")
        with pytest.raises(ProviderError, match="prepared SHA-256"):
            analyze(
                segment,
                tmp_path,
                provider=provider,
                model="model-for-test",
                base_url=base,
                key_env=key_name,
                allow_upload=True,
                question="Analyze this.",
            )
    assert requests == []


def test_http_base_url_with_query_is_rejected_before_request(tmp_path, monkeypatch):
    segment = make_segment(tmp_path)
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, b"{}")) as (base, requests):
        monkeypatch.setenv("TEST_PROVIDER_KEY", "local-secret")
        with pytest.raises(ProviderError, match="base URL"):
            analyze(
                segment,
                tmp_path,
                provider="openai",
                model="gpt-test-vision",
                base_url=base + "?leak=no",
                key_env="TEST_PROVIDER_KEY",
                allow_upload=True,
                question="Analyze this.",
            )
    assert requests == []


def test_manual_validation_allows_only_reviewed_frames_and_transcript():
    segment = {
        "start": 10.0,
        "end": 15.0,
        "frames": [{"id": "frame-1", "actual_time": 10.5}],
        "transcript": [
            {"id": "transcript-1", "start": 9.5, "end": 10.5, "text": "overlaps the start"}
        ],
        "clip": {"id": "native-clip-1"},
    }
    result = provider_result(evidence="transcript-1", start=10.0, end=10.5)

    assert validate_result(result, segment) is result

    clip_citation = copy.deepcopy(result)
    clip_citation["observations"][0]["evidence_ids"] = ["native-clip-1"]
    with pytest.raises(ProviderError, match="unknown evidence"):
        validate_result(clip_citation, segment)

    out_of_time_frame = copy.deepcopy(result)
    out_of_time_frame["observations"][0].update(
        {"start": 10.0, "end": 10.4, "evidence_ids": ["frame-1"]}
    )
    with pytest.raises(ProviderError, match="does not overlap"):
        validate_result(out_of_time_frame, segment)


@pytest.mark.parametrize("bad_time", [True, math.nan, math.inf])
def test_manual_validation_rejects_boolean_and_non_finite_times(bad_time):
    segment = {
        "start": 0.0,
        "end": 2.0,
        "frames": [{"id": "frame-1", "actual_time": 0.5}],
        "transcript": [],
    }
    result = provider_result(evidence="frame-1", start=0.0, end=1.0)
    result["observations"][0]["start"] = bad_time

    with pytest.raises(ProviderError):
        validate_result(result, segment)


@pytest.mark.parametrize(
    ("provider", "evidence_id", "claim_start", "claim_end", "clip_window"),
    [
        ("openai", "frame-1", 10.0, 10.4, None),
        ("openai", "transcript-1", 12.0, 13.0, None),
        ("gemini", "native-clip-1", 10.0, 10.5, (11.0, 12.0)),
    ],
    ids=["frame-after-claim", "transcript-before-claim", "clip-after-claim"],
)
def test_analysis_rejects_evidence_outside_observation_time(
    tmp_path, monkeypatch, provider, evidence_id, claim_start, claim_end, clip_window
):
    segment = make_segment(tmp_path)
    if clip_window:
        segment["clip"]["start"], segment["clip"]["end"] = clip_window
    result_body = (
        openai_response(provider_result(evidence=evidence_id, start=claim_start, end=claim_end))
        if provider == "openai"
        else gemini_response(provider_result(evidence=evidence_id, start=claim_start, end=claim_end))
    )
    key_name = "TEST_PROVIDER_KEY" if provider == "openai" else "TEST_GEMINI_KEY"
    monkeypatch.setenv(key_name, "local-secret")
    with local_server(lambda _path: (200, {"Content-Type": "application/json"}, result_body)) as (base, _requests):
        with pytest.raises(ProviderError, match="does not overlap"):
            analyze(
                segment,
                tmp_path,
                provider=provider,
                model="model-for-test",
                base_url=base,
                key_env=key_name,
                allow_upload=True,
                question="Analyze this.",
            )
