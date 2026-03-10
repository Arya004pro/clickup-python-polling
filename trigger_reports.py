"""
Manual trigger helper for Motia report flow.

Default behavior:
- period: today
- spaces: ALL monitored spaces, including AIX (as "Monitored AIX" scope)

Use --no-aix to exclude the monitored AIX space.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests


def _load_spaces(include_aix: bool) -> list[str]:
    steps_root = os.path.join(os.path.dirname(__file__), "clickup-motia-reports")
    if steps_root not in sys.path:
        sys.path.insert(0, steps_root)
    from steps.config import MONITORED_SPACES  # type: ignore

    names = [s.get("name", "").strip() for s in MONITORED_SPACES if s.get("name")]
    # AIX is included by default (as "Monitored AIX" scope via monitoring_config.json).
    # Pass --no-aix to exclude it.
    if not include_aix:
        return [n for n in names if n.lower() != "aix"]
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger Motia report generation")
    parser.add_argument(
        "--period",
        default="today",
        choices=[
            "today",
            "yesterday",
            "this_week",
            "last_week",
            "this_month",
            "last_month",
        ],
        help="Report period",
    )
    parser.add_argument(
        "--no-aix",
        action="store_true",
        help="Exclude the Monitored AIX space from the triggered spaces",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Optional schedule label override",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("MOTIA_TRIGGER_URL", "http://localhost:3111/trigger-report"),
        help="Motia trigger endpoint",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=int(os.getenv("MOTIA_TRIGGER_RETRIES", "8")),
        help="Retry attempts for transient connection/startup failures",
    )
    parser.add_argument(
        "--retry-delay-s",
        type=float,
        default=float(os.getenv("MOTIA_TRIGGER_RETRY_DELAY_S", "2")),
        help="Delay between retries in seconds",
    )
    args = parser.parse_args()

    spaces = _load_spaces(include_aix=not args.no_aix)
    if not spaces:
        print("No spaces found to trigger.")
        return 1

    label = (
        args.label
        or f"Manual {args.period} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    payload = {"period": args.period, "schedule_label": label, "spaces": spaces}

    print(f"Trigger URL : {args.url}")
    print(f"Period      : {args.period}")
    print(f"Label       : {label}")
    print(f"Spaces({len(spaces)}): {spaces}")

    data = None
    last_err: Exception | None = None
    attempts = max(1, args.retries)
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(args.url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            break
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.HTTPError,
        ) as exc:
            last_err = exc
            if attempt >= attempts:
                break
            print(
                f"Trigger attempt {attempt}/{attempts} failed ({exc.__class__.__name__}). "
                f"Retrying in {args.retry_delay_s}s..."
            )
            time.sleep(max(0.1, args.retry_delay_s))

    if data is None:
        raise RuntimeError(f"Trigger failed after {attempts} attempts: {last_err}")

    print("\nTrigger response:")
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
