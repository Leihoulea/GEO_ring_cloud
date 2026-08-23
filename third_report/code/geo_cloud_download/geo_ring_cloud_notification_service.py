"""Independent transition monitor for GEO download/upload email notifications."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional


COMPONENT_ROLE = "notification_monitor_service"
RELATED_STAGE_IDS = ["stage_00"]
APP_ROOT = Path(__file__).resolve().parent
CORE_CODE_ROOT = APP_ROOT.parent / "geo_ring_cloud_stage1"
if str(CORE_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_CODE_ROOT))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from geo_ring_cloud.notifications import PersistentEmailNotifier  # noqa: E402
from geo_ring_cloud.lineage import code_commit, generating_script_state  # noqa: E402
from geo_ring_cloud.paths import PROJECT_ROOT  # noqa: E402
from geo_ring_cloud_transfer_dashboard import DashboardState  # noqa: E402


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="独立监控 GEO 批次状态，并通过持久队列发送邮件通知。"
    )
    parser.add_argument("--batch-root", required=True)
    parser.add_argument("--state-path")
    parser.add_argument("--interval-seconds", type=int, default=30 * 60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    batch_root = Path(args.batch_root).resolve()
    if not batch_root.is_dir():
        print("ERROR: 批次目录不存在：{}".format(batch_root), file=sys.stderr)
        return 2
    state_path = (
        Path(args.state_path).resolve()
        if args.state_path
        else batch_root.parent
        / "_geo_ring_cloud_control"
        / "notifications"
        / "notification_state.json"
    )
    notifier = PersistentEmailNotifier(state_path)
    if args.test_email:
        result = notifier.send_test()
        print(result)
        return 0

    dashboard = DashboardState(batch_root)
    interval = max(5, int(args.interval_seconds))
    script_state = generating_script_state(Path(__file__).resolve(), PROJECT_ROOT)
    notifier.update_monitor(
        status="RUNNING",
        pid=os.getpid(),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        interval_seconds=interval,
        email_enabled=notifier.enabled,
        desktop_enabled=notifier.desktop_enabled,
        credentials_persisted=notifier.credentials_persisted,
        automatic_delete=False,
        generating_script=str(Path(__file__).resolve()),
        code_commit=code_commit(PROJECT_ROOT),
        code_commit_scope="repository_head_at_run_start",
        generating_script_state=script_state,
        lineage_warnings=(
            []
            if script_state["commit_represents_script"]
            else ["code_commit does not fully represent the generating script content"]
        ),
    )
    exit_code = 0
    try:
        while True:
            try:
                tasks = dashboard.task_summaries()
                created = notifier.observe(tasks)
                delivered = notifier.deliver_due()
                notifier.update_monitor(
                    status="RUNNING",
                    pid=os.getpid(),
                    email_enabled=notifier.enabled,
                    credentials_persisted=notifier.credentials_persisted,
                    batch_count=len(tasks),
                    events_created_last_cycle=created,
                    events_delivered_last_cycle=delivered,
                    last_cycle_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    last_error="",
                )
            except Exception as exc:
                notifier.update_monitor(
                    status="RUNNING_WITH_ERROR",
                    pid=os.getpid(),
                    last_error="{}: {}".format(type(exc).__name__, exc),
                )
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        notifier.update_monitor(
            status="STOPPED",
            pid=os.getpid(),
            stopped_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
