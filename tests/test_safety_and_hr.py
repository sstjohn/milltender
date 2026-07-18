"""The speed ceiling under adversarial conditions, stop-flow exits, HR parsing."""

import asyncio
import time

import milltender
from conftest import FakeRequest, walk_samples


async def fast_tick(daemon, monkeypatch, results=("tick",)):
    """Replace the 1 s program tick with an instant one."""
    seq = iter(results)
    last = results[-1]

    async def tick():
        return next(seq, last)

    monkeypatch.setattr(daemon, "_prog_tick", tick)


async def test_downward_ramp_never_commands_above_ceiling(daemon, monkeypatch):
    """A remote-set overspeed plus an until-target in the (MAX_MPH, speed) gap
    must decelerate to the ceiling, not to the raw target."""
    monkeypatch.setattr(milltender, "MAX_MPH", 2.5)
    daemon._prog_speed = 5.0  # the remote put it there; we merely observed
    await fast_tick(daemon, monkeypatch)
    ok = await daemon._run_ramp("t", 0, 1, {"rate_mph": -0.5, "per_s": 1, "until_mph": 3.0})
    assert ok
    speeds = [p[2] for p in daemon.sent if p[:2] == bytes([0x53, 0x02])]
    assert speeds, "ramp never commanded"
    assert max(speeds) <= 25  # every command at or below 2.5 mph
    assert speeds[-1] == 25   # settled on the ceiling, segment complete


async def test_upward_ramp_terminates_at_ceiling(daemon, monkeypatch):
    monkeypatch.setattr(milltender, "MAX_MPH", 2.5)
    daemon._prog_speed = 1.5
    await fast_tick(daemon, monkeypatch)
    ok = await daemon._run_ramp("t", 0, 1, {"rate_mph": 0.5, "per_s": 1, "until_mph": 6.0})
    assert ok
    speeds = [p[2] for p in daemon.sent if p[:2] == bytes([0x53, 0x02])]
    assert max(speeds) <= 25


async def test_stop_flow_aborts_when_belt_resumes(daemon, uploaded, monkeypatch):
    monkeypatch.setattr(milltender, "RECOVERY_S", 5)
    daemon.in_session = True
    daemon.samples = walk_samples(120)
    daemon.latest["state"] = 0
    task = asyncio.get_running_loop().create_task(daemon._stop_flow())
    await asyncio.sleep(2.5)  # inside the recovery window
    daemon.latest["state"] = milltender.ST_RUNNING
    await task
    assert daemon.in_session          # session survived
    assert uploaded == []
    assert daemon.recovery_until is None


async def test_stop_flow_finish_collapse_uploads(daemon, uploaded, monkeypatch):
    monkeypatch.setattr(milltender, "RECOVERY_S", 30)
    daemon.in_session = True
    daemon.samples = walk_samples(120)
    daemon.latest["state"] = 0
    task = asyncio.get_running_loop().create_task(daemon._stop_flow())
    await asyncio.sleep(2.5)  # recovery armed by now
    await daemon.h_recovery_finish(FakeRequest())
    await asyncio.wait_for(task, timeout=5)
    assert not daemon.in_session
    assert len(uploaded) == 1
    assert daemon.recovery_until is None


def hr_packet(hr: int, rr: list[float] = (), wide: bool = False, energy: bool = False) -> bytes:
    flags = (0x01 if wide else 0) | (0x08 if energy else 0) | (0x10 if rr else 0)
    out = [flags]
    out += [hr & 0xFF, hr >> 8] if wide else [hr]
    if energy:
        out += [0x00, 0x04]  # decodes to a plausible 1.0 s RR if the skip regresses
    for v in rr:
        raw = round(v * 1024)
        out += [raw & 0xFF, raw >> 8]
    return bytes(out)


def test_on_hr_narrow_and_wide(daemon):
    daemon.on_hr(None, bytearray(hr_packet(88)))
    assert daemon.latest_hr == 88
    daemon.on_hr(None, bytearray(hr_packet(300, wide=True)))
    assert daemon.latest_hr == 300


def test_on_hr_rr_extraction_skips_energy_field(daemon):
    daemon.on_hr(None, bytearray(hr_packet(90, rr=[0.8, 0.75], energy=True)))
    assert [round(v, 2) for _, v in daemon.rr_events] == [0.8, 0.75]


def test_on_hr_drops_artifact_rr(daemon):
    daemon.on_hr(None, bytearray(hr_packet(90, rr=[0.1, 0.8, 3.0])))
    assert [round(v, 2) for _, v in daemon.rr_events] == [0.8]


def test_on_hr_stale_marker(daemon):
    daemon.on_hr(None, bytearray(hr_packet(90)))
    assert time.time() - daemon.hr_last_seen < 1
