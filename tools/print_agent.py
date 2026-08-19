#!/usr/bin/env python3
"""CRM print agent — run this on a Linux machine that's on the SAME
local network as the Xprinter XP-420B (the CRM server itself is a
remote VPS with no route to a printer behind your router). Polls the
CRM for pending barcode-label print jobs and sends each one to the
printer via CUPS (`lp`). Standard library only — no pip install needed.

Setup:
  1. Add the printer in CUPS, pointing at its IP on your network
     (replace 192.168.1.50 with the Xprinter's actual IP — check your
     router's connected-devices list, or the printer's own network
     settings menu/printout):

       sudo lpadmin -p xprinter -E -v socket://192.168.1.50:9100 -m raw

     Port 9100 is the standard raw/JetDirect printing port that
     Xprinter (like most label/receipt printers) listens on.

  2. Confirm it actually prints:

       lp -d xprinter /usr/share/cups/data/testprint

     If nothing comes out, the CUPS setup — not this script — is the
     thing to debug first (`lpstat -p`, `lpq -P xprinter`).

  3. Fill in PRINTER_NAME below if you used a different name than
     "xprinter" in step 1.

  4. Set PRINT_AGENT_TOKEN and run it:

       export PRINT_AGENT_TOKEN=...   # get this value from Павел, not committed to git
       python3 print_agent.py

     Leave it running — it polls every few seconds. To have it survive
     reboots/logouts, set it up as a systemd user service or a cron
     @reboot entry (put the export in the service/crontab, not just
     your interactive shell) — ask if you want the exact steps for
     that.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CRM_URL = "https://crm.2.59.217.221.sslip.io"
AGENT_TOKEN = os.environ.get("PRINT_AGENT_TOKEN")
PRINTER_NAME = "xprinter"  # must match the CUPS printer name from step 1 (`lpstat -p`)
POLL_SECONDS = 3

if not AGENT_TOKEN:
    sys.exit("Set PRINT_AGENT_TOKEN in the environment first — see the setup notes at the top of this file.")


def _url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{CRM_URL}{path}{sep}token={AGENT_TOKEN}"


def _get_json(path: str) -> dict:
    with urllib.request.urlopen(_url(path), timeout=10) as resp:
        return json.loads(resp.read())


def _get_bytes(path: str) -> bytes:
    with urllib.request.urlopen(_url(path), timeout=10) as resp:
        return resp.read()


def _post(path: str) -> None:
    req = urllib.request.Request(_url(path), method="POST", data=b"")
    urllib.request.urlopen(req, timeout=10)


def _ack(job_id: int, ok: bool, error: str = "") -> None:
    params = {"ok": "true" if ok else "false"}
    if error:
        params["error"] = error
    query = urllib.parse.urlencode(params)
    _post(f"/print-agent/jobs/{job_id}/ack?{query}")


def print_job(job: dict) -> None:
    job_id = job["id"]
    try:
        png = _get_bytes(f"/print-agent/jobs/{job_id}/label.png")
    except urllib.error.HTTPError as exc:
        print(f"job {job_id}: couldn't fetch label — {exc}")
        _ack(job_id, False, f"fetch failed: {exc}")
        return

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        path = f.name

    result = subprocess.run(["lp", "-d", PRINTER_NAME, path], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"job {job_id}: printed ({result.stdout.strip()})")
        _ack(job_id, True)
    else:
        print(f"job {job_id}: FAILED — {result.stderr.strip()}")
        _ack(job_id, False, result.stderr.strip()[:200])


def main() -> None:
    print(f"CRM print agent watching {CRM_URL} for printer '{PRINTER_NAME}'. Ctrl+C to stop.")
    while True:
        try:
            data = _get_json("/print-agent/jobs")
            for job in data.get("jobs", []):
                print_job(job)
        except Exception as exc:  # network hiccup, CRM restart, etc — keep polling
            print(f"poll failed: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
