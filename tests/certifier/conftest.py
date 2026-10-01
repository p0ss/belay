from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from belay.contract.service import create_app
from belay.contract.stub import StubBackend
from belay.certifier.suite import load_suite

SUITE = Path(__file__).parent / "fixtures" / "suite"


@pytest.fixture
def suite():
    return load_suite(SUITE, need_cases=True, need_canaries=True)


@pytest.fixture
def stub(tmp_path):
    """Factory: an in-process stub detector in a tamper mode, and its alert log path."""
    n = [0]

    def make(tamper="none", seed=0, backend=None):
        n[0] += 1
        log = tmp_path / f"alerts-{n[0]}-{tamper}-{seed}.jsonl"
        app = create_app(backend or StubBackend(tamper, seed=seed, latency_ms=0), log)
        return TestClient(app), log
    return make
