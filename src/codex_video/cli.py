from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import pipeline, media


def positive(value):
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("Must be finite and positive")
    return result


def count(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Timecoded video evidence; visual coverage is sampled.")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Extract local evidence; rerun identical command to resume")
    p.add_argument("video", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--segment-seconds", type=positive, default=60.0)
    p.add_argument("--sample-seconds", type=positive, default=10.0)
    p.add_argument("--subtitles", type=Path, dest="subtitle_path")
    p.add_argument("--audio", action="store_true")
    p.add_argument("--transcribe", action="store_true")
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    p.add_argument("--native-clips", action="store_true")
    p.add_argument("--max-segments", type=count)
    p = commands.add_parser("analyze", help="Use explicit adapter; remote upload requires --allow-upload")
    p.add_argument("run", type=Path)
    p.add_argument("--provider", choices=("mock", "openai", "gemini"), default="mock")
    p.add_argument("--model")
    p.add_argument("--base-url")
    p.add_argument("--key-env", default="VIDEO_API_KEY")
    p.add_argument("--allow-upload", action="store_true")
    p.add_argument("--question", default="Review this video with source evidence.")
    p.add_argument("--max-segments", type=count)
    p = commands.add_parser("browser-import", help="Import authorized browser captures with observed media times")
    p.add_argument("capture", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--segment-seconds", type=positive, default=60.0)
    p = commands.add_parser("review", help="Attach a declared Codex/manual image review with validated citations")
    p.add_argument("run", type=Path)
    p.add_argument("--segment", required=True, dest="segment_id")
    p.add_argument("--result", required=True, type=Path, dest="result_path")
    p.add_argument("--viewed", required=True, help="Comma-separated frame IDs actually viewed")
    p.add_argument("--reviewer", default="codex")
    p = commands.add_parser("report", help="Verify evidence and regenerate coverage.json and report.md")
    p.add_argument("run", type=Path)
    p = commands.add_parser("frame", help="Inspect another source time without changing an existing run")
    p.add_argument("video", type=Path)
    p.add_argument("--at", required=True, type=float)
    p.add_argument("--out", required=True, type=Path)
    args = vars(parser.parse_args(argv))
    cmd = args.pop("command")
    try:
        if cmd == "prepare":
            manifest = pipeline.prepare(args.pop("video"), args.pop("out"), **args)
        elif cmd == "analyze":
            manifest = pipeline.analyze_run(args.pop("run"), **args)
        elif cmd == "browser-import":
            manifest = pipeline.browser_import(args.pop("capture"), args.pop("out"), **args)
        elif cmd == "review":
            args["viewed"] = [v.strip() for v in args["viewed"].split(",") if v.strip()]
            manifest = pipeline.review(args.pop("run"), **args)
        elif cmd == "report":
            root = args["run"]
            with pipeline.lock(root):
                manifest = pipeline.read(root)
                pipeline.write_report(root, manifest)
        else:
            print(json.dumps(media.frame(args["video"], args["at"], args["out"]), ensure_ascii=False))
            return 0
        failed = any(s["status"] == "failed" or s.get("analysis", {}).get("status") == "failed" for s in manifest["segments"])
        print(json.dumps({"segments": len(manifest["segments"]),
                          "prepared": sum(s["status"] == "prepared" for s in manifest["segments"]),
                          "unprepared": sum(s["status"] != "prepared" for s in manifest["segments"]),
                          "failed_visual_samples": sum(len(s.get("sample_failures", [])) for s in manifest["segments"]),
                          "review_results_done": sum(s.get("analysis", {}).get("status") == "done" and s["analysis"].get("semantic_analysis", False) for s in manifest["segments"]),
                          "mock_checks_done": sum(s.get("analysis", {}).get("status") == "done" and not s["analysis"].get("semantic_analysis", False) for s in manifest["segments"]),
                          "failed": failed}, ensure_ascii=False))
        return 2 if failed else 0
    except Exception as exc:
        # Do not echo URLs, paths, response bodies, credentials, or stack traces.
        print(json.dumps({"error": type(exc).__name__,
                          "message": "Operation failed; verify inputs, run integrity, dependencies and provider settings. See documentation."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
