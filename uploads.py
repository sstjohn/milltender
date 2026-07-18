"""Upload a FIT file to Strava (official API) and Garmin Connect (unofficial).

CLI:
  python uploads.py strava-login       # one-time OAuth: prints URL, saves refresh token
  python uploads.py garmin-login       # one-time interactive Garmin login (MFA prompt)
  python uploads.py send <file.fit>    # upload to both, as the daemon would
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from dotenv import load_dotenv

GARMIN_TOKENSTORE = os.path.expanduser("~/.garminconnect")
STRAVA_TOKEN_FILE = Path(__file__).resolve().parent / ".strava_refresh_token"

load_dotenv(Path(__file__).resolve().parent / ".env")


def upload_strava(fit_path: Path, name: str = "Treadmill walk",
                  description: str = "Recorded by milltender — "
                                     "https://github.com/sstjohn/milltender") -> int:
    """Returns the Strava activity id. Raises on failure or duplicate-rejection."""
    refresh = ((STRAVA_TOKEN_FILE.read_text().strip() if STRAVA_TOKEN_FILE.exists() else "")
               or os.environ.get("STRAVA_REFRESH_TOKEN", ""))
    if not refresh:
        raise RuntimeError("no Strava refresh token — run: python uploads.py strava-login")
    token = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }, timeout=30)
    token.raise_for_status()
    body = token.json()
    access = body["access_token"]
    if body.get("refresh_token") and body["refresh_token"] != refresh:
        # Strava rotated it; write atomically so a crash can't strand an empty file
        tmp = STRAVA_TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(body["refresh_token"])
        os.replace(tmp, STRAVA_TOKEN_FILE)

    with fit_path.open("rb") as fh:
        r = requests.post(
            "https://www.strava.com/api/v3/uploads",
            headers={"Authorization": f"Bearer {access}"},
            files={"file": fh},
            data={"data_type": "fit", "name": name, "description": description,
                  "trainer": "true", "external_id": fit_path.stem},
            timeout=60,
        )
    r.raise_for_status()
    upload_id = r.json()["id"]
    for _ in range(40):
        time.sleep(1.5)
        status = requests.get(f"https://www.strava.com/api/v3/uploads/{upload_id}",
                              headers={"Authorization": f"Bearer {access}"}, timeout=30).json()
        if status.get("error"):
            raise RuntimeError(f"strava rejected upload: {status['error']}")
        if status.get("activity_id"):
            return status["activity_id"]
    raise TimeoutError("strava upload still processing after 60s")


def strava_login() -> None:
    """One-time OAuth dance: authorize in a browser, paste the redirect back."""
    client_id = os.environ.get("STRAVA_CLIENT_ID") or input("Strava client id: ").strip()
    secret = os.environ.get("STRAVA_CLIENT_SECRET") or input("Strava client secret: ").strip()
    url = ("https://www.strava.com/oauth/authorize"
           f"?client_id={client_id}&response_type=code&redirect_uri=http://localhost"
           "&approval_prompt=force&scope=read,activity:write")
    print("\nOpen this in a browser logged into Strava and click Authorize:\n\n"
          f"  {url}\n\n"
          "The browser will land on an unreachable localhost page — that's expected.\n"
          "Copy the full URL from the address bar and paste it here.\n")
    raw = input("redirect URL (or just the code): ").strip()
    code = parse_qs(urlparse(raw).query).get("code", [raw])[0] if "://" in raw else raw
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": client_id, "client_secret": secret,
        "code": code, "grant_type": "authorization_code"}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    tmp = STRAVA_TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(body["refresh_token"])
    os.replace(tmp, STRAVA_TOKEN_FILE)
    athlete = body.get("athlete") or {}
    print(f"\nAuthorized as {athlete.get('username') or athlete.get('firstname', 'you')}; "
          f"refresh token saved to {STRAVA_TOKEN_FILE.name} (gitignored).\n"
          "Keep STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env.")


def _garmin_client():
    from garminconnect import Garmin
    client = Garmin()
    client.login(GARMIN_TOKENSTORE)  # loads cached tokens; raises if absent/expired
    return client


def _mfa_prompt() -> str:
    if sys.stdin.isatty():
        return input("Garmin MFA code: ")
    # non-interactive: poll for a code dropped into .mfa_code next to this file
    path = Path(__file__).resolve().parent / ".mfa_code"
    print(f"MFA required — waiting up to 10 min for code in {path}", flush=True)
    for _ in range(600):
        if path.exists():
            code = path.read_text().strip()
            path.unlink()
            if code:
                return code
        time.sleep(1)
    raise TimeoutError("no MFA code provided within 10 minutes")


def garmin_login() -> None:
    """First-time login; caches tokens for the daemon."""
    from garminconnect import Garmin
    client = Garmin(email=os.environ["GARMIN_EMAIL"],
                    password=os.environ["GARMIN_PASSWORD"],
                    prompt_mfa=_mfa_prompt)
    # passing the tokenstore makes login() persist tokens there itself (master-branch API)
    client.login(GARMIN_TOKENSTORE)
    print(f"Garmin tokens cached in {GARMIN_TOKENSTORE}")


def upload_garmin(fit_path: Path):
    client = _garmin_client()
    return client.upload_activity(str(fit_path))


def upload_all(fit_path: Path, name: str = "Treadmill walk") -> dict:
    """Best-effort upload to both platforms; returns per-platform outcome."""
    results = {}
    for platform, fn in (("strava", lambda: upload_strava(fit_path, name)),
                         ("garmin", lambda: upload_garmin(fit_path))):
        try:
            results[platform] = {"ok": True, "result": fn()}
        except Exception as exc:  # noqa: BLE001 — daemon must survive either side failing
            results[platform] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return results


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "strava-login":
        strava_login()
    elif len(sys.argv) >= 2 and sys.argv[1] == "garmin-login":
        garmin_login()
    elif len(sys.argv) >= 3 and sys.argv[1] == "send":
        print(upload_all(Path(sys.argv[2])))
    else:
        print(__doc__)
