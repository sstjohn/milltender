"""Build a FIT activity file from recorded treadmill samples."""

import dataclasses
import time
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Event, EventType, FileType, Manufacturer, Sport, SubSport,
)

PAUSE_GAP_S = 3.0  # a gap between samples larger than this becomes a timer stop/start


def cadence_series(samples: list["Sample"]) -> list[float]:
    """Steps/min per sample over a trailing 20 s window."""
    out: list[float] = []
    j = 0
    for i, s in enumerate(samples):
        while samples[j].t < s.t - 20:
            j += 1
        if i > j and s.t > samples[j].t:
            out.append(max(0.0, round((s.steps - samples[j].steps) * 60.0
                                      / (s.t - samples[j].t))))
        else:
            out.append(0.0)
    return out


@dataclasses.dataclass
class Sample:
    t: float          # wall clock, epoch seconds
    speed_mps: float
    dist_m: float     # cumulative, smoothed (speed-integrated)
    steps: int        # cumulative, true count from the base
    kcal: float       # cumulative
    hr: int | None    # latest strap reading, None if no strap
    active: bool = True  # False while belt is stopped/paused (HR-only samples)
    hrv: float | None = None  # rolling rMSSD ms (60 s window); not encoded in FIT


def _hrv_message(rr_s: float):
    """FIT hrv message (one RR interval, seconds). Returns None if unsupported."""
    try:
        from fit_tool.profile.messages.hrv_message import HrvMessage
    except ImportError:
        return None
    msg = HrvMessage()
    msg.time = [rr_s]
    return msg


def build_fit(samples: list[Sample], out_dir: Path,
              rr_events: list[tuple[float, float]] | None = None) -> Path:
    """rr_events: [(wall_time, rr_interval_seconds)] from the HR strap, optional."""
    assert len(samples) >= 2, "need at least two samples"
    rr_events = sorted(rr_events or [])
    rr_i = 0
    start, end = samples[0], samples[-1]
    start_ms = int(start.t * 1000)

    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 0x7863
    file_id.time_created = start_ms
    builder.add(file_id)

    def event(ts_ms: int, etype: EventType) -> None:
        ev = EventMessage()
        ev.event = Event.TIMER
        ev.event_type = etype
        ev.timestamp = ts_ms
        builder.add(ev)

    event(start_ms, EventType.START)

    timer_s = 0.0
    prev = None
    cads = cadence_series(samples)
    for s, cadence_spm in zip(samples, cads):
        if prev is not None:
            gap = s.t - prev.t
            if prev.active and not s.active:      # pause begins (records continue: HR)
                event(int(s.t * 1000), EventType.STOP_ALL)
            elif not prev.active and s.active:    # resumed
                event(int(s.t * 1000), EventType.START)
            elif prev.active and s.active and gap > PAUSE_GAP_S:  # missing data
                event(int(prev.t * 1000), EventType.STOP_ALL)
                event(int(s.t * 1000), EventType.START)
            if prev.active and s.active and gap <= PAUSE_GAP_S:
                timer_s += gap
        rec = RecordMessage()
        rec.timestamp = int(s.t * 1000)
        rec.distance = s.dist_m
        rec.speed = s.speed_mps
        rec.cadence = min(255, round(cadence_spm / 2))  # strides/min, u8 field
        if s.hr:
            rec.heart_rate = s.hr
        # interleave hrv messages for beats since the previous record
        while rr_i < len(rr_events) and rr_events[rr_i][0] <= s.t:
            hrv = _hrv_message(rr_events[rr_i][1])
            if hrv is not None:
                builder.add(hrv)
            rr_i += 1
        builder.add(rec)
        prev = s

    end_ms = int(end.t * 1000)
    event(end_ms, EventType.STOP_ALL)

    total_steps = end.steps - start.steps
    elapsed = end.t - start.t

    lap = LapMessage()
    lap.timestamp = end_ms
    lap.start_time = start_ms
    lap.total_elapsed_time = elapsed
    lap.total_timer_time = timer_s
    lap.total_distance = end.dist_m
    builder.add(lap)

    session = SessionMessage()
    session.timestamp = end_ms
    session.start_time = start_ms
    session.total_elapsed_time = elapsed
    session.total_timer_time = timer_s
    session.sport = Sport.WALKING
    session.sub_sport = SubSport.TREADMILL
    session.total_distance = end.dist_m
    session.avg_speed = end.dist_m / timer_s if timer_s else 0.0
    session.total_strides = total_steps // 2
    session.total_calories = round(end.kcal - start.kcal)
    hrs = [s.hr for s in samples if s.hr]
    if hrs:
        session.avg_heart_rate = round(sum(hrs) / len(hrs))
        session.max_heart_rate = max(hrs)
    builder.add(session)

    activity = ActivityMessage()
    activity.timestamp = end_ms
    activity.total_timer_time = timer_s
    activity.num_sessions = 1
    builder.add(activity)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / time.strftime("walk-%Y%m%d-%H%M%S.fit", time.localtime(start.t))
    builder.build().to_file(str(out))
    return out
