import json
import sys

from belay.bench.cli import main
from belay.bench.launch import serve_in_thread
from belay.bench.monitors import GpuPoller
from belay.bench.report import percentile, stats
from belay.bench.runner import load_prompts
from belay.contract.service import create_app
from belay.contract.stub import StubBackend

FAST = ["--stub-latency-ms", "0", "--warmup", "1", "--quiet", "--no-gpu", "--stream-grace", "2"]


def test_percentile_and_stats():
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert abs(percentile(list(range(1, 101)), 0.95) - 95.05) < 1e-9
    s = stats([3, None, 1, 2])
    assert s["n"] == 3 and s["median"] == 2 and s["max"] == 3


def test_prompt_set_spans_fields_and_designated_ones():
    prompts = load_prompts()
    fields = {p["field"] for p in prompts}
    assert len(prompts) >= 40 and len(fields) >= 30
    assert {"ConstitutionalLaw", "PoliticalTheory", "LegalStudies"} <= fields
    assert len({p["id"] for p in prompts}) == len(prompts)


def test_dry_run_full_matrix(tmp_path):
    main(["run", "--dry-run", "--sessions", "1,4,8,16", "--requests", "16", "--out", str(tmp_path),
          "--run-id", "t", *FAST])
    results = json.loads((tmp_path / "bench-t.json").read_text())
    cells = results["cells"]
    assert [(c["summary"]["setting"], c["summary"]["sessions"]) for c in cells] == [
        (s, n) for s in ("off", "proxy", "full") for n in (1, 4, 8, 16)]
    for c in cells:
        m = c["summary"]
        assert m["requests"] == 16 and m["errors"] == 0
        assert m["warmup_requests"] == m["sessions"]
        assert m["signature_failures"] == {"responses": 0, "stream_records": 0}
        assert m["ms_per_token"]["median"] > 0 and m["throughput_tok_s"] > 0
        assert m["resident_peak"]["max"] == 12
        # The committed prompts include designated fields, so the stub alerts in every cell,
        # and every alert shows up on GET /alerts.
        assert m["alerts"]["count"] > 0 and m["alerts"]["stream_missing"] == 0
        assert m["alerts"]["stream_latency_ms"]["median"] is not None
        assert m["gpu_peak_mb"] is None
    assert len(results["diff_vs_off"]) == 8
    assert (tmp_path / "bench-t.csv").read_text().count("\n") == 13
    assert (tmp_path / "bench-t-diff.csv").exists()


def test_url_runs_then_summarize(tmp_path):
    for setting in ("off", "proxy"):
        app = create_app(StubBackend(latency_ms=0), tmp_path / "alerts.jsonl")
        with serve_in_thread(app) as url:
            main(["run", "--url", url, "--setting", setting, "--sessions", "1,4", "--requests", "4",
                  "--out", str(tmp_path), "--run-id", setting, *FAST])
    assert not (tmp_path / "bench-off-off-diff.csv").exists()
    main(["summarize", str(tmp_path / "bench-off-off.json"), str(tmp_path / "bench-proxy-proxy.json"),
          "--out", str(tmp_path), "--run-id", "m"])
    merged = json.loads((tmp_path / "bench-m.json").read_text())
    assert len(merged["cells"]) == 4
    assert [(d["setting"], d["sessions"]) for d in merged["diff_vs_off"]] == [("proxy", 1), ("proxy", 4)]
    assert "ms_per_token_median_diff_pct" in merged["diff_vs_off"][0]


def test_bad_signatures_are_counted(tmp_path):
    app = create_app(StubBackend(latency_ms=0), tmp_path / "alerts.jsonl", key=b"not-the-lab-key")
    with serve_in_thread(app) as url:
        main(["run", "--url", url, "--setting", "proxy", "--sessions", "2", "--requests", "8",
              "--out", str(tmp_path), "--run-id", "bad", *FAST])
    m = json.loads((tmp_path / "bench-bad-proxy.json").read_text())["cells"][0]["summary"]
    assert m["signature_failures"]["responses"] == 8
    assert m["signature_failures"]["stream_records"] == m["alerts"]["count"] > 0


def test_launch_mode_starts_and_stops_detector(tmp_path):
    command = (f"{sys.executable} -m belay.contract.stub --port {{port}} --latency-ms 0 "
               f"--log {tmp_path}/stub-{{setting}}.jsonl")
    main(["run", "--launch", "--settings", "off,proxy", "--command", command, "--port", "8799",
          "--sessions", "1", "--requests", "2", "--startup-timeout", "30", "--out", str(tmp_path),
          "--run-id", "l", "--no-stream", *FAST])
    # (--no-stream: the contract server notices a closed /alerts stream only at its next
    # 15 s keepalive, and uvicorn waits for it before exiting.)
    results = json.loads((tmp_path / "bench-l.json").read_text())
    assert [c["summary"]["setting"] for c in results["cells"]] == ["off", "proxy"]
    assert (tmp_path / "bench-l-off.log").exists()


def test_gpu_poller_present_and_absent():
    gpu = GpuPoller(command=["echo", "1234\n100"]).start()
    assert gpu.available and gpu.peak() == 1334
    gpu.stop()
    missing = GpuPoller(command=["definitely-not-nvidia-smi"])
    assert not missing.available and missing.start().peak() is None
