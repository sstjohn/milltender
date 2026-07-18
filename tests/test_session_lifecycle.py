"""The session state machine, including every quirk the TX6 taught us the hard way."""

import time

import milltender
from conftest import FakeRequest, drain, feed, status_frame, walk_samples


async def test_running_frame_starts_session(daemon):
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=1, steps=2))
    assert daemon.in_session
    assert daemon.latest["speed_mph"] == 1.0


async def test_backfill_reconstructs_missed_walking(daemon):
    # joined 10 min into a real walk: 600 s, 620 steps, 0.186 mi
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=600, dist_raw=186, steps=620))
    assert daemon.in_session
    assert len(daemon.samples) > 100
    assert daemon.samples[0].steps == 0
    assert daemon.samples[-1].steps == 620
    assert daemon.dist_m > 250


async def test_phantom_counters_recorded_fresh(daemon):
    # base carried 20 min / 0.46 mi from a stale session but almost no steps
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=1200, dist_raw=460, steps=4))
    assert daemon.in_session
    assert len(daemon.samples) == 1  # no backfill fabricated
    assert not daemon.anchor_ok


async def test_startup_stale_frame_rebases(daemon):
    # first frame carries the previous session's step count in stale RAM
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=1, steps=186))
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=2, steps=0))
    assert daemon.steps_offset == 0
    assert daemon.samples[-1].steps == 0


def spread(daemon):
    """Tests feed frames instantly; stretch sample timestamps to 1 Hz so the
    startup-glitch rebase (first 10 s) doesn't swallow mid-session events."""
    n = len(daemon.samples)
    for k, s in enumerate(daemon.samples):
        s.t -= (n - k)


async def test_mid_session_reset_carries_offsets(daemon):
    for i in range(30):
        feed(daemon, status_frame(state=3, speed_raw=10, elapsed=i, steps=i * 2))
    spread(daemon)  # session established, 58 steps
    # belt stop+restart: counters reset
    daemon._reset_guard_until = 0
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=1, steps=0))
    assert daemon.steps_offset == 58
    assert daemon.samples[-1].steps == 58


async def test_reset_guard_drops_stale_followup_frame(daemon):
    for i in range(30):
        feed(daemon, status_frame(state=3, speed_raw=10, elapsed=i, steps=i * 2))
    spread(daemon)
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=1, steps=0))     # reset
    n = len(daemon.samples)
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=2, steps=58))    # stale RAM echo
    assert len(daemon.samples) == n  # dropped, not double-counted
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=3, steps=2))     # sane frame
    assert daemon.samples[-1].steps == 60


async def test_end_state_arms_grace_not_finalize(daemon):
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=5, steps=10))
    feed(daemon, status_frame(state=0))
    await drain()
    assert daemon.in_session
    assert daemon.pending_end_t is not None


async def test_resume_within_grace_clears_timer(daemon):
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=5, steps=10))
    feed(daemon, status_frame(state=0))
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=6, steps=12))
    assert daemon.pending_end_t is None


async def test_start_clears_expired_grace_before_unpausing(daemon):
    """A resume after a long pause must not let the stale timer finalize the session."""
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=5, steps=10))
    feed(daemon, status_frame(state=0))
    daemon.pending_end_t = time.time() - 999  # long expired
    daemon.user_paused = True
    await daemon.h_start(FakeRequest())
    assert daemon.pending_end_t is None
    assert not daemon.user_paused


async def test_pause_frames_record_hr_only_samples(daemon):
    feed(daemon, status_frame(state=3, speed_raw=10, elapsed=5, steps=10))
    daemon.latest_hr, daemon.hr_last_seen = 95, time.time()
    daemon.last_sample_t = time.time() - 2
    feed(daemon, status_frame(state=10))
    s = daemon.samples[-1]
    assert not s.active
    assert s.speed_mps == 0.0
    assert s.hr == 95


async def test_finalize_discards_tiny_sessions(daemon, uploaded):
    daemon.in_session = True
    daemon.samples = walk_samples(10)
    await daemon.finalize()
    assert uploaded == []
    assert not daemon.in_session


async def test_finalize_builds_sidecar_and_uploads_once(daemon, uploaded):
    daemon.in_session = True
    daemon.samples = walk_samples(120)
    daemon.dist_m = daemon.samples[-1].dist_m
    await daemon.finalize()
    await daemon.finalize()  # double call must not double-upload
    assert len(uploaded) == 1
    sidecars = list(milltender.SESSIONS_DIR.glob("walk-*.json"))
    assert len(sidecars) == 1
    import json
    meta = json.loads(sidecars[0].read_text())
    assert meta["steps"] == 238
    assert len(meta["samples"][0]) == 6
    assert meta["kcal"] == round(11.9)


async def test_recovery_finish_noops_while_running(daemon):
    daemon.recovery_until = time.time() + 60
    daemon.latest["state"] = milltender.ST_RUNNING
    await daemon.h_recovery_finish(FakeRequest())
    assert daemon.recovery_until > time.time() + 30


async def test_recovery_finish_collapses_when_stopped(daemon):
    daemon.recovery_until = time.time() + 60
    daemon.latest["state"] = 0
    await daemon.h_recovery_finish(FakeRequest())
    assert daemon.recovery_until <= time.time()


async def test_recovery_extend_adds_a_minute(daemon):
    until = time.time() + 10
    daemon.recovery_until = until
    await daemon.h_recovery_extend(FakeRequest())
    assert daemon.recovery_until == until + 60


async def test_stop_is_single_flight(daemon, uploaded, monkeypatch):
    monkeypatch.setattr(milltender, "RECOVERY_S", 0)
    daemon.in_session = True
    daemon.samples = walk_samples(120)
    await daemon.h_stop(FakeRequest())
    task1 = daemon.stop_task
    await daemon.h_stop(FakeRequest())  # repeat press while in flight
    assert daemon.stop_task is task1
    await task1
    assert len(uploaded) == 1


async def test_speed_endpoint_clamps_to_global_limit(daemon, monkeypatch):
    monkeypatch.setattr(milltender, "MAX_MPH", 2.5)
    resp = await daemon.h_speed(FakeRequest({"mph": 5.0}))
    assert resp.status == 200
    assert daemon.sent[-1][2] == 25  # 2.5 mph in tenths
