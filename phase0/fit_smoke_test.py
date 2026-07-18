#!/usr/bin/env python3
"""End-to-end upload smoke test: synthetic walking FIT -> Strava + Garmin Connect.

Proves the whole Plan-A upload path before any hardware exists, and lets us
observe whether Garmin's Strava auto-sync duplicates the activity on Strava
(we upload to Strava directly, so if you have Garmin->Strava sync enabled,
check Strava afterwards for a duplicate).

One-time setup, credentials in .env next to this repo's README:

  Strava (needs a paid sub as of June 2026 to hold an API app):
    1. Create an app at https://www.strava.com/settings/api  -> client id/secret
    2. Authorize your own account with activity:write scope — open in browser:
       https://www.strava.com/oauth/authorize?client_id=<ID>&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:write
       Copy `code=` from the redirect URL, then:
       curl -X POST https://www.strava.com/oauth/token -d client_id=<ID> -d client_secret=<SECRET> -d code=<CODE> -d grant_type=authorization_code
    3. Put in .env: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN

  Garmin (unofficial API — see PLAN.md caveats):
    .env: GARMIN_EMAIL, GARMIN_PASSWORD   (MFA prompt appears on first login;
    tokens cached in ~/.garminconnect thereafter)

Usage:
  python fit_smoke_test.py [--minutes 10] [--mph 2.0] [--skip-strava] [--skip-garmin]
"""

import argparse
import datetime as dt
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

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

MPH_TO_MPS = 0.44704
WALK_CADENCE_SPM = 105  # steps/min, plausible slow-walk cadence for the fake data


def build_fit(path: Path, minutes: int, mph: float) -> Path:
    """Synthetic steady walk ending 5 minutes ago."""
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    start = end - dt.timedelta(minutes=minutes)
    start_ms = int(start.timestamp() * 1000)
    total_s = minutes * 60
    speed_mps = mph * MPH_TO_MPS
    total_m = speed_mps * total_s
    total_steps = round(WALK_CADENCE_SPM * minutes)

    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 0x5EED
    file_id.time_created = start_ms
    builder.add(file_id)

    timer_start = EventMessage()
    timer_start.event = Event.TIMER
    timer_start.event_type = EventType.START
    timer_start.timestamp = start_ms
    builder.add(timer_start)

    for sec in range(0, total_s + 1, 10):
        rec = RecordMessage()
        rec.timestamp = start_ms + sec * 1000
        rec.distance = speed_mps * sec
        rec.speed = speed_mps
        rec.cadence = WALK_CADENCE_SPM // 2  # FIT cadence = full cycles (strides)/min
        builder.add(rec)

    timer_stop = EventMessage()
    timer_stop.event = Event.TIMER
    timer_stop.event_type = EventType.STOP_ALL
    timer_stop.timestamp = start_ms + total_s * 1000
    builder.add(timer_stop)

    lap = LapMessage()
    lap.timestamp = start_ms + total_s * 1000
    lap.start_time = start_ms
    lap.total_elapsed_time = float(total_s)
    lap.total_timer_time = float(total_s)
    lap.total_distance = total_m
    builder.add(lap)

    session = SessionMessage()
    session.timestamp = start_ms + total_s * 1000
    session.start_time = start_ms
    session.total_elapsed_time = float(total_s)
    session.total_timer_time = float(total_s)
    session.sport = Sport.WALKING
    session.sub_sport = SubSport.TREADMILL
    session.total_distance = total_m
    session.avg_speed = speed_mps
    session.total_strides = total_steps // 2
    session.total_calories = round(0.57 * 170 * (total_m / 1609.34))  # rough walking kcal
    builder.add(session)

    activity = ActivityMessage()
    activity.timestamp = start_ms + total_s * 1000
    activity.total_timer_time = float(total_s)
    activity.num_sessions = 1
    builder.add(activity)

    builder.build().to_file(str(path))
    print(f"built {path} ({minutes} min @ {mph} mph, {total_m:.0f} m, ~{total_steps} steps)")
    return path


def upload_strava(fit_path: Path) -> None:
    token = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30).json()["access_token"]

    with fit_path.open("rb") as fh:
        upload = requests.post(
            "https://www.strava.com/api/v3/uploads",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": fh},
            data={"data_type": "fit", "name": "TX6 smoke test",
                  "trainer": "true", "external_id": fit_path.stem},
            timeout=60,
        ).json()
    print(f"strava upload id={upload.get('id')} status={upload.get('status')!r} error={upload.get('error')!r}")
    for _ in range(20):
        time.sleep(1.5)
        status = requests.get(
            f"https://www.strava.com/api/v3/uploads/{upload['id']}",
            headers={"Authorization": f"Bearer {token}"}, timeout=30).json()
        if status.get("activity_id") or status.get("error"):
            print(f"strava result: activity_id={status.get('activity_id')} error={status.get('error')!r}")
            return
    print("strava: still processing after 30s; check the app")


def upload_garmin(fit_path: Path) -> None:
    from garminconnect import Garmin
    tokenstore = os.path.expanduser("~/.garminconnect")
    try:
        client = Garmin()
        client.login(tokenstore)
    except Exception:
        client = Garmin(email=os.environ["GARMIN_EMAIL"],
                        password=os.environ["GARMIN_PASSWORD"],
                        prompt_mfa=lambda: input("Garmin MFA code: "))
        client.login()
        client.garth.dump(tokenstore)
    result = client.upload_activity(str(fit_path))
    print(f"garmin upload: {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--mph", type=float, default=2.0)
    parser.add_argument("--skip-strava", action="store_true")
    parser.add_argument("--skip-garmin", action="store_true")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    out = Path(__file__).resolve().parent / f"smoke-{int(time.time())}.fit"
    build_fit(out, args.minutes, args.mph)
    if not args.skip_strava:
        upload_strava(out)
    if not args.skip_garmin:
        upload_garmin(out)
    print("\nNow check: Strava activity present? Garmin activity present with steps?")
    print("If Garmin->Strava sync is enabled, did Strava get a DUPLICATE? (see README #4)")


if __name__ == "__main__":
    main()
