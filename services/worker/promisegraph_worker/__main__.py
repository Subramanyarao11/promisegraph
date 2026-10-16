"""`uv run python -m promisegraph_worker [--once] [--max-jobs N] [--exit-when-idle]`"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import REPO_ROOT, load_settings
from .worker import Worker, install_signal_handlers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="promisegraph_worker", description="PromiseGraph private worker (no inbound port)")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    parser.add_argument("--max-jobs", type=int, default=None)
    parser.add_argument("--exit-when-idle", action="store_true", help="exit when no job is due (tests/demos)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings(args.env_file)
    logging.getLogger("promisegraph.worker").info(
        "worker %s -> %s workspaces=%s planner=scripted%s linear=%s",
        settings.worker_id, settings.api_base_url, ",".join(settings.workspaces),
        "+model" if settings.model_configured else " (model disabled)", "enabled" if settings.linear_configured else "disabled",
    )
    worker = Worker(settings)
    install_signal_handlers(worker)
    if args.once:
        worker.run_once()
        return 0
    worker.run_forever(max_jobs=args.max_jobs, exit_when_idle=args.exit_when_idle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
