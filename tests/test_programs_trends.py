"""Program validation, replay compression, and the trends snapshot."""

import json
import time

import pytest
from aiohttp import web

import milltender
from conftest import FakeRequest


async def test_program_save_normalizes_all_segment_kinds(daemon):
    await daemon.h_program_save(FakeRequest({"name": "mix", "segments": [
        {"type": "hold", "minutes": 5, "mph": 9.9},
        {"type": "ramp", "rate_mph": 5, "per_s": 1, "until_mph": 2.5},
        {"type": "ramp", "rate_mph": -0.5, "per_s": 30, "minutes": 2},
        {"type": "hr", "bpm": 300, "minutes": 20},
        {"type": "goal", "mph": 2.0, "miles": 1.5},
    ]}))
    saved = json.loads(milltender.PROGRAMS_FILE.read_text())["mix"]
    assert saved[0]["mph"] == 6.0            # clamped to protocol range
    assert saved[1]["rate_mph"] == 2.0 and saved[1]["per_s"] == 2
    assert saved[2]["minutes"] == 2
    assert saved[3]["bpm"] == 190 and saved[3]["max_mph"] == 3.0
    assert saved[4]["miles"] == 1.5


async def test_program_runs_with_durationless_segments(daemon, monkeypatch):
    """Ramp-to-a-speed and goal segments carry no 'minutes'; the loop must still run."""
    async def tick():
        return "tick"

    async def no_stop_flow():
        pass

    monkeypatch.setattr(daemon, "_prog_tick", tick)
    monkeypatch.setattr(daemon, "_stop_flow", no_stop_flow)
    daemon.in_session = True
    daemon.latest = {"state": milltender.ST_RUNNING, "speed_mph": 1.5}
    daemon.dist_m = 99999
    await daemon._program_loop("mix", [
        {"type": "ramp", "rate_mph": 0.2, "per_s": 1, "until_mph": 2.0},
        {"type": "goal", "mph": 2.0, "miles": 1.0},
        {"type": "hold", "minutes": 0.05, "mph": 1.5},
    ])
    speeds = [p[2] for p in daemon.sent if p[:2] == bytes([0x53, 0x02])]
    assert speeds[-1] == 15                             # reached the final hold
    assert bytes([0x53, 0x03]) in daemon.sent           # program completed, belt stopped


async def test_program_save_rejects_empty(daemon):
    with pytest.raises(web.HTTPBadRequest):
        await daemon.h_program_save(FakeRequest({"name": "x", "segments": []}))


async def test_replay_compresses_speed_profile(daemon, monkeypatch):
    rows = []
    t = 0.0
    for mph, seconds in ((1.5, 300), (2.5, 180), (1.5, 4), (2.0, 120)):
        for _ in range(int(seconds)):
            rows.append([t, mph, None, 1, None, 100])
            t += 1.0
    (milltender.SESSIONS_DIR / "walk-x.json").write_text(json.dumps(
        {"start": 0, "samples": rows}))
    started = {}
    monkeypatch.setattr(daemon, "_start_program",
                        lambda name, segs: started.update(name=name, segs=segs))
    await daemon.h_replay(FakeRequest({"name": "walk-x"}))
    mphs = [s["mph"] for s in started["segs"]]
    assert mphs == [1.5, 2.5, 2.0]  # 4 s blip dropped, runs merged


async def test_replay_refuses_foreign_names(daemon):
    (milltender.SESSIONS_DIR / "failed-1.json").write_text("{}")
    with pytest.raises(web.HTTPNotFound):
        await daemon.h_replay(FakeRequest({"name": "failed-1"}))


def _sidecar(name, start, steps, dist_m, samples):
    (milltender.SESSIONS_DIR / f"{name}.json").write_text(json.dumps({
        "start": start, "duration_s": len(samples), "dist_m": dist_m,
        "steps": steps, "kcal": 10, "hr_avg": 95, "hrr60": 20,
        "hrv_baseline": 15.0, "samples": samples}))


def test_trends_snapshot_windows_and_thresholds(daemon):
    now = time.time()
    steady = [[200 + i, 2.0, 100, 1, None, 110] for i in range(150)]
    _sidecar("walk-old", now - 30 * 86400, 300, 480, steady)
    _sidecar("walk-new", now - 3600, 300, 480, steady[:130])
    snap = daemon._trends_snapshot(now, 7)
    assert snap["all"]["sessions"] == 2
    assert snap["week"]["sessions"] == 1
    # 280 samples all-time at 2.0 mph clears the 120 floor; 130 recent clears 60
    assert snap["curve_all"] == [{"mph": 2.0, "bpm": 100}]
    assert snap["curve_recent"] == [{"mph": 2.0, "bpm": 100}]
    assert snap["recent"][0]["hrr60"] == 20
    # a thin recent window: bucket present recent-only
    snap30 = daemon._trends_snapshot(now, 30)
    assert snap30["curve_recent"] == [{"mph": 2.0, "bpm": 100}]


def test_trends_snapshot_ignores_early_and_slow_samples(daemon):
    now = time.time()
    rows = ([[i, 2.0, 100, 1, None, 110] for i in range(150)]          # first 3 min: excluded
            + [[400 + i, 0.2, 100, 1, None, 20] for i in range(200)])  # sub-walking speed
    _sidecar("walk-y", now - 3600, 100, 100, rows)
    snap = daemon._trends_snapshot(now, 7)
    assert snap["curve_all"] == []
