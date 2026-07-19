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


def _recovery_session(peak, floor, tau):
    """A minute of walking at `peak` bpm, then 90 s of exponential HR decay."""
    from math import exp
    from fit_build import Sample
    out, t = [], 0.0
    for _ in range(60):
        out.append(Sample(t=t, speed_mps=0.9, dist_m=0, steps=int(t) * 2, kcal=0,
                          hr=peak, active=True)); t += 1
    for k in range(90):
        hr = round(floor + (peak - floor) * exp(-k / tau))
        out.append(Sample(t=t, speed_mps=0.0, dist_m=0, steps=120, kcal=0,
                          hr=hr, active=False)); t += 1
    return out


def test_recovery_recovers_hrr_and_tau():
    rec = milltender.recovery_analysis(_recovery_session(150, 80, 40))
    assert rec["peak"] == 150
    assert 20 <= rec["hrr30"] <= 40      # a real drop
    assert rec["hrr60"] > rec["hrr30"]
    assert 25 <= rec["tau"] <= 60        # recovers the ~40 s constant it was built with


def test_recovery_refuses_flat_signal():
    # HR barely moves (walking intensity): tau must be None, no fabricated metric
    rec = milltender.recovery_analysis(_recovery_session(92, 90, 40))
    assert rec is None or rec["tau"] is None


def test_recovery_dropout_does_not_fabricate_tau():
    # a flat 90-bpm tail with one strap glitch reading 40 must not manufacture a
    # time constant off that lone sample
    from fit_build import Sample
    s = _recovery_session(92, 90, 40)
    for x in s:
        if not x.active:
            x.hr = 90
    s[75].hr = 40  # single dropout mid-recovery
    rec = milltender.recovery_analysis(s)
    assert rec is None or rec["tau"] is None


def _steady_walk(minutes, hr_first, hr_second, speed=0.9):
    from fit_build import Sample
    n = minutes * 60
    return [Sample(t=i, speed_mps=speed, dist_m=0, steps=i * 2, kcal=0,
                   hr=hr_first if i < n / 2 else hr_second, active=True)
            for i in range(n)]


def test_drift_positive_when_hr_climbs():
    # steady pace, HR drifts 100 -> 112: efficiency drops, decoupling positive
    d = milltender.cardiac_drift(_steady_walk(30, 100, 112))
    assert d is not None and d > 5


def test_drift_near_zero_when_steady():
    d = milltender.cardiac_drift(_steady_walk(30, 105, 105))
    assert d is not None and abs(d) < 2


def test_drift_none_on_short_walk():
    assert milltender.cardiac_drift(_steady_walk(10, 100, 115)) is None


def test_drift_none_when_halves_share_no_pace():
    from fit_build import Sample
    # first half slow, second half fast, split cleanly at the post-warmup midpoint:
    # no common pace to compare, so decoupling is undefined
    s = [Sample(t=i, speed_mps=0.8 if i < 1080 else 1.4, dist_m=0, steps=i * 2, kcal=0,
                hr=105, active=True) for i in range(1980)]
    assert milltender.cardiac_drift(s) is None


def test_drift_detected_despite_intervals():
    from fit_build import Sample
    # pace alternates every minute; HR is +10 in the second half at BOTH paces —
    # matching on pace catches the drift the raw efficiency ratio would blur
    s = []
    for i in range(1800):
        mph = 0.8 if (i // 60) % 2 else 1.4
        hr = (105 if mph == 0.8 else 120) + (10 if i >= 900 else 0)
        s.append(Sample(t=i, speed_mps=mph * 0.44704, dist_m=0, steps=i * 2, kcal=0,
                        hr=hr, active=True))
    d = milltender.cardiac_drift(s)
    assert d is not None and d > 5


def test_recovery_none_without_belt_stop():
    from fit_build import Sample
    active_only = [Sample(t=i, speed_mps=0.9, dist_m=0, steps=i * 2, kcal=0, hr=100)
                   for i in range(120)]
    assert milltender.recovery_analysis(active_only) is None
