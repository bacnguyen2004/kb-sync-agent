"""Daily job: re-scrape support articles and sync delta to OpenAI Vector Store."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from scraper import scrape
from uploader import ask_assistant, sync_docs

ROOT = Path(__file__).parent
LOGS_DIR = ROOT / "logs"


def setup_logging() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_path


def run_pipeline(scrape_limit: int) -> dict:
    logging.info("Starting scrape (limit=%s)", scrape_limit)
    scrape_stats = scrape(limit=scrape_limit)
    logging.info(
        "Scrape done: added=%s updated=%s skipped=%s total=%s",
        scrape_stats["added"],
        scrape_stats["updated"],
        scrape_stats["skipped"],
        scrape_stats["count"],
    )

    logging.info("Starting vector store sync")
    upload_stats = sync_docs()
    logging.info(
        "Upload done: added=%s updated=%s skipped=%s embedded_files=%s",
        upload_stats["added"],
        upload_stats["updated"],
        upload_stats["skipped"],
        upload_stats.get("embedded_files", 0),
    )

    return {
        "scrape": scrape_stats,
        "upload": upload_stats,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="kb-sync-agent daily job")
    parser.add_argument("--test", metavar="QUESTION", help="Ask OptiBot a test question")
    parser.add_argument("--limit", type=int, default=int(os.getenv("SCRAPE_LIMIT", "30")))
    args = parser.parse_args()

    if args.test:
        print(ask_assistant(args.test))
        return 0

    log_path = setup_logging()
    logging.info("Log file: %s", log_path)

    try:
        summary = run_pipeline(scrape_limit=args.limit)
        logging.info("Job completed successfully")
        print(json.dumps(summary, indent=2))
        return 0
    except Exception:
        logging.exception("Job failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())