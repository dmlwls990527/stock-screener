#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
marcap 데이터셋(GitHub clone)을 git pull로 최신화합니다.

test.py / plot 스크립트에서는 pull 하지 않고, 이 파일만 cron 등으로 주기 실행하면 됩니다.

cron 예시 (매일 00:00, RHEL 계열):
  0 0 * * * /usr/bin/python3 /data/frame/update_marcap_dataset.py >> /data/frame/logs/update_marcap_cron.log 2>&1

환경 변수:
  MARCAP_REPO  marcap 저장소 경로 (기본: 이 스크립트와 같은 디렉터리의 marcap/)
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_repo() -> Path:
    env = os.environ.get("MARCAP_REPO")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "marcap"


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "update_marcap.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def git_pull(repo: Path, ff_only: bool = True) -> int:
    git_dir = repo / ".git"
    if not git_dir.exists():
        logging.error("Git 저장소가 아닙니다 (.git 없음): %s", repo)
        return 1

    cmd = ["git", "-C", str(repo), "pull"]
    if ff_only:
        cmd.append("--ff-only")

    logging.info("실행: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.stdout:
        logging.info(p.stdout.rstrip())
    if p.stderr:
        logging.warning(p.stderr.rstrip())

    if p.returncode != 0:
        logging.error("git pull 실패 (exit %s)", p.returncode)
        return p.returncode

    logging.info("git pull 완료")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="marcap 저장소 git pull")
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="marcap clone 경로 (기본: 환경변수 MARCAP_REPO 또는 ./marcap)",
    )
    parser.add_argument(
        "--no-ff-only",
        action="store_true",
        help="git pull 시 --ff-only 생략 (merge 커밋이 필요한 경우에만)",
    )
    args = parser.parse_args()

    repo = (args.repo or _default_repo()).resolve()
    log_dir = Path(__file__).resolve().parent / "logs"
    setup_logging(log_dir)

    logging.info("시작 (UTC %s)", datetime.now(timezone.utc).isoformat())
    logging.info("저장소: %s", repo)

    return git_pull(repo, ff_only=not args.no_ff_only)


if __name__ == "__main__":
    raise SystemExit(main())
