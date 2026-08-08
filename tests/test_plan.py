import json
import time
from datetime import date, datetime, timedelta

import pytest
from aiohttp import web
from conftest import FakeRequest

import milltender

MONDAY = date(2026, 8, 10)

PROGRAMS = {
    "walk-jog 8x1": [{"type": "hold", "minutes": 1.0, "mph": 4.8},
                     {"type": "hold", "minutes": 2.0, "mph": 3.0}] * 8,
    "walk-jog 6x2": [{"type": "hold", "minutes": 2.0, "mph": 4.8},
                     {"type": "hold", "minutes": 2.0, "mph": 3.0}] * 6,
    "tempo walk 3x6": [{"type": "hold", "minutes": 6.0, "mph": 4.0}] * 3,
    "easy 30": [{"type": "hold", "minutes": 30.0, "mph": 3.0}],
}


def seed(daemon, weeks, mode="days", start=MONDAY, programs=PROGRAMS):
    milltender.PROGRAMS_FILE.write_text(json.dumps(programs))
    milltender.PLAN_FILE.write_text(json.dumps(
        {"name": "walk to jog", "start": start.isoformat(), "mode": mode, "weeks": weeks}))


def at(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour).timestamp()


def walked(name, when: float, program=None, mph=3.0, minutes=30):
    """A finished session at one speed, written the way finalize() writes it."""
    rows = [[i, mph, 100, 1, None, 130] for i in range(minutes * 60)]
    (milltender.SESSIONS_DIR / f"{name}.json").write_text(json.dumps({
        "start": when, "duration_s": len(rows), "moving_s": len(rows), "program": program,
        "dist_m": 100, "steps": 500, "samples": rows}))


def test_plan_week_maps_a_date_to_its_week(daemon):
    plan = {"start": "2026-08-10", "weeks": [{"mon": "a"}, {"mon": "b"}]}
    assert milltender.plan_week(plan, MONDAY)[0] == 1
    assert milltender.plan_week(plan, MONDAY + timedelta(days=6))[0] == 1
    assert milltender.plan_week(plan, MONDAY + timedelta(days=7))[0] == 2
    assert milltender.plan_week(plan, MONDAY - timedelta(days=1)) is None  # before it opens
    assert milltender.plan_week(plan, MONDAY + timedelta(days=14)) is None  # past the end


def test_plan_start_snaps_back_to_its_monday(daemon):
    # a mid-week start would otherwise roll the weeks over on a Wednesday
    assert milltender.plan_start({"start": "2026-08-12"}) == MONDAY
    assert milltender.plan_start({"start": "nonsense"}) is None
    assert milltender.plan_start({}) is None


def test_week_sessions_normalises_both_modes(daemon):
    assert milltender.week_sessions({"wed": "b", "mon": "a"}) == [("mon", "a"), ("wed", "b")]
    assert milltender.week_sessions(["a", "b"]) == [(None, "a"), (None, "b")]
    assert milltender.week_sessions({}) == []


def test_program_impact_min_counts_held_running_time_only(daemon):
    assert milltender.program_impact_min(PROGRAMS["walk-jog 6x2"]) == 12
    assert milltender.program_impact_min(PROGRAMS["tempo walk 3x6"]) == 0  # 4.0 mph is a walk
    # ramps are transit and goals have no fixed duration, so neither counts
    assert milltender.program_impact_min(
        [{"type": "ramp", "rate_mph": 0.1, "per_s": 30, "until_mph": 5.0},
         {"type": "goal", "mph": 5.0, "miles": 1.0}]) == 0


