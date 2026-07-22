#!/usr/bin/env python3
"""Treadmill daemon: LifeSpan TX6 Glow-Up (FitShow BLE) -> FIT -> Strava + Garmin.

Runs forever: connects to the treadmill, polls status at 1 Hz, auto-detects
sessions (state RUNNING), records speed/distance/steps/kcal plus heart rate and
RR intervals from an optional BLE HR strap, and on session end builds a FIT in
sessions/ and uploads it to both platforms.

Environment (.env): TREADMILL_ADDRESS, HRM_ADDRESS (optional; "auto" scans for
any Heart Rate service device when a session starts).

Run:  python milltender.py            (Garmin needs one-time `python uploads.py garmin-login`)
"""

import asyncio
import dataclasses
import json
import logging
import math
import os
import signal
import time
from pathlib import Path

from aiohttp import web
from bleak import BleakClient, BleakScanner
from dotenv import load_dotenv

import uploads
from fit_build import Sample, build_fit, cadence_series

load_dotenv(Path(__file__).resolve().parent / ".env")

TREADMILL_ADDRESS = os.environ.get("TREADMILL_ADDRESS", "9C7EC52B-C7C8-4765-8DC5-242A1D94B663")
TREADMILL_NAME_PREFIX = os.environ.get("TREADMILL_NAME_PREFIX", "FS-")
HRM_ADDRESS = os.environ.get("HRM_ADDRESS", "auto")

WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
HR_SERVICE = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"

MPH_TO_MPS = 0.44704
MAX_MPH = float(os.environ.get("MAX_MPH", "6.0"))  # hard ceiling on daemon-commanded speed
WEB_PORT = int(os.environ.get("WEB_PORT", "8321"))
GRACE_S = int(os.environ.get("GRACE_S", "180"))  # resume window after belt stop
RECOVERY_S = int(os.environ.get("RECOVERY_S", "60"))  # HR capture after deliberate stop
MIN_SESSION_S = 60
MIN_SESSION_STEPS = 20
SESSIONS_DIR = Path(__file__).resolve().parent / "sessions"
PROGRAMS_FILE = Path(__file__).resolve().parent / "programs.json"

ST_RUNNING, ST_PAUSED = 3, 10
END_STATES = {0, 1, 4}  # NORMAL, END, STOP

log = logging.getLogger("milltender")


def frame(payload: bytes) -> bytes:
    xor = 0
    for b in payload:
        xor ^= b
    return bytes([0x02, *payload, xor, 0x03])


