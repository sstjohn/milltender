"""FIT encoding: events, totals, cadence, and HRV round-trip through a real decode."""

import time

from fit_tool.fit_file import FitFile
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.hrv_message import HrvMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import EventType

from conftest import walk_samples
from fit_build import Sample, build_fit, cadence_series


def decode(path):
    events, records, hrv, session = [], [], [], None
    for rec in FitFile.from_file(str(path)).records:
        m = rec.message
        if isinstance(m, EventMessage):
            events.append(getattr(m.event_type, "value", m.event_type))
        elif isinstance(m, RecordMessage):
            records.append(m)
        elif isinstance(m, HrvMessage):
            hrv.extend(m.time or [])
        elif isinstance(m, SessionMessage):
            session = m
    return events, records, hrv, session


def test_steady_walk_totals(tmp_path):
    samples = walk_samples(300)
    path = build_fit(samples, tmp_path)
    events, records, _, session = decode(path)
    assert len(records) == 300
    assert session.total_strides == (samples[-1].steps - samples[0].steps) // 2
    assert round(session.total_timer_time) == 299
    assert round(session.total_calories) == round(samples[-1].kcal - samples[0].kcal)
    assert events.count(EventType.STOP_ALL.value) == 1  # only the closing event


def test_pause_marked_and_excluded_from_timer(tmp_path):
    t0 = time.time() - 400
    samples = walk_samples(120, start=t0)
    frozen = samples[-1]
    for i in range(60):  # a minute standing: HR-only samples
        samples.append(Sample(t=frozen.t + 1 + i, speed_mps=0.0, dist_m=frozen.dist_m,
                              steps=frozen.steps, kcal=frozen.kcal, hr=88, active=False))
    for i in range(120):  # resumed
        prev = samples[-1]
        samples.append(Sample(t=prev.t + 1, speed_mps=0.67, dist_m=prev.dist_m + 0.67,
                              steps=prev.steps + 2, kcal=prev.kcal + 0.1, hr=92))
    path = build_fit(samples, tmp_path)
    events, records, _, session = decode(path)
    assert events.count(EventType.STOP_ALL.value) >= 2  # pause + close
    assert events.count(EventType.START.value) >= 2     # open + resume
    assert session.total_timer_time < 245         # the standing minute is not moving time
    assert session.total_elapsed_time > 295
    paused_hr = [r.heart_rate for r in records if r.speed == 0.0]
    assert paused_hr and all(h == 88 for h in paused_hr)


def test_rr_events_become_hrv_messages(tmp_path):
    samples = walk_samples(120)
    rr = [(samples[10].t + i * 0.8, 0.8) for i in range(50)]
    path = build_fit(samples, tmp_path, rr_events=rr)
    _, _, hrv, _ = decode(path)
    assert len(hrv) == 50
    assert abs(hrv[0] - 0.8) < 0.01


def test_cadence_clamped_to_u8(tmp_path):
    samples = walk_samples(120, cadence=40)  # absurd 2400 spm
    path = build_fit(samples, tmp_path)
    _, records, _, _ = decode(path)
    assert max(r.cadence for r in records) == 255


def test_cadence_series_window_and_gap():
    samples = walk_samples(60)
    cads = cadence_series(samples)
    assert cads[0] == 0.0
    assert 115 <= cads[-1] <= 125  # 2 steps/s
    # a data gap flushes the window: first sample after it has no basis
    later = walk_samples(30, start=samples[-1].t + 300)
    combined = samples + later
    cads2 = cadence_series(combined)
    assert cads2[len(samples)] == 0.0
