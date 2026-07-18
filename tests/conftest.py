import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import milltender  # noqa: E402
from fit_build import Sample  # noqa: E402


def status_frame(state: int, speed_raw: int = 0, elapsed: int = 0,
                 dist_raw: int = 0, kcal_raw: int = 0, steps: int = 0) -> bytes:
    """A FitShow SYS_STATUS notification as the base emits it."""
    payload = bytes([0x51, state, speed_raw, 0,
                     elapsed & 0xFF, elapsed >> 8,
                     dist_raw & 0xFF, dist_raw >> 8,
                     kcal_raw & 0xFF, kcal_raw >> 8,
                     steps & 0xFF, steps >> 8, 0, 0])
    return milltender.frame(payload)


def walk_samples(n: int = 120, start: float | None = None, cadence: int = 2,
                 hr: int | None = 90) -> list[Sample]:
    """n seconds of steady walking at 1 Hz, ~1.5 mph."""
    t0 = start or time.time() - n
    return [Sample(t=t0 + i, speed_mps=0.67, dist_m=0.67 * i, steps=cadence * i,
                   kcal=0.1 * i, hr=hr, hrv=None) for i in range(n)]


class FakeRequest:
    def __init__(self, body: dict | None = None, query: dict | None = None):
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


@pytest.fixture
def daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(milltender, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(milltender, "PROGRAMS_FILE", tmp_path / "programs.json")
    milltender.SESSIONS_DIR.mkdir()
    d = milltender.Daemon()

    async def quiet_hrm():
        pass

    monkeypatch.setattr(d, "hrm_loop", quiet_hrm)
    d.sent = []  # every BLE command the daemon tried to write

    async def record_cmd(payload: bytes):
        d.sent.append(bytes(payload))

    monkeypatch.setattr(d, "send_cmd", record_cmd)
    return d


@pytest.fixture
def uploaded(monkeypatch):
    """Capture upload_all calls instead of talking to Strava/Garmin."""
    calls = []

    def fake_upload_all(fit_path, name):
        calls.append((Path(fit_path), name))
        return {"strava": {"ok": True, "result": 1}, "garmin": {"ok": True, "result": {}}}

    monkeypatch.setattr(milltender.uploads, "upload_all", fake_upload_all)
    return calls


def feed(d, frame_bytes: bytes) -> None:
    d.on_treadmill_frame(None, bytearray(frame_bytes))


async def drain():
    """Let fire-and-forget tasks created by callbacks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)
