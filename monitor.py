"""Check a list of Starlink hosts and notify via Telegram on state changes.

Reads `targets.yaml`, performs ICMP ping (or TCP connect if `port` is set) per
target, persists per-target state in `state.json`, and posts to Telegram when a
target crosses the failure threshold or recovers.

Environment:
    TELEGRAM_BOT_TOKEN  Bot token from @BotFather.
    TELEGRAM_CHAT_ID    Target chat/user id.

Exit code is always 0 unless the config itself is unreadable, so a single
unreachable host does not fail the workflow run.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "targets.yaml"
STATE_PATH = ROOT / "state.json"


@dataclass
class Target:
    name: str
    host: str
    port: int | None = None  # None => ICMP ping; int => TCP connect


def load_config() -> tuple[list[Target], dict]:
    with CONFIG_PATH.open() as f:
        raw = yaml.safe_load(f) or {}
    targets = [
        Target(name=t["name"], host=t["host"], port=t.get("port"))
        for t in raw.get("targets", [])
    ]
    settings = raw.get("settings", {}) or {}
    settings.setdefault("ping_count", 3)
    settings.setdefault("timeout_seconds", 5)
    settings.setdefault("failure_threshold", 2)
    return targets, settings


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def check_icmp(host: str, count: int, timeout: int) -> bool:
    # `-w` is the overall deadline in seconds (Linux iputils ping).
    result = subprocess.run(
        ["ping", "-n", "-c", str(count), "-w", str(timeout), host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def check_tcp(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check(target: Target, settings: dict) -> bool:
    timeout = int(settings["timeout_seconds"])
    if target.port is None:
        return check_icmp(target.host, int(settings["ping_count"]), timeout)
    return check_tcp(target.host, target.port, timeout)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram credentials missing; skipping notification.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Telegram request failed: {e}", file=sys.stderr)


def main() -> int:
    targets, settings = load_config()
    if not targets:
        print("No targets configured.", file=sys.stderr)
        return 0

    state = load_state()
    threshold = int(settings["failure_threshold"])
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for target in targets:
        prev = state.get(target.name, {"consecutive_failures": 0, "alerted_down": False})
        is_up = check(target, settings)
        endpoint = target.host if target.port is None else f"{target.host}:{target.port}"

        if is_up:
            if prev.get("alerted_down"):
                send_telegram(
                    f"✅ RECOVERED: {target.name} ({endpoint}) is back up at {now_iso}."
                )
            state[target.name] = {
                "consecutive_failures": 0,
                "alerted_down": False,
                "last_up": now_iso,
            }
            print(f"[up]   {target.name} {endpoint}")
        else:
            failures = int(prev.get("consecutive_failures", 0)) + 1
            alerted = bool(prev.get("alerted_down", False))
            if failures >= threshold and not alerted:
                send_telegram(
                    f"\U0001F6A8 DOWN: {target.name} ({endpoint}) failed "
                    f"{failures} consecutive checks as of {now_iso}."
                )
                alerted = True
            state[target.name] = {
                "consecutive_failures": failures,
                "alerted_down": alerted,
                "last_up": prev.get("last_up"),
                "last_fail": now_iso,
            }
            print(f"[DOWN] {target.name} {endpoint} (failures={failures})")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
