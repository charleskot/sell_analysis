"""CLI entrypoint to run the daily pipeline once and exit.

Usage:
  python run_daily.py            # full pipeline
  python run_daily.py --dry-run  # scrape + analyze, do not send email
"""
from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(description="Run auction analyzer once")
    parser.add_argument("--dry-run", action="store_true", help="Skip email send")
    args = parser.parse_args()

    if args.dry_run:
        # Monkey-patch send_email to a no-op
        from app import notifier
        notifier.send_email = lambda *a, **k: logging.info("(dry-run) email no enviado")

    from app.pipeline import run_daily
    result = run_daily()
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