def recovery_analysis(samples: list[Sample]) -> dict | None:
    """Heart-rate recovery after the belt stops: HRR at 30 and 60 s, and the
    exponential decay time constant tau (lower = faster recovery). The signal is
    weak at walking intensity (small HR excursion), so every field is guarded and
    the whole thing is None when there was no real drop to measure."""
    last_active = next((s for s in reversed(samples) if s.active), None)
    if not last_active or not last_active.hr:
        return None
    t0, peak = last_active.t, last_active.hr
    pts = [(s.t - t0, s.hr) for s in samples if not s.active and s.hr and s.t >= t0]
    if len(pts) < 5 or pts[-1][0] < 25:
        return None

    def hrr_at(sec: int) -> int | None:
        # median of the ±10 s window, so one strap dropout can't swing it
        win = sorted(hr for t, hr in pts if abs(t - sec) <= 10)
        return peak - win[len(win) // 2] if win else None

    hrr30, hrr60 = hrr_at(30), hrr_at(60)

    # tau needs a real, sustained drop behind it: below ~5 bpm the fit is chasing
    # noise. Gate on the median-windowed HRR (immune to a lone bad sample), never
    # a single minimum.
    real_drop = max((d for d in (hrr30, hrr60) if d is not None), default=0)
    tau, lo, hi = None, 8, 200
    if real_drop >= 5:
        best_resid = None
        for candidate in range(lo, hi + 1, 2):  # grid-search tau; (c, a) linear given it
            xs = [math.exp(-t / candidate) for t, _ in pts]
            ys = [hr for _, hr in pts]
            n = len(pts)
            sx, sy = sum(xs), sum(ys)
            denom = n * sum(x * x for x in xs) - sx * sx
            if abs(denom) < 1e-9:
                continue
            a = (n * sum(x * y for x, y in zip(xs, ys)) - sx * sy) / denom
            c = (sy - a * sx) / n
            resid = sum((y - (c + a * x)) ** 2 for x, y in zip(xs, ys))
            if a > 0 and (best_resid is None or resid < best_resid):
                best_resid, tau = resid, candidate
        if tau == hi:  # railed at the ceiling = "barely recovering", not a real constant
            tau = None

    if hrr30 is None and hrr60 is None and tau is None:
        return None
    return {"peak": peak, "hrr30": hrr30, "hrr60": hrr60, "tau": tau}


def cardiac_drift(samples: list[Sample]) -> float | None:
    """Aerobic decoupling: at the paces walked in both halves of a session, did
    the heart work harder in the second half? Matching on pace controls for a
    variable walk, so this holds up where a plain efficiency ratio wouldn't. None
    for short sessions or when the halves share no pace. Positive % = HR higher
    later at the same pace (drift, less durable); under ~5% is solid."""
    active = [s for s in samples if s.active and s.hr and s.speed_mps > 0.1]
    if len(active) < 60 or active[-1].t - active[0].t < 1200:  # need ~20 min
        return None
    core = [s for s in active if s.t >= active[0].t + 180]  # drop warmup: HR still climbing
    if len(core) < 40 or core[-1].t - core[0].t < 600:
        return None
    mid = core[0].t + (core[-1].t - core[0].t) / 2

    def by_pace(half: list[Sample]) -> dict[float, list[int]]:
        buckets: dict[float, list[int]] = {}
        for s in half:
            buckets.setdefault(round(s.speed_mps / MPH_TO_MPS, 1), []).append(s.hr)
        return {mph: hrs for mph, hrs in buckets.items() if len(hrs) >= 20}

    first = by_pace([s for s in core if s.t < mid])
    second = by_pace([s for s in core if s.t >= mid])
    num = den = 0.0
    for mph in set(first) & set(second):  # weighted by time spent at each shared pace
        h1 = sum(first[mph]) / len(first[mph])
        h2 = sum(second[mph]) / len(second[mph])
        w = min(len(first[mph]), len(second[mph]))
        num, den = num + w * (h2 - h1) / h1, den + w
    return round(num / den * 100, 1) if den else None


def clamp_mph(mph: float) -> float:
    """Every daemon-commanded speed passes through here. The remote is hardware
    and beyond our reach; everything we send respects MAX_MPH."""
    return max(0.4, min(MAX_MPH, mph))


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Daemon:
    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.rr_events: list[tuple[float, float]] = []
        self.dist_m = 0.0
        self.last_sample_t = 0.0
        self.latest_hr: int | None = None
        self.hr_last_seen = 0.0
        self.in_session = False
        self.hrm_task: asyncio.Task | None = None
        self.client: BleakClient | None = None
        self.latest: dict = {}  # live state for the web UI
        self.pending_end_t: float | None = None  # belt stopped; grace timer running
        self.user_paused = False  # explicit UI pause: hold session open, no grace timer
        self.program_task: asyncio.Task | None = None
        self.program_status: dict | None = None  # {name, seg, total, seg_left_s}
        self.recovery_until: float | None = None
        self.stop_task: asyncio.Task | None = None
        self.hr_baseline: dict[float, float] = {}  # mph -> typical bpm, from archive
        self._recent_hr: list[tuple[float, float, int]] = []  # (t, mph, hr)
        self._fail_count = 0
        self._last_raw_log = 0.0
        self._reset_guard_until = 0.0
        self.raw_dist_m = 0.0
        self.anchor_ok = True
        self.steps_offset = 0
        self.kcal_offset = 0.0

    # ---------- treadmill ----------

    def rebuild_hr_baseline(self) -> None:
        """Personal typical-HR-per-speed from the session archive (steady state:
        active samples past the first 3 min; buckets need >= 2 min of data)."""
        buckets: dict[float, list[float]] = {}
        for sidecar in SESSIONS_DIR.glob("walk-*.json"):
            try:
                for p in json.loads(sidecar.read_text()).get("samples", []):
                    if len(p) >= 4 and p[2] and p[3] == 1 and p[0] > 180 and p[1] >= 0.4:
                        buckets.setdefault(round(p[1] * 10) / 10, []).append(p[2])
            except Exception:  # noqa: BLE001
                continue
        self.hr_baseline = {mph: sum(v) / len(v) for mph, v in buckets.items()
                            if len(v) >= 120}
        log.info("HR baseline rebuilt: %s",
                 {m: round(b) for m, b in sorted(self.hr_baseline.items())} or "insufficient data")

    def _hrv_archive_baselines(self) -> list[float]:
        out = []
        for sidecar in SESSIONS_DIR.glob("walk-*.json"):
            try:
                v = json.loads(sidecar.read_text()).get("hrv_baseline")
                if v:
                    out.append(v)
            except Exception:  # noqa: BLE001
                continue
        return out

    async def run(self) -> None:
        SESSIONS_DIR.mkdir(exist_ok=True)
        self.rebuild_hr_baseline()
        await self.start_web()
        while True:
            try:
                address = await self.find_treadmill()
                await self.poll_treadmill(address)
            except Exception as exc:  # noqa: BLE001 — daemon survives all BLE weather
                self._fail_count += 1
                if self._fail_count == 1 or self._fail_count % 100 == 0:
                    log.warning("treadmill unreachable (asleep is normal when idle): %s "
                                "(attempt %d; retrying quietly)", exc, self._fail_count)
            if self.in_session:
                # disconnect mid-walk: give it one quick retry cycle before finalizing
                if time.time() - self.last_sample_t > 90:
                    log.warning("no data for 90s during session; finalizing with what we have")
                    await self.finalize()
            await asyncio.sleep(5 if self.in_session else 15)

    async def find_treadmill(self) -> str:
        try:
            device = await BleakScanner.find_device_by_address(TREADMILL_ADDRESS, timeout=10)
            if device:
                return device.address
        except Exception:  # noqa: BLE001
            pass
        log.info("configured address not found; scanning for name prefix %r", TREADMILL_NAME_PREFIX)
        for device in await BleakScanner.discover(timeout=10):
            if (device.name or "").startswith(TREADMILL_NAME_PREFIX):
                log.info("found treadmill %s at %s", device.name, device.address)
                return device.address
        raise ConnectionError("treadmill not found (asleep? in use by the LifeSpan app?)")

    async def poll_treadmill(self, address: str) -> None:
        async with BleakClient(address, timeout=20.0) as client:
            log.info("connected to treadmill %s", address)
            self._fail_count = 0
            self.client = client
            try:
                await client.start_notify(NOTIFY_CHAR, self.on_treadmill_frame)
                while client.is_connected:
                    await client.write_gatt_char(WRITE_CHAR, frame(bytes([0x51])), response=False)
                    if (self.pending_end_t and not self.user_paused
                            and time.time() - self.pending_end_t > GRACE_S):
                        await self.finalize()
                        await self.reset_base()
                    await asyncio.sleep(1.0)
            finally:
                self.client = None
                self.latest.pop("state", None)  # last-known state is stale once disconnected

    def on_treadmill_frame(self, _char, data: bytearray) -> None:
        data = bytes(data)
        if len(data) < 4 or data[0] != 0x02 or data[-1] != 0x03:
            return
        payload = data[1:-2]
        xor = 0
        for b in payload:
            xor ^= b
        if xor != data[-2]:
            return  # corrupt frame; the next poll is a second away
        if not payload or payload[0] != 0x51 or len(payload) < 2:
            return
        state = payload[1]
        self.latest["state"] = state
        self.latest["ts"] = time.time()
        if state == ST_RUNNING and len(payload) >= 12:
            if self.pending_end_t is not None:
                log.info("belt resumed within grace period — continuing the session")
                self.pending_end_t = None
            self.on_running_data(payload)
        elif state in END_STATES and self.in_session and self.pending_end_t is None:
            self.pending_end_t = time.time()
            if not self.user_paused:
                log.info("belt stopped — uploading in %ds unless resumed", GRACE_S)
        if self.in_session and state != ST_RUNNING and self.samples:
            # belt paused/stopped but session open: keep recording HR at 1 Hz
            now = time.time()
            if now - self.last_sample_t >= 0.9:
                prev = self.samples[-1]
                hr = self.latest_hr if now - self.hr_last_seen < 10 else None
                hrv = self._hrv_rmssd(now)
                self.latest["hrv_ms"] = hrv
                self.samples.append(Sample(t=now, speed_mps=0.0, dist_m=self.dist_m,
                                           steps=prev.steps, kcal=prev.kcal, hr=hr,
                                           active=False, hrv=hrv))
                self.last_sample_t = now
                self.latest["cadence_spm"] = self._live_cadence()

    def on_running_data(self, payload: bytes) -> None:
        now = time.time()
        if now - self._last_raw_log > 60:
            self._last_raw_log = now
            log.info("raw running frame: %s", payload.hex(" "))
        speed_mps = payload[2] / 10.0 * MPH_TO_MPS
        elapsed_s = payload[4] | (payload[5] << 8)
        raw_dist_m = (payload[6] | (payload[7] << 8)) * 1.60934  # base counts 0.001 mi
        kcal = (payload[8] | (payload[9] << 8)) / 10.0
        steps = payload[10] | (payload[11] << 8)
        self.latest.update(speed_mph=payload[2] / 10.0, elapsed_s=elapsed_s,
                           dist_m=raw_dist_m, kcal=kcal, steps=steps)
        if not self.in_session:
            self.begin_session(now, elapsed_s, raw_dist_m, steps, kcal)
        prev = self.samples[-1] if self.samples else None
        if prev is not None and now < self._reset_guard_until \
                and abs((steps + self.steps_offset) - prev.steps) > 25:
            return  # stale RAM frame right after a counter reset — drop it
        if prev is not None and steps + self.steps_offset < prev.steps - 5:
            if prev.t - self.samples[0].t < 10:
                # The base's first frame(s) after a start can carry stale RAM
                # counters; a backward jump this early is that glitch, not a
                # reset — discard the poisoned opening samples and rebase.
                log.info("stale counter glitch at session start (%d) — rebasing", prev.steps)
                self.samples.clear()
                self.dist_m = 0.0
                self.steps_offset = 0
                self.kcal_offset = 0.0
            else:
                # Base did a full stop+restart mid-session (e.g. resume after
                # step-off auto-stop): its counters reset — carry them forward.
                log.info("base counters reset mid-session — continuing from %d steps", prev.steps)
                self.steps_offset = prev.steps
                self.kcal_offset = prev.kcal
                self.anchor_ok = False  # base odometer no longer covers the whole session
                self._reset_guard_until = now + 8  # distrust frames while base settles
        steps += self.steps_offset
        kcal += self.kcal_offset
        if self.samples:
            self.dist_m += speed_mps * min(now - self.last_sample_t, 5.0)
        self.raw_dist_m = raw_dist_m
        hr = self.latest_hr if now - self.hr_last_seen < 10 else None
        hrv = self._hrv_rmssd(now)
        self.latest["hrv_ms"] = hrv
        self.samples.append(Sample(t=now, speed_mps=speed_mps, dist_m=self.dist_m,
                                   steps=steps, kcal=kcal, hr=hr, hrv=hrv))
        self.last_sample_t = now
        self.latest["cadence_spm"] = self._live_cadence()
        self._update_freshness(now, payload[2] / 10.0, hr)

    def _update_freshness(self, now: float, mph: float, hr: int | None) -> None:
        """Live HR-at-speed deviation vs the personal archive baseline."""
        if hr:
            self._recent_hr.append((now, mph, hr))
        self._recent_hr = [r for r in self._recent_hr if now - r[0] <= 75]
        fresh = None
        if self.samples and self.samples[-1].t - self.samples[0].t > 180:
            window = [r for r in self._recent_hr if now - r[0] <= 60]
            bucket = round(mph * 10) / 10
            typical = self.hr_baseline.get(bucket)
            if typical and len(window) >= 45 and all(r[1] == bucket for r in window):
                fresh = round(typical - sum(r[2] for r in window) / len(window), 1)
        self.latest["fresh_bpm"] = fresh
        self.latest["fresh_mph"] = round(mph * 10) / 10 if fresh is not None else None
        age = self.samples[-1].t - self.samples[0].t if self.samples else 0
        if "readiness" not in self.latest and 150 <= age <= 240:
            early = sorted(s.hrv for s in self.samples if s.hrv)
            if early:
                mine = round(early[len(early) // 2], 1)
                hist = self._hrv_archive_baselines()
                typ = sd = None
                if len(hist) >= 4:
                    typ = sum(hist) / len(hist)
                    sd = (sum((v - typ) ** 2 for v in hist) / len(hist)) ** 0.5
                self.latest["readiness"] = {"hrv": mine,
                                            "typ": round(typ, 1) if typ else None,
                                            "sd": round(sd, 1) if sd else None}
                log.info("session HRV baseline: %.1f ms (typical %s)", mine,
                         f"{typ:.1f}±{sd:.1f}" if typ and sd else "n/a yet")

    def _live_cadence(self) -> float:
        """Steps/min over the trailing 20 s of samples."""
        if len(self.samples) < 2:
            return 0.0
        return cadence_series(self.samples[-25:])[-1]  # 25 samples spans the window at 1 Hz

    def _hrv_rmssd(self, now: float) -> float | None:
        """Rolling rMSSD (ms) over the last 60 s of RR intervals, artifact-filtered."""
        rr = [v for t, v in self.rr_events[-150:] if now - t <= 60]
        if len(rr) < 10:
            return None
        diffs = [(b - a) * 1000 for a, b in zip(rr, rr[1:])
                 if abs(b - a) / a <= 0.2]  # drop ectopic/dropout artifacts
        if len(diffs) < 5:
            return None
        return round((sum(d * d for d in diffs) / len(diffs)) ** 0.5, 1)

    def begin_session(self, now: float, elapsed_s: int, raw_dist_m: float,
                      steps: int, kcal: float) -> None:
        self.in_session = True
        self.samples, self.rr_events = [], []
        for key in ("hrv_ms", "fresh_bpm", "fresh_mph", "readiness"):
            self.latest.pop(key, None)
        self._recent_hr = []
        self.dist_m = 0.0
        self.raw_dist_m = 0.0
        self.anchor_ok = True
        self.steps_offset = 0
        self.kcal_offset = 0.0
        cadence_spm = steps / (elapsed_s / 60) if elapsed_s else 0
        if elapsed_s > 10 and (steps < 20 or cadence_spm < 15):
            # Counters carried over from a paused/earlier session with no real
            # walking behind them (the base continues elapsed/distance across
            # pause) — backfilling would fabricate a phantom walk.
            log.info("base reports %ds/%0.f m but only %d steps — ignoring "
                     "pre-join counters, recording fresh", elapsed_s, raw_dist_m, steps)
            self.anchor_ok = False  # base odometer includes unwalked distance
        elif elapsed_s > 10:
            # Joined mid-session: the base's counters are cumulative since ITS
            # session start, so backfill the missed part at average pace.
            log.info("joined session already %ds in (%d steps, %.0f m) — backfilling",
                     elapsed_s, steps, raw_dist_m)
            start_t = now - elapsed_s
            avg_speed = raw_dist_m / elapsed_s
            n = min(max(2, elapsed_s // 2), 3600)  # keep spacing under fit_build's pause gap
            for i in range(n):
                f = i / n
                self.samples.append(Sample(t=start_t + f * elapsed_s, speed_mps=avg_speed,
                                           dist_m=raw_dist_m * f, steps=round(steps * f),
                                           kcal=kcal * f, hr=None))
            self.dist_m = raw_dist_m
            self.last_sample_t = self.samples[-1].t
        log.info("session started")
        if self.hrm_task is None or self.hrm_task.done():
            self.hrm_task = asyncio.get_running_loop().create_task(self.hrm_loop())

    async def reset_base(self) -> None:
        """BLE stop clears the base's session state (unlike the remote's stop,
        which only pauses) — prevents stale counters on the next start."""
        try:
            if self.client and self.client.is_connected:
                await self.client.write_gatt_char(WRITE_CHAR, frame(bytes([0x53, 0x03])),
                                                  response=False)
                log.info("base session reset")
        except Exception as exc:  # noqa: BLE001
            log.warning("base reset failed: %s", exc)

    async def finalize(self) -> None:
        if not self.in_session:
            return
        self.in_session = False  # first: hrm_loop watches this
        self.pending_end_t = None
        self.user_paused = False
        if self.program_task and not self.program_task.done():
            self.program_task.cancel()
        samples, rr = self.samples, self.rr_events
        self.samples, self.rr_events = [], []
        if len(samples) < 2:
            return
        duration = samples[-1].t - samples[0].t
        steps = samples[-1].steps - samples[0].steps
        if duration < MIN_SESSION_S or steps < MIN_SESSION_STEPS:
            log.info("discarding tiny session (%.0fs, %d steps)", duration, steps)
            return
        if self.anchor_ok and self.raw_dist_m and samples[-1].dist_m:
            # anchor smooth integrated distance to the base's own odometer
            factor = self.raw_dist_m / samples[-1].dist_m
            if 0.8 < factor < 1.2 and abs(factor - 1) > 0.01:
                log.info("distance anchor: scaling by %.3f to match base odometer", factor)
                for s in samples:
                    s.dist_m *= factor
        try:
            fit_path = build_fit(samples, SESSIONS_DIR, rr_events=rr)
        except Exception:
            # Never lose a session to an encoding bug: dump raw data for offline rebuild.
            dump = SESSIONS_DIR / time.strftime("failed-%Y%m%d-%H%M%S.json")
            dump.write_text(json.dumps({"samples": [dataclasses.asdict(s) for s in samples],
                                        "rr_events": rr}))
            log.exception("FIT build failed — raw session dumped to %s", dump)
            return
        log.info("session ended: %.1f min, %d steps, %.0f m, hr samples: %d -> %s",
                 duration / 60, steps, samples[-1].dist_m,
                 sum(1 for s in samples if s.hr), fit_path.name)
        hrs = [s.hr for s in samples if s.hr]
        recovery = recovery_analysis(samples)
        drift = cardiac_drift(samples)
        if recovery:
            log.info("recovery: HRR60=%s HRR30=%s tau=%s",
                     recovery["hrr60"], recovery["hrr30"], recovery["tau"])
        if drift is not None:
            log.info("cardiac drift: %+.1f%%", drift)
        early = sorted(s.hrv for s in samples if s.hrv and s.t - samples[0].t <= 150)
        hrv_baseline = round(early[len(early) // 2], 1) if early else None
        write_atomic(fit_path.with_suffix(".json"), json.dumps({
            "start": samples[0].t, "duration_s": round(duration),
            "dist_m": round(samples[-1].dist_m, 1), "steps": steps,
            "recovery": recovery, "hrr60": recovery and recovery["hrr60"],
            "drift_pct": drift, "hrv_baseline": hrv_baseline,
            "kcal": round(samples[-1].kcal - samples[0].kcal),
            "hr_avg": round(sum(hrs) / len(hrs)) if hrs else None,
            "samples": [[round(s.t - samples[0].t, 1), round(s.speed_mps / MPH_TO_MPS, 2),
                         s.hr, int(s.active), s.hrv, cad]
                        for s, cad in zip(samples, cadence_series(samples))],
        }))
        await asyncio.to_thread(self.rebuild_hr_baseline)
        name = f"Treadmill walk ({steps} steps)"
        results = await asyncio.to_thread(uploads.upload_all, fit_path, name)
        for platform, res in results.items():
            if res["ok"]:
                log.info("%s upload OK: %s", platform, res["result"])
            else:
                log.error("%s upload FAILED: %s (file kept: %s)", platform, res["error"], fit_path)

    # ---------- web UI ----------

    async def start_web(self) -> None:
        app = web.Application()
        app.router.add_get("/", self.h_index)
        app.router.add_get("/api/status", self.h_status)
        app.router.add_get("/api/history", self.h_history)
        app.router.add_post("/api/speed", self.h_speed)
        app.router.add_post("/api/start", self.h_start)
        app.router.add_post("/api/pause", self.h_pause)
        app.router.add_post("/api/stop", self.h_stop)
        app.router.add_post("/api/recovery/finish", self.h_recovery_finish)
        app.router.add_post("/api/recovery/extend", self.h_recovery_extend)
        app.router.add_get("/api/trends", self.h_trends)
        app.router.add_get("/api/sessions", self.h_sessions)
        app.router.add_get("/api/session", self.h_session)
        app.router.add_get("/api/programs", self.h_programs)
        app.router.add_post("/api/programs", self.h_program_save)
        app.router.add_post("/api/program/delete", self.h_program_delete)
        app.router.add_post("/api/program/run", self.h_program_run)
        app.router.add_post("/api/program/cancel", self.h_program_cancel)
        app.router.add_post("/api/replay", self.h_replay)
        app.router.add_static("/sessions/", SESSIONS_DIR, show_index=False)
        runner = web.AppRunner(app, access_log=None)  # keep milltender.log readable
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
        log.info("web UI: http://localhost:%d (reachable on your LAN)", WEB_PORT)

    async def h_index(self, _req: web.Request) -> web.FileResponse:
        return web.FileResponse(Path(__file__).resolve().parent / "web" / "index.html")

    async def h_status(self, _req: web.Request) -> web.Response:
        now = time.time()
        return web.json_response({
            **self.latest,
            "connected": self.client is not None,
            "in_session": self.in_session,
            "hr": self.latest_hr if now - self.hr_last_seen < 10 else None,
            "hr_connected": now - self.hr_last_seen < 10,
            "grace_s_left": (max(0, round(GRACE_S - (now - self.pending_end_t)))
                             if self.pending_end_t and not self.user_paused else None),
            "user_paused": self.user_paused,
            "program": self.program_status,
            "recovery_s_left": (max(0, round(self.recovery_until - now))
                                if self.recovery_until else None),
            "max_mph": MAX_MPH,
        })

    async def h_history(self, _req: web.Request) -> web.Response:
        samples = self.samples
        if not samples:
            return web.json_response({"t0": None, "points": []})
        step = max(1, len(samples) // 900)
        t0 = samples[0].t
        cads = cadence_series(samples)
        points = [[round(s.t - t0, 1), round(s.speed_mps / MPH_TO_MPS, 2), s.hr, s.hrv, c]
                  for s, c in zip(samples[::step], cads[::step])]
        return web.json_response({"t0": t0, "points": points})

    async def send_cmd(self, payload: bytes) -> None:
        if self.client is None or not self.client.is_connected:
            raise web.HTTPConflict(text="treadmill not connected")
        await self.client.write_gatt_char(WRITE_CHAR, frame(payload), response=False)

    async def h_speed(self, req: web.Request) -> web.Response:
        mph = float((await req.json())["mph"])
        if not math.isfinite(mph):
            raise web.HTTPBadRequest(text="mph must be a finite number")
        mph = clamp_mph(mph)
        await self.send_cmd(bytes([0x53, 0x02, round(mph * 10), 0x00]))
        log.info("web control: set speed %.1f mph", mph)
        return web.json_response({"ok": True, "mph": mph})

    async def h_start(self, _req: web.Request) -> web.Response:
        # Clear the grace timer BEFORE un-pausing: an expired timer must not
        # fire in the gap between this command and the belt's first RUNNING frame.
        self.pending_end_t = None
        self.user_paused = False
        await self.send_cmd(bytes([0x53, 0x00, 0x01, 0x00, 0x6E, 0x28, 0x50, 0xAA]))
        await asyncio.sleep(0.5)
        await self.send_cmd(bytes([0x53, 0x01, 0, 0, 0, 0, 0x00, 0x00, 0x00, 0x00]))
        log.info("web control: start")
        return web.json_response({"ok": True})

    async def h_pause(self, _req: web.Request) -> web.Response:
        # The TX6 ignores FitShow's pause opcode (53 06 — verified live), so
        # pause is daemon-held: stop the belt, keep the session open, no grace.
        if not self.in_session:
            raise web.HTTPConflict(text="no active session")
        self.user_paused = True
        await self.send_cmd(bytes([0x53, 0x03]))
        log.info("web control: pause (daemon-held)")
        return web.json_response({"ok": True})

    async def h_stop(self, _req: web.Request) -> web.Response:
        """Deliberate stop: no grace — finalize, upload, and reset right away."""
        try:
            await self.send_cmd(bytes([0x53, 0x03]))
        except web.HTTPConflict:
            pass  # treadmill unreachable; still flush whatever we recorded
        if self.stop_task and not self.stop_task.done():
            # a stop is already in flight; a second press means "wrap it up now"
            if self.recovery_until:
                self.recovery_until = time.time()
            return web.json_response({"ok": True})
        log.info("web control: stop — uploading immediately")
        self.stop_task = asyncio.get_running_loop().create_task(self._stop_flow())
        return web.json_response({"ok": True})

    async def h_recovery_finish(self, _req: web.Request) -> web.Response:
        if self.recovery_until and self.latest.get("state") != ST_RUNNING:
            self.recovery_until = time.time()
        return web.json_response({"ok": True})

    async def h_recovery_extend(self, _req: web.Request) -> web.Response:
        if self.recovery_until:
            self.recovery_until += 60
            log.info("recovery window extended to %ds",
                     round(self.recovery_until - time.time()))
        return web.json_response({"ok": True})

    async def _stop_flow(self) -> None:
        self.user_paused = False
        self.pending_end_t = None  # a stale grace timer must not race the recovery window
        await asyncio.sleep(2.0)  # let the belt spin down and final frames land
        if self.in_session and RECOVERY_S > 0:
            self.recovery_until = time.time() + RECOVERY_S
            log.info("recording %ds of recovery HR before upload", RECOVERY_S)
            while time.time() < (self.recovery_until or 0):
                await asyncio.sleep(1)
                if self.latest.get("state") == ST_RUNNING:
                    break
            self.recovery_until = None
            if self.latest.get("state") == ST_RUNNING:
                log.info("belt resumed during recovery window — continuing session")
                return
        await self.finalize()
        await self.reset_base()

    # ---------- history & programs ----------

    def _trends_snapshot(self, now: float, days: int) -> dict:
        """One pass over the archive: HR-by-speed curves for both windows + aggregates."""
        cutoff = now - days * 86400
        all_b: dict[float, list[float]] = {}
        recent_b: dict[float, list[float]] = {}
        week = {"dist_m": 0.0, "steps": 0, "seconds": 0, "sessions": 0}
        alltime = {"dist_m": 0.0, "steps": 0, "seconds": 0, "sessions": 0}
        sessions = []
        for sidecar in sorted(SESSIONS_DIR.glob("walk-*.json")):
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:  # noqa: BLE001
                continue
            start = meta.get("start") or 0
            for agg, cond in ((alltime, True), (week, start >= now - 7 * 86400)):
                if cond:
                    agg["dist_m"] += meta.get("dist_m") or 0
                    agg["steps"] += meta.get("steps") or 0
                    agg["seconds"] += meta.get("duration_s") or 0
                    agg["sessions"] += 1
            sessions.append({"start": start, "duration_s": meta.get("duration_s"),
                             "dist_m": meta.get("dist_m"), "hrr60": meta.get("hrr60"),
                             "hrv_baseline": meta.get("hrv_baseline")})
            in_window = start >= cutoff
            for p in meta.get("samples", []):
                if len(p) >= 4 and p[2] and p[3] == 1 and p[0] > 180 and p[1] >= 0.4:
                    mph = round(p[1] * 10) / 10
                    all_b.setdefault(mph, []).append(p[2])
                    if in_window:
                        recent_b.setdefault(mph, []).append(p[2])

        def curve(buckets: dict, min_n: int) -> list[dict]:
            return [{"mph": mph, "bpm": round(sum(v) / len(v), 1)}
                    for mph, v in sorted(buckets.items()) if len(v) >= min_n]

        return {"curve_all": curve(all_b, 120), "curve_recent": curve(recent_b, 60),
                "days": days, "week": week, "all": alltime,
                "recent": sessions[-14:][::-1]}

    async def h_trends(self, req: web.Request) -> web.Response:
        try:
            days = max(1, min(3650, int(req.query.get("days", 7))))
        except ValueError:
            days = 7
        snapshot = await asyncio.to_thread(self._trends_snapshot, time.time(), days)
        return web.json_response(snapshot)

    async def h_sessions(self, _req: web.Request) -> web.Response:
        out = []
        for sidecar in sorted(SESSIONS_DIR.glob("walk-*.json"), reverse=True):
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:  # noqa: BLE001
                continue
            out.append({"name": sidecar.stem, "start": meta.get("start"),
                        "duration_s": meta.get("duration_s"), "dist_m": meta.get("dist_m"),
                        "steps": meta.get("steps"), "hr_avg": meta.get("hr_avg"),
                        "hrr60": meta.get("hrr60"),
                        "fit": f"/sessions/{sidecar.stem}.fit"})
        return web.json_response(out)

    async def h_session(self, req: web.Request) -> web.Response:
        name = Path(req.query.get("name", "")).name  # sanitize
        path = SESSIONS_DIR / f"{name}.json"
        if not name.startswith("walk-") or not path.exists():
            raise web.HTTPNotFound
        return web.Response(text=path.read_text(), content_type="application/json")

    def _load_programs(self) -> dict:
        try:
            return json.loads(PROGRAMS_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}

    async def h_programs(self, _req: web.Request) -> web.Response:
        return web.json_response(self._load_programs())

    async def h_program_save(self, req: web.Request) -> web.Response:
        body = await req.json()
        name = str(body["name"]).strip()[:60]
        segments = []
        for s in list(body["segments"])[:100]:
            if s.get("type") == "ramp":
                seg = {"type": "ramp",
                       "rate_mph": max(-2.0, min(2.0, float(s["rate_mph"]))),
                       "per_s": max(2, min(300, round(float(s["per_s"]))))}
                if seg["rate_mph"] == 0:
                    continue
                if s.get("until_mph") is not None:
                    seg["until_mph"] = max(0.4, min(6.0, float(s["until_mph"])))
                else:
                    seg["minutes"] = max(0.1, float(s["minutes"]))
                segments.append(seg)
            elif s.get("type") == "hr":
                segments.append({"type": "hr",
                                 "bpm": max(60, min(190, round(float(s["bpm"])))),
                                 "minutes": max(0.1, float(s["minutes"])),
                                 "max_mph": max(0.4, min(6.0, float(s.get("max_mph", 3.0))))})
            elif s.get("type") == "goal":
                seg = {"type": "goal", "mph": max(0.4, min(6.0, float(s["mph"])))}
                if s.get("steps") is not None:
                    seg["steps"] = max(1, round(float(s["steps"])))
                else:
                    seg["miles"] = max(0.01, float(s["miles"]))
                segments.append(seg)
            else:
                segments.append({"type": "hold",
                                 "minutes": max(0.1, float(s["minutes"])),
                                 "mph": max(0.4, min(6.0, float(s["mph"])))})
        if not name or not segments:
            raise web.HTTPBadRequest(text="name and segments required")
        programs = self._load_programs()
        programs[name] = segments
        write_atomic(PROGRAMS_FILE, json.dumps(programs, indent=1))
        return web.json_response({"ok": True})

    async def h_program_delete(self, req: web.Request) -> web.Response:
        programs = self._load_programs()
        programs.pop((await req.json()).get("name"), None)
        write_atomic(PROGRAMS_FILE, json.dumps(programs, indent=1))
        return web.json_response({"ok": True})

    async def h_program_run(self, req: web.Request) -> web.Response:
        name = (await req.json()).get("name")
        segments = self._load_programs().get(name)
        if not segments:
            raise web.HTTPNotFound(text="no such program")
        self._start_program(name, segments)
        return web.json_response({"ok": True})

    async def h_program_cancel(self, _req: web.Request) -> web.Response:
        if self.program_task and not self.program_task.done():
            self.program_task.cancel()
            log.info("program cancelled (belt keeps current speed)")
        return web.json_response({"ok": True})

    async def h_replay(self, req: web.Request) -> web.Response:
        name = Path((await req.json()).get("name", "")).name
        path = SESSIONS_DIR / f"{name}.json"
        if not name.startswith("walk-") or not path.exists():
            raise web.HTTPNotFound
        samples = json.loads(path.read_text())["samples"]
        segments = []
        for t, mph, _hr, active, *_ in samples:
            if not active or mph < 0.4:
                continue
            mph = round(mph * 10) / 10
            if segments and segments[-1]["mph"] == mph:
                segments[-1]["minutes"] += 1 / 60
            else:
                segments.append({"mph": mph, "minutes": 1 / 60})
        segments = [s for s in segments if s["minutes"] * 60 >= 10]  # drop blips
        if not segments:
            raise web.HTTPBadRequest(text="no usable speed profile in that session")
        self._start_program(f"replay {name}", segments)
        return web.json_response({"ok": True, "segments": len(segments)})

    def _start_program(self, name: str, segments: list[dict]) -> None:
        if self.program_task and not self.program_task.done():
            raise web.HTTPConflict(text="a program is already running")
        self.program_task = asyncio.get_running_loop().create_task(
            self._program_loop(name, segments))

    async def _program_loop(self, name: str, segments: list[dict]) -> None:
        # ramps-to-a-speed and goal segments have no fixed duration, so the total
        # is a floor, not a promise
        log.info("program '%s': %d segments, %.1f+ min total", name, len(segments),
                 sum(s.get("minutes", 0) for s in segments))
        try:
            if self.latest.get("state") != ST_RUNNING:
                self.user_paused = False
                await self.send_cmd(bytes([0x53, 0x00, 0x01, 0x00, 0x6E, 0x28, 0x50, 0xAA]))
                await asyncio.sleep(0.5)
                await self.send_cmd(bytes([0x53, 0x01, 0, 0, 0, 0, 0x00, 0x00, 0x00, 0x00]))
                for _ in range(30):  # wait out the start countdown
                    if self.latest.get("state") == ST_RUNNING:
                        break
                    await asyncio.sleep(1)
                else:
                    log.warning("program '%s': belt never started", name)
                    return
            self._prog_speed = self.latest.get("speed_mph") or 0.4
            runners = {"ramp": self._run_ramp, "hr": self._run_hr, "goal": self._run_goal}
            for i, seg in enumerate(segments):
                kind = seg.get("type", "hold")
                log.info("program '%s': segment %d/%d — %s", name, i + 1, len(segments), seg)
                if kind in runners:
                    ok = await runners[kind](name, i, len(segments), seg)
                else:
                    self._prog_speed = clamp_mph(seg["mph"])
                    await self.send_cmd(bytes([0x53, 0x02, round(self._prog_speed * 10), 0x00]))
                    ok = await self._run_hold(name, i, len(segments), seg["minutes"] * 60,
                                              f"{self._prog_speed} mph")
                if not ok:
                    log.info("program '%s': session ended — aborting", name)
                    return
            log.info("program '%s' complete — stopping and uploading", name)
            await self.send_cmd(bytes([0x53, 0x03]))
            if not (self.stop_task and not self.stop_task.done()):
                self.stop_task = asyncio.get_running_loop().create_task(self._stop_flow())
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("program '%s' failed: %s", name, exc)
        finally:
            self.program_status = None

    async def _prog_tick(self) -> str:
        """One wall-clock second of program time: 'tick' | 'hold' | 'abort'."""
        await asyncio.sleep(1)
        if not self.in_session:
            return "abort"
        if self.latest.get("state") == ST_RUNNING and not self.user_paused:
            return "tick"
        return "hold"  # paused/stopped: freeze the program clock

    def _prog_report(self, name: str, i: int, total: int, left_s: float,
                     label: str = "") -> None:
        self.program_status = {"name": name, "seg": i + 1, "total": total,
                               "mph": self._prog_speed, "seg_left_s": round(left_s),
                               "label": label or f"{self._prog_speed} mph"}

    async def _run_hold(self, name: str, i: int, total: int, seconds: float,
                        label: str = "") -> bool:
        remaining = seconds
        while remaining > 0:
            self._prog_report(name, i, total, remaining, label)
            r = await self._prog_tick()
            if r == "abort":
                return False
            if r == "tick":
                remaining -= 1
        return True

    async def _run_hr(self, name: str, i: int, total: int, seg: dict) -> bool:
        """Closed loop: nudge speed to hold heart rate at seg['bpm'] ± 3."""
        remaining = seg["minutes"] * 60
        since_adjust = 0.0
        while remaining > 0:
            self._prog_report(name, i, total, remaining, f"hold ♥ {seg['bpm']} bpm")
            r = await self._prog_tick()
            if r == "abort":
                return False
            if r != "tick":
                continue
            remaining -= 1
            since_adjust += 1
            hr = self.latest_hr if time.time() - self.hr_last_seen < 10 else None
            if hr is None or since_adjust < 20:
                continue  # no strap: hold current speed; else wait out response lag
            delta = -0.1 if hr > seg["bpm"] + 3 else (0.1 if hr < seg["bpm"] - 3 else 0)
            if delta:
                cap = min(seg.get("max_mph", 3.0), MAX_MPH)
                new = max(0.4, min(cap, round((self._prog_speed + delta) * 10) / 10))
                if new != self._prog_speed:
                    self._prog_speed = new
                    await self.send_cmd(bytes([0x53, 0x02, round(new * 10), 0x00]))
            since_adjust = 0
        return True

    async def _run_goal(self, name: str, i: int, total: int, seg: dict) -> bool:
        """Fixed speed until the session total reaches a step or distance goal."""
        self._prog_speed = clamp_mph(seg["mph"])
        await self.send_cmd(bytes([0x53, 0x02, round(self._prog_speed * 10), 0x00]))
        while True:
            cur_steps = self.samples[-1].steps if self.samples else 0
            cur_m = self.dist_m
            if "steps" in seg:
                left = seg["steps"] - cur_steps
                if left <= 0:
                    return True
                est = left / 1.8  # ~108 spm walking
                label = f"{left} steps to go"
            else:
                left_m = seg["miles"] * 1609.34 - cur_m
                if left_m <= 0:
                    return True
                speed = max(self._prog_speed * MPH_TO_MPS, 0.1)
                est = left_m / speed
                label = f"{left_m / 1609.34:.2f} mi to go"
            self._prog_report(name, i, total, est, label)
            r = await self._prog_tick()
            if r == "abort":
                return False

    async def _run_ramp(self, name: str, i: int, total: int, seg: dict) -> bool:
        rate, per = seg["rate_mph"], seg["per_s"]
        target = seg.get("until_mph")
        if target is not None:
            # the ceiling binds the destination too, else a downward ramp toward a
            # target above MAX_MPH would command it verbatim
            target = clamp_mph(target)
            rate = abs(rate) if target >= self._prog_speed else -abs(rate)
        end_in = seg.get("minutes", 0) * 60
        since_step = 0
        while True:
            if target is not None:
                if abs(target - self._prog_speed) < 0.05:
                    return True
                left = abs(target - self._prog_speed) / abs(rate) * per
            else:
                if end_in <= 0:
                    return True
                left = end_in
            self._prog_report(name, i, total, left,
                              f"ramp → {target} mph" if target is not None
                              else f"ramp {rate:+.1f}/{per}s")
            r = await self._prog_tick()
            if r == "abort":
                return False
            if r != "tick":
                continue
            if target is None:
                end_in -= 1
            since_step += 1
            if since_step >= per:
                since_step = 0
                new = clamp_mph(round((self._prog_speed + rate) * 10) / 10)
                if target is not None:
                    new = min(new, target) if rate > 0 else max(new, target)
                if new != self._prog_speed:
                    self._prog_speed = new
                    await self.send_cmd(bytes([0x53, 0x02, round(new * 10), 0x00]))
                elif target is None:
                    continue  # clamped at a speed limit; ride out the duration
                else:
                    return True

    # ---------- heart rate strap ----------

    async def hrm_loop(self) -> None:
        """Maintain a strap connection while a session is active."""
        while self.in_session:
            try:
                device = await self.find_hrm()
                if device is None:
                    await asyncio.sleep(20)
                    continue
                async with BleakClient(device, timeout=15.0) as client:
                    log.info("HR strap connected: %s", device)
                    await client.start_notify(HR_MEASUREMENT, self.on_hr)
                    while client.is_connected and self.in_session:
                        await asyncio.sleep(1.0)
            except Exception as exc:  # noqa: BLE001
                log.warning("HR strap connection failed: %s (retrying)", exc)
                await asyncio.sleep(15)
        log.info("HR strap loop ended")

    async def find_hrm(self) -> str | None:
        if HRM_ADDRESS.lower() != "auto":
            return HRM_ADDRESS
        hits = [d for d, adv in (await BleakScanner.discover(timeout=8, return_adv=True)).values()
                if HR_SERVICE in [u.lower() for u in adv.service_uuids]]
        # chest straps (HRM-*) beat watch broadcasts: they carry RR intervals for HRV
        hits.sort(key=lambda d: not (d.name or "").upper().startswith("HRM"))
        if hits:
            log.info("found HR device: %s (%s)", hits[0].name, hits[0].address)
            return hits[0].address
        return None

    def on_hr(self, _char, data: bytearray) -> None:
        now = time.time()
        flags = data[0]
        idx = 1
        if flags & 0x01:
            hr = data[idx] | (data[idx + 1] << 8)
            idx += 2
        else:
            hr = data[idx]
            idx += 1
        if flags & 0x08:  # energy expended present
            idx += 2
        if flags & 0x10:  # RR intervals present
            while idx + 1 < len(data):
                rr = (data[idx] | (data[idx + 1] << 8)) / 1024.0
                idx += 2
                if 0.24 <= rr <= 2.5:  # sanity: 24-250 bpm
                    self.rr_events.append((now, rr))
        if hr:
            self.latest_hr = hr
            self.hr_last_seen = now


def _sigterm(*_args) -> None:
    # Convert SIGTERM into KeyboardInterrupt so BleakClient contexts disconnect
    # cleanly — abrupt exits can wedge peripherals (e.g. Garmin broadcast HR).
    raise KeyboardInterrupt


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)  # fit_tool configures root logging on import; override it
    signal.signal(signal.SIGTERM, _sigterm)
    log.info("milltender starting; treadmill=%s hrm=%s", TREADMILL_ADDRESS, HRM_ADDRESS)
    if MAX_MPH < 0.4:
        log.warning("MAX_MPH=%.1f is below the 0.4 protocol floor; the floor wins", MAX_MPH)
    daemon = Daemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        if daemon.in_session:
            log.info("interrupted mid-session; finalizing")
            asyncio.run(daemon.finalize())


if __name__ == "__main__":
    main()
