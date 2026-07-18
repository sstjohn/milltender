# TX6 Glow-Up → Strava + Garmin: Plan Options

*Research date: 2026-07-16. Sources: blak3r/treadspan (protocol captures), marcuspuchalla/lifespan-sc110, pcorliss/treadmill, daeken/lostmsu gists, TX6 owner's manual, Strava/Garmin API docs and forums.*

## TL;DR

- **TreadSpan doesn't solve your problem.** It's ESP32 firmware + an *iOS-only* app that
  writes *steps-only* to Apple HealthKit. No Strava, no Garmin, no Android. Its value to
  us is its MIT-licensed protocol reverse-engineering and ESP32 driver code.
- **Your treadmill has two usable interfaces**, both partially documented: a DB9 console
  port speaking 4800-baud 5V-TTL serial (Modbus-RTU-like; control + real step counts),
  and built-in BLE in the base (confirmed working with generic FTMS; possibly also the
  LifeSpan-native protocol with *control*, per a sister Glow-Up model).
- **The hard part is Garmin, not Strava.** Garmin has no hobbyist API; Strava's API now
  requires a paid Strava subscription (June 2026 policy). The two viable Garmin routes:
  an unofficial Python library (works today, perpetually fragile), or emulating a BLE
  footpod that a **Garmin watch** records from (fully official, also the only route that
  credits daily steps).
- **Recommendation: start with a ~1-evening Phase 0 probe** (common to every plan) that
  resolves the three unknowns on *your specific unit*, then commit. My tentative pick is
  **Plan A** (wired console-emulator + Linux uploader), upgraded to **Plan C** if you
  own a Garmin watch.

## What the research established

### Treadmill side

| Interface | Status on TX6 Glow-Up |
|---|---|
| **DB9 serial port** (for the optional desk console) | Protocol captured on TR1200B: **4800 8-N-1, 5V TTL**. Pinout known: pin 1 = +12V, 2 = GND, 3 = base TX, 4 = base RX. Modbus-RTU-ish frames: set speed `01 06 00 0A <u16>`, start `01 06 00 01 00 01`, pause `01 06 00 02 00 01`, **read true steps** `01 03 00 0F` — steps are counted in the base. TX6 uses the same optional console (error code E91 = "communication line" confirms the serial link), so same protocol is *likely but unverified on TX6*. |
| **Base BLE** | Manual confirms the base has built-in Bluetooth (that's what the bad app uses). A TX6-Glow-Up owner got TreadSpan's generic **FTMS** mode working → base speaks standard FTMS (speed/distance, **no real steps** — distance→steps guessing came out ~2× actual). Separately, `pcorliss/treadmill` drives a **TR1000-Glow-Up** over BLE with the LifeSpan-native protocol *including control*: start `E1…`, stop `E0…`, speed `D0…`, reads `A1 85/89/82` (needs an init sequence + keep-alive). If the TX6 base shares that firmware, BLE gives us control **and** native data with zero wiring. Unverified on TX6. |
| **Handheld remote** | Radio protocol undocumented anywhere (BLE vs proprietary 2.4GHz unknown). Not a useful attack surface; ignore. |
| Gotchas | Manual: when a desk console is connected, **the remote is disabled** (one master at a time) — a wired console-emulator probably takes over exclusively, which is fine since we'd implement control, but worth confirming. The official console also caps speed at 4 mph; unknown whether that cap lives in the console (we're free) or the base. Remote caps at 2 mph until unlocked; base auto-stops at 99:59. |

### Upload side (verified July 2026)