def test_session_impact_uses_real_gaps_and_ignores_walking(daemon):
    fast = {"samples": [[2 * i, 4.8, 120, 1, None, 170] for i in range(31)]}
    secs, spm = milltender.session_impact(fast)
    assert secs == 60 and spm == 170  # 2 s spacing summed, not assumed to be 1 Hz
    assert milltender.session_impact({"samples": [[i, 4.0, 120, 1, None, 130]
                                                  for i in range(60)]}) == (0.0, None)
    # an inactive stretch and a dropout hole both contribute nothing
    assert milltender.session_impact({"samples": [[0, 4.8, 120, 0, None, 170],
                                                  [1, 4.8, 120, 1, None, 170],
                                                  [99, 4.8, 120, 1, None, 170]]}) == (0.0, None)


def test_days_mode_marks_the_slot_the_prescribed_program_ran(daemon):
    seed(daemon, [{"mon": "walk-jog 8x1", "wed": "tempo walk 3x6"}])
    walked("walk-a", at(MONDAY), program="walk-jog 8x1", mph=4.8, minutes=8)
    snap = daemon._plan_snapshot(at(MONDAY, 18))
    mon = next(s for s in snap["week_plan"] if s["day"] == "mon")
    assert mon["state"] == "done" and mon["jog_min"] == 8.0 and mon["impact"] is True
    assert snap["today"]["program"] == "walk-jog 8x1" and snap["today"]["done"] is True
    assert snap["jog_min"] == 8.0 and snap["jog_min_plan"] == 8.0
    assert next(s for s in snap["week_plan"] if s["day"] == "tue")["state"] == "rest"
    assert snap["next"]["program"] == "tempo walk 3x6" and snap["next"]["in_days"] == 2


def test_days_mode_ignores_a_session_that_ran_something_else(daemon):
    seed(daemon, [{"mon": "walk-jog 8x1"}])
    walked("walk-a", at(MONDAY), program="easy 30")
    snap = daemon._plan_snapshot(at(MONDAY, 18))
    assert snap["today"]["done"] is False
    assert snap["sessions_today"] == [{"program": "easy 30", "moving_s": 1800}]


def test_open_mode_consumes_each_session_once(daemon):
    seed(daemon, [["walk-jog 8x1", "easy 30", "easy 30"]], mode="open")
    walked("walk-a", at(MONDAY), program="easy 30")
    snap = daemon._plan_snapshot(at(MONDAY, 18))
    states = [s["state"] for s in snap["week_plan"]]
    assert states.count("done") == 1  # one of the two easy 30s, not both
    assert snap["week_plan"][0]["state"] == "next"  # the jog is still owed


def test_open_mode_holds_an_impact_session_inside_the_recovery_window(daemon):
    seed(daemon, [["walk-jog 6x2", "easy 30"]], mode="open")
    walked("walk-jog", at(MONDAY) - 12 * 3600, program="walk-jog 8x1", mph=4.8, minutes=8)
    snap = daemon._plan_snapshot(at(MONDAY, 12))
    assert snap["week_plan"][0]["state"] == "held"
    assert snap["week_plan"][1]["state"] == "next"
    assert snap["today"]["program"] == "easy 30"  # the walk is offered instead
    assert snap["warn"]["kind"] == "spaced"


def test_open_mode_offers_the_impact_session_once_rested(daemon):
    seed(daemon, [["walk-jog 6x2", "easy 30"]], mode="open")
    walked("walk-old", at(MONDAY) - 72 * 3600, program="walk-jog 8x1", mph=4.8, minutes=8)
    snap = daemon._plan_snapshot(at(MONDAY, 12))
    assert snap["week_plan"][0]["state"] == "next" and snap["warn"] is None


def test_days_mode_warns_when_a_jog_day_lands_too_soon(daemon):
    seed(daemon, [{"mon": "walk-jog 6x2"}])
    walked("walk-jog", at(MONDAY) - 20 * 3600, program="walk-jog 8x1", mph=4.8, minutes=8)
    snap = daemon._plan_snapshot(at(MONDAY, 12))
    assert snap["warn"]["kind"] == "too_soon"
    assert snap["today"]["program"] == "walk-jog 6x2"  # reported, never withheld
    assert snap["hours_since_impact"] == 20.0