| Route | Verdict |
|---|---|
| **Strava API direct** | Technically easy (OAuth + multipart FIT/TCX upload, instant self-serve app registration) but as of June 2026 **requires a paid Strava subscription** to hold API access. |
| **Garmin official API** | Enterprise-only. Not available to hobbyists. |
| **`python-garminconnect` (unofficial)** | Works as of v0.3.6 (June 2026) using TLS-fingerprint impersonation (`curl_cffi`); handles MFA + token caching; supports FIT activity upload. Its sibling `garth` was killed by Garmin's Cloudflare changes in March 2026 — assume ongoing cat-and-mouse. Python/Linux only (Pi/BeagleBone/Mac, not ESP32). |
| **BLE footpod → Garmin watch** | Garmin watches do **not** support FTMS, but all modern ones pair BLE/ANT+ **footpods (Running Speed & Cadence)**. An ESP32 broadcasting RSC = the watch records the walk natively with treadmill-accurate pace/distance, **daily steps credit** (wrist counts them), and syncs onward officially. A $50 commercial product (RunBridge) proves this exact architecture. Requires owning + wearing a Garmin watch and starting the activity on it. |
| **Fan-out** | Garmin Connect → Strava auto-sync is official and reliable. So: get data *into Garmin*, get Strava for free. (Reverse direction doesn't exist.) Uploaded files show steps *on the activity* but never add to Garmin *daily* step totals — only the watch route does that. |
| **File format** | FIT preferred (has real step/cadence fields; official Garmin C SDK embeds fine on ESP32). Python-side FIT encoding is the weak spot (`fit-tool` is a stale fork) — viable, or generate FIT on the ESP32 and merely upload from Linux. |

## The plans

Two independent choices — treadmill interface and upload path — bundled into three coherent plans:

### Plan A — Wired console-emulator + Linux uploader  *(matches all your stated preferences)*
ESP32 T-Display plugs into the DB9 port as the bus master: polls real steps/speed, sends
start/stop/speed (control ✅), shows session on its screen, optionally powered from the
port's 12V pin via a buck converter. On session end it posts the session over WiFi to a
small daemon (Pi/BeagleBone/your Mac — anything always-on) that encodes FIT and uploads
to Garmin Connect via `python-garminconnect`; Garmin forwards to Strava. No phone, no
watch, no subscription.
- **Build:** custom PCB-free wiring: DB9 breakout + bidirectional level shifter (5V TTL ↔ 3.3V) + optional 12V→5V buck. ~$25–30 in parts incl. a cheap logic analyzer.
- **Risks:** TX6 serial protocol is inferred from TR1200B (Phase 0 verifies); `python-garminconnect` may break someday and need patching (it's a 50-line daemon — annoying, not fatal); miswiring the 12V pin into a data pin could damage the controller (careful bench work).

### Plan B — Pure BLE bridge, no wiring  *(least effort; requires a Garmin watch)*
T-Display connects to the base's BLE (FTMS at minimum; native protocol with control if
Phase 0 confirms the pcorliss findings) and re-broadcasts as a **BLE RSC footpod**. Your
Garmin watch pairs it, records the walk, syncs to Garmin + Strava officially. Zero
fragile APIs, zero wiring, daily-step credit. If the native BLE protocol pans out, we
even get control + true step-rate for cadence.
- **Build:** software only; $0 extra hardware.
- **Risks:** needs a Garmin watch on your wrist per walk; ESP32 must run BLE central + peripheral concurrently (known-workable with NimBLE, but it's the fiddliest firmware bit); if the base only speaks FTMS, cadence/steps are derived from speed (fixable with a calibration constant, but approximate).

### Plan C — Wired reader/controller + footpod broadcast  *(best data quality; watch required)*
Plan A's wiring, Plan B's upload: ESP32 reads *true* steps/speed off the DB9 wire, has
control, and broadcasts RSC with **accurate cadence** to the watch. No fragile upload
code anywhere; the Linux daemon disappears entirely. Optionally add Plan A's uploader
later as a watchless fallback mode.
- **Build/risks:** same wiring as A; same watch dependency as B.

| | A: wired + uploader | B: BLE + watch | C: wired + watch |
|---|---|---|---|
| Wired (your pref) | ✅ | ❌ | ✅ |
| Treadmill control | ✅ | ⚠️ if native BLE confirmed | ✅ |
| No phone/watch needed | ✅ | ❌ watch | ❌ watch |
| Real steps in data | ✅ | ⚠️ derived (unless native BLE) | ✅ + daily-step credit |
| Fragile-API exposure | ⚠️ garminconnect | none | none |
| Extra hardware cost | ~$25–30 | $0 | ~$25–30 |
| Firmware complexity | medium | medium-high | medium |

## Phase 0 — verify before committing (~1 evening, needed for every plan)

1. **BLE census (do first, zero risk, laptop-only):** `bleak` scan + GATT enumeration of
   the base; try TreadSpan's `A1` reads on `FFF0/FFF1/FFF2`, then pcorliss's init +
   `E1/E0/D0` control sequence (belt speed low, nobody on it). Outcome: does the TX6
   speak native-LifeSpan BLE, FTMS only, or both — and is BLE control real?
2. **Port inspection:** photograph the DB9 (gender/labeling), multimeter the pins
   against the TR1200B pinout (find 12V/GND/idle-high UART lines) **before** connecting
   anything.
3. **Serial probe:** logic analyzer (or just the ESP32) on pins 3/4 at 4800 8N1; replay
   the steps-read frame `01 03 00 0F 00 00 B9 C9` through the level shifter; confirm a
   sane response and whether the remote keeps working alongside.
4. **Upload smoke test (no hardware):** hand-craft a 10-minute walking FIT in Python,
   upload via `python-garminconnect`, confirm it lands in Garmin Connect *and*
   auto-forwards to Strava with steps/pace displayed.

**Shopping list (Plan A/C, ≤$50):** DB9 breakout/solder-cup pair (~$8) · BSS138 4-ch
bidirectional level shifter (~$7) · MP1584 12V→5V buck (~$8) · 8-ch USB logic analyzer
clone (~$13) · dupont wires (~$6). Order after Phase 0 step 2 confirms the connector.

## Software we'd write

- **ESP32 firmware** (PlatformIO, C++; cannibalize TreadSpan's MIT driver classes):
  treadmill driver (serial master and/or BLE central), session state machine,
  T-Display UI, and per-plan output (HTTP POST to daemon / RSC peripheral / on-device
  FIT encode with Garmin's C SDK).
- **Uploader daemon** (Plan A; Python, ~150 lines): receive session JSON → FIT encode →
  `python-garminconnect` upload → optional direct-Strava fallback. Runs on any Pi /
  BeagleBone / the Mac.
- Keep your Meshtastic nodes and the book reader out of it — the T-Display (on order)
  is the right board; a bare $5 ESP32 devkit is a fine stand-in until it arrives.

## REVISION after live probing (2026-07-16, evening)

Phase 0 BLE census run against the actual treadmill changed the picture:

- The TX6 base's BLE module is a **FITSHOW FS-BT-D2** (advertises as `FS-3D6CD7`,
  serial FS231221001, fw 1.3.3) — *not* LifeSpan's own protocol. The LifeSpan `A1`
  opcodes get no response. FitShow is a widespread white-label fitness-BLE ecosystem
  with a fully reverse-engineered protocol (QDomyos-Zwift `fitshowtreadmill` driver).
- FitShow service `FFF0` (write `FFF2` w/o response, notify `FFF1`), frames
  `02 <payload> <xor> 03`. **Live-verified on our unit**: SYS_INFO and SYS_STATUS
  round-trip. Protocol offers **real step counts** (SYS_STATUS running data / SYS_DATA)
  and **control**: SYS_CONTROL 0x53 sub 0=user 1=start 2=set-speed 3=stop 6=pause.
- Standard **FTMS** is also present and live (Treadmill Data notifications verified;
  features: total distance, step count flag, energy, HR, elapsed time; control point
  with speed target; supported speed range raw 100–600 step 10).
- Consequence: **no custom hardware is needed at all.** Plan D (below) supersedes
  Plan A's ESP32+DB9 build; the wired path remains documented as a fallback if the
  BLE session proves flaky or FitShow data turns out unit-confused.

**Plan D (adopted): pure-Python BLE daemon.** One process (develop on the Mac,
deploy to a hidden Pi Zero 2 W/Pi 3+ — anything with BLE) that: connects to the base
over BLE (FitShow primary, FTMS cross-check), detects session start/stop, polls live
data incl. real steps, builds the FIT, uploads natively to Strava (official API) and
into Garmin Connect (`python-garminconnect`). Optional control (start/stop/speed) via
FitShow SYS_CONTROL — could grow a tiny web UI later. The T-Display/DB9 build is
shelved unless BLE disappoints.

**Control test PASSED (2026-07-16 evening):** start → countdown (state START) →
RUNNING, set-speed `53 02 0a 00` took effect (readback 10 = "1.0"), stop → NORMAL.
Belt visually confirmed moving. Verified status layout (frame `02 51 <state>
<speed*10> <incline> <elapsed u16le> <dist u16le> <kcal u16le> <steps u16le> <hr?>
<xor> 03`). **Units resolved:** commanded raw 10 → LED displayed **1.0 mph**, so FitShow speed
raw = tenths of a mph (range 4–60 = 0.4–6.0 mph, matching the TX6 spec; the FTMS
chars mislabel these mph values as km/h — known FitShow quirk, ignore FTMS units).
**Calibration walk done (3 min @ 1.0 mph, phase0/walk-calibration.log):**
elapsed = seconds; **distance raw = 0.001 mi** (initially misread as 0.01 mi from the
probe's scaled output; raw bytes + a 28-min session both confirm 0.001 mi);
**steps are real** (~63/min at 1.0 mph, ticking ~1/s — genuinely counted, not derived);
kcal raw likely 0.1 kcal. 180 consecutive 1 Hz polls, zero drops.
Remaining open items: duplicate-on-Strava check (Phase 0 step 4, needs .env
credentials); keep-alive over a 30–60 min session (will observe in daemon testing).

## Decision (2026-07-16) — superseded by the revision above

Saul chose **Plan A, modified: native Strava upload**. Has a Garmin watch but prefers
not to depend on it; has a paid Strava subscription.

Correction folded in: **Strava→Garmin sync does not exist** (Garmin only pushes out),
so the uploader daemon pushes the same FIT to both — Strava via the official API,
Garmin via `python-garminconnect`. If the user's existing Garmin→Strava connection
duplicates the activity on Strava, either rely on Strava's duplicate rejection
(external_id/time overlap — Phase 0 step 4 tests this) or disable that connection.
Watch-footpod broadcast (Plan C's upload) stays on the table as a later bonus mode.

Phase 0 probe scripts live in `phase0/` — see README.md for the checklist.