def test_a_renamed_program_is_flagged_rather_than_offered(daemon):
    seed(daemon, [{"mon": "walk-jog 9x9"}])
    snap = daemon._plan_snapshot(at(MONDAY, 12))
    assert snap["today"]["known"] is False and snap["today"]["minutes"] == 0


def test_weekly_pairs_completed_jog_minutes_against_the_ramp(daemon):
    seed(daemon, [{"mon": "walk-jog 8x1"}, {"mon": "walk-jog 6x2"}])
    walked("walk-a", at(MONDAY), program="walk-jog 8x1", mph=4.8, minutes=8)
    snap = daemon._plan_snapshot(at(MONDAY + timedelta(days=8), 12))
    assert snap["weekly"] == [{"week": 1, "plan_min": 8.0, "jog_min": 8.0},
                              {"week": 2, "plan_min": 12.0, "jog_min": 0.0}]
    assert snap["week"] == 2


def test_snapshot_echoes_the_authored_plan_for_the_editor(daemon):
    # the derived view reports a week count, so the editor needs the weeks themselves
    seed(daemon, [{"mon": "easy 30"}, {"mon": "walk-jog 8x1"}])
    for when in (at(MONDAY - timedelta(days=3)), at(MONDAY)):  # before it opens, and inside it
        snap = daemon._plan_snapshot(when)
        assert snap["plan"]["weeks"] == [{"mon": "easy 30"}, {"mon": "walk-jog 8x1"}]
        assert snap["weeks"] == 2 and snap["impact_mph"] == milltender.IMPACT_MPH


async def test_plan_shift_moves_the_anchor_and_clamps(daemon):
    seed(daemon, [{"mon": "easy 30"}])
    await daemon.h_plan_shift(FakeRequest({"weeks": -1}))  # repeat this week
    assert json.loads(milltender.PLAN_FILE.read_text())["start"] == "2026-08-17"
    await daemon.h_plan_shift(FakeRequest({"weeks": 999}))
    assert json.loads(milltender.PLAN_FILE.read_text())["start"] == "2025-08-18"


async def test_saving_a_plan_validates_and_normalises(daemon):
    await daemon.h_plan_save(FakeRequest(
        {"name": " block ", "start": "2026-08-12", "mode": "days",
         "weeks": [{"mon": "easy 30", "xxx": "junk", "tue": ""}]}))
    saved = json.loads(milltender.PLAN_FILE.read_text())
    assert saved == {"name": "block", "start": "2026-08-12", "mode": "days",
                     "weeks": [{"mon": "easy 30"}]}  # unknown keys and blanks dropped
    with pytest.raises(web.HTTPBadRequest):
        await daemon.h_plan_save(FakeRequest({"start": "2026-08-10", "weeks": []}))
    with pytest.raises(web.HTTPBadRequest):
        await daemon.h_plan_save(FakeRequest({"start": "whenever", "weeks": [{"mon": "a"}]}))


async def test_no_plan_file_is_quiet(daemon):
    assert daemon._plan_snapshot(time.time()) == {}
    resp = await daemon.h_plan(FakeRequest())
    assert json.loads(resp.body) == {}
    await daemon.h_plan_delete(FakeRequest())  # deleting nothing is not an error


def test_sidecars_from_before_the_feature_still_count(daemon):
    seed(daemon, [{"mon": "walk-jog 8x1"}])
    (milltender.SESSIONS_DIR / "walk-old.json").write_text(json.dumps({
        "start": at(MONDAY), "duration_s": 480, "moving_s": 480, "dist_m": 640, "steps": 900,
        "samples": [[i, 4.8, 120, 1, None, 170] for i in range(481)]}))  # no "program" key
    snap = daemon._plan_snapshot(at(MONDAY, 18))
    assert snap["jog_min"] == 8.0  # jog minutes come from the samples, not the label
    assert snap["week_plan"][0]["state"] == "today"  # unlabelled, so it claims no slot
