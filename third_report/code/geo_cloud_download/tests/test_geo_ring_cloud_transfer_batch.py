import hashlib
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

from geo_ring_cloud_transfer_batch import prepare_manifest, verify_manifest  # noqa: E402
from geo_ring_cloud_transfer_dashboard import (  # noqa: E402
    DashboardState,
    HTML_PATH,
    ORDER_SOURCE_CONFIG,
    auto_upload_status,
    dashboard_trends,
    download_launcher_status,
    parse_disk_gate,
    process_is_running,
    upload_throughput,
    write_json_atomic as dashboard_write_json_atomic,
)
from geo_ring_cloud_auto_uploader import (  # noqa: E402
    build_auto_upload_manifest,
    discover_completed_files,
    progressive_upload_worker_counts,
    sftp_quote,
    subprocess_creation_flags,
    subprocess_startupinfo,
    validate_server_root,
    write_json_atomic as uploader_write_json_atomic,
)
from geo_ring_cloud.batch_queue import (  # noqa: E402
    estimate_required_space,
    make_queue_item,
    normalize_request,
    read_queue_state,
    refine_estimate_from_inventory,
)
import geo_cloud_downloader  # noqa: E402
import geo_ring_cloud_transfer_dashboard as transfer_dashboard  # noqa: E402
from geo_ring_cloud.notifications import (  # noqa: E402
    PersistentEmailNotifier,
    read_state as read_notification_state,
    save_secure_email_config,
)
import geo_ring_cloud.notifications as notifications  # noqa: E402


class TransferBatchTests(unittest.TestCase):
    def test_downloader_command_records_component_run_lineage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = SimpleNamespace(root=temp_dir, command="validate")
            with patch.object(geo_cloud_downloader, "parse_args", return_value=args), patch.object(
                geo_cloud_downloader, "run_validate"
            ), patch.object(geo_cloud_downloader, "write_lineage_manifest") as writer:
                return_code = geo_cloud_downloader.main([])

        self.assertEqual(return_code, 0)
        self.assertEqual(writer.call_count, 1)
        kwargs = writer.call_args.kwargs
        self.assertEqual(kwargs["canonical_stage_id"], "")
        self.assertEqual(kwargs["component_role"], "data_download_orchestrator")
        self.assertEqual(kwargs["related_stage_ids"], ("stage_00", "stage_00f"))
        self.assertEqual(kwargs["extra"]["final_status"], "PASS")

    def test_dpapi_email_config_never_persists_plaintext_password(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"GEO_RING_NOTIFY_CONFIG_PATH": str(Path(temp_dir) / "email.json")},
            clear=True,
        ), patch.object(notifications, "_dpapi_protect", return_value="ciphertext"):
            path = save_secure_email_config(
                smtp_host="mail.example.test",
                smtp_port=465,
                smtp_user="sender@example.test",
                smtp_password="plain-secret",
                sender="sender@example.test",
                recipient="phone@example.test",
                use_ssl=True,
                use_starttls=False,
            )
            serialized = path.read_text(encoding="utf-8")

        self.assertIn("ciphertext", serialized)
        self.assertNotIn("plain-secret", serialized)
        self.assertIn("WindowsCurrentUser", serialized)

    def test_notifier_loads_user_scoped_dpapi_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "email.json"
            config_path.write_text(
                json.dumps(
                    {
                        "smtp_host": "mail.example.test",
                        "smtp_port": 465,
                        "smtp_user": "sender@example.test",
                        "email_from": "sender@example.test",
                        "email_to": "phone@example.test",
                        "smtp_ssl": True,
                        "smtp_starttls": False,
                        "password_dpapi": "ciphertext",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"GEO_RING_NOTIFY_CONFIG_PATH": str(config_path)},
                clear=True,
            ), patch.object(notifications, "_dpapi_unprotect", return_value="secret"):
                notifier = PersistentEmailNotifier(Path(temp_dir) / "state.json")
                status = notifier.public_status()

        self.assertTrue(status["enabled"])
        self.assertTrue(status["credentials_persisted"])
        self.assertEqual(status["credential_source"], "windows_dpapi")
        self.assertEqual(status["recipient"], "ph***@example.test")

    def test_email_notifier_persists_transition_and_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "GEO_RING_NOTIFY_SMTP_HOST": "smtp.example.test",
                "GEO_RING_NOTIFY_EMAIL_FROM": "sender@example.test",
                "GEO_RING_NOTIFY_EMAIL_TO": "phone@example.test",
            },
            clear=False,
        ):
            state_path = Path(temp_dir) / "notification_state.json"
            notifier = PersistentEmailNotifier(state_path)
            notifier.desktop_enabled = False
            running = {
                "batch_name": "batch-a",
                "download_status": "RUNNING",
                "upload_status": "PENDING",
                "server_status": "PENDING",
                "updated_at": "2026-08-12T00:00:00Z",
            }
            complete = {
                **running,
                "download_status": "COMPLETE",
                "updated_at": "2026-08-12T01:00:00Z",
            }
            self.assertEqual(notifier.observe([running]), 0)
            self.assertEqual(notifier.observe([complete]), 1)
            with patch.object(notifier, "_send_message", side_effect=OSError("offline")):
                self.assertEqual(notifier.deliver_due(), 0)
            persisted = read_notification_state(state_path)

        event = persisted["outbox"][0]
        self.assertEqual(event["stage"], "download")
        self.assertEqual(event["status"], "COMPLETE")
        self.assertEqual(event["delivery_status"], "RETRY_WAIT")
        self.assertEqual(event["attempts"], 1)
        self.assertFalse(persisted["credentials_persisted"])
        serialized = json.dumps(persisted)
        self.assertNotIn("smtp.example.test", serialized)
        self.assertNotIn("phone@example.test", serialized)

    def test_email_test_requires_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "GEO_RING_NOTIFY_SMTP_HOST": "",
                "GEO_RING_NOTIFY_EMAIL_FROM": "",
                "GEO_RING_NOTIFY_EMAIL_TO": "",
            },
            clear=False,
        ):
            notifier = PersistentEmailNotifier(Path(temp_dir) / "state.json")
            with self.assertRaisesRegex(RuntimeError, "SMTP"):
                notifier.send_test()

    def test_upload_parallelism_ramps_two_three_four(self):
        self.assertEqual(progressive_upload_worker_counts(13, 4), [2, 3, 4, 4])
        self.assertEqual(progressive_upload_worker_counts(5, 3), [2, 3])
        self.assertEqual(progressive_upload_worker_counts(3, 1), [1, 1, 1])

    def test_download_adaptive_parallelism_probes_and_backs_off(self):
        probe, reason = geo_cloud_downloader.choose_adaptive_worker_count(
            4, 2, 12, 10_000_000, 9_000_000, 8, 0
        )
        self.assertEqual((probe, reason), (5, "throughput_probe_up"))
        backoff, reason = geo_cloud_downloader.choose_adaptive_worker_count(
            5, 2, 12, 8_000_000, 10_000_000, 6, 2
        )
        self.assertEqual((backoff, reason), (4, "errors_backoff"))

    def test_active_parts_keeps_sampling_separate_per_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            older = root / "older"
            current_part = current / "Himawari-9" / "data.nc.part"
            older_part = older / "transfer" / "auto_upload_status.json.part"
            current_part.parent.mkdir(parents=True)
            older_part.parent.mkdir(parents=True)
            current_part.write_bytes(b"a" * 100)
            older_part.write_bytes(b"control")

            with patch.object(transfer_dashboard, "_PART_SNAPSHOT", {}), patch(
                "geo_ring_cloud_transfer_dashboard.time.time", side_effect=[100.0, 105.0, 110.0]
            ):
                first = transfer_dashboard.active_parts(current)
                old_batch = transfer_dashboard.active_parts(older)
                with current_part.open("ab") as handle:
                    handle.write(b"b" * 100)
                second = transfer_dashboard.active_parts(current)

        self.assertEqual(first["total_rate_label"], "测量中")
        self.assertEqual(old_batch["count"], 0)
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["total_rate_bps"], 10.0)
        self.assertEqual(second["items"][0]["rate_label"], "10.0 B/s")

    def test_upload_throughput_uses_confirmed_bytes_rolling_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch.object(transfer_dashboard, "_UPLOAD_SNAPSHOT", {}), patch(
                "geo_ring_cloud_transfer_dashboard.time.time", side_effect=[100.0, 120.0]
            ):
                first = upload_throughput(root, {"completed_size_bytes": 100})
                second = upload_throughput(root, {"completed_size_bytes": 500})

        self.assertIsNone(first["rate_bps"])
        self.assertEqual(second["rate_bps"], 20.0)
        self.assertEqual(second["rate_label"], "20.0 B/s")

    def test_dashboard_trends_sample_at_five_minute_interval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir()
            sample = {
                "download_rate_bps": 100,
                "upload_rate_bps": 50,
                "disk_free_bytes": 1000,
                "download_percent": 25,
                "upload_percent": 10,
            }
            with patch.object(transfer_dashboard, "_TREND_LAST_WRITE", {}), patch(
                "geo_ring_cloud_transfer_dashboard.time.time",
                side_effect=[100.0, 101.0, 401.0],
            ):
                first = dashboard_trends(transfer, sample, True)
                second = dashboard_trends(transfer, sample, True)
                third = dashboard_trends(transfer, sample, True)

        self.assertEqual(len(first["samples"]), 1)
        self.assertEqual(len(second["samples"]), 1)
        self.assertEqual(len(third["samples"]), 2)
        self.assertEqual(third["sample_interval_seconds"], 300)

    def test_dashboard_trends_warms_up_second_sample_in_thirty_seconds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir()
            sample = {"download_rate_bps": 2.0, "upload_rate_bps": 3.0}
            with patch(
                "geo_ring_cloud_transfer_dashboard.time.time",
                side_effect=[100.0, 129.0, 130.0],
            ):
                first = dashboard_trends(transfer, sample, True)
                second = dashboard_trends(transfer, sample, True)
                third = dashboard_trends(transfer, sample, True)
        self.assertTrue(first["warmup_pending"])
        self.assertEqual(len(second["samples"]), 1)
        self.assertEqual(len(third["samples"]), 2)
        self.assertFalse(third["warmup_pending"])
        self.assertEqual(third["warmup_interval_seconds"], 30)

    def test_dashboard_trends_keeps_warmup_until_speed_is_observed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir()
            trend_path = transfer / "dashboard_trends.jsonl"
            trend_path.write_text(
                "{\"download_rate_bps\":null,\"upload_rate_bps\":null}\n"
                "{\"download_rate_bps\":null,\"upload_rate_bps\":null}\n",
                encoding="utf-8",
            )
            with patch(
                "geo_ring_cloud_transfer_dashboard.time.time", return_value=100.0
            ):
                result = dashboard_trends(
                    transfer,
                    {"download_rate_bps": None, "upload_rate_bps": 9.0},
                    True,
                )
        self.assertEqual(len(result["samples"]), 3)
        self.assertFalse(result["warmup_pending"])

    def test_open_cleanup_folder_requires_approval_and_never_deletes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            transfer = root / "transfer"
            transfer.mkdir(parents=True)
            raw = root / "raw.nc"
            raw.write_bytes(b"keep")
            dashboard = DashboardState(root)
            with self.assertRaisesRegex(RuntimeError, "先确认允许清理"):
                dashboard.open_cleanup_folder()
            (transfer / "local_cleanup_approval.json").write_text("{}", encoding="utf-8")
            with patch.object(transfer_dashboard.os, "name", "nt"), patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen"
            ) as opener:
                result = dashboard.open_cleanup_folder()
                self.assertTrue(result["opened"])
                self.assertFalse(result["delete_executed"])
                self.assertEqual(result["folder_kind"], "batch_root")
                opener.assert_called_once()
                self.assertEqual(raw.read_bytes(), b"keep")

    def test_fy4b_cleanup_folder_opens_external_official_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "GEO_Cloud_2024_batches" / "fy4b_20240601_20240601"
            transfer = root / "transfer"
            source = Path(temp_dir) / "official_fy4b"
            transfer.mkdir(parents=True)
            source.mkdir()
            raw = source / "FY4B-_AGRI--_N_DISK_1050E_L2-_CLM-_MULT_NOM_20240601000000_20240601001459_4000M_V0001.NC"
            raw.write_bytes(b"keep")
            (transfer / "local_cleanup_approval.json").write_text("{}", encoding="utf-8")
            (transfer / "fy4b_official_import_request.json").write_text(
                json.dumps({"source_root": str(source)}), encoding="utf-8"
            )
            dashboard = DashboardState(root)
            with patch.object(transfer_dashboard.os, "name", "nt"), patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen"
            ) as opener:
                result = dashboard.open_cleanup_folder()
            self.assertEqual(result["folder"], str(source.resolve()))
            self.assertEqual(result["folder_kind"], "fy4b_official_source")
            self.assertFalse(result["delete_executed"])
            self.assertEqual(raw.read_bytes(), b"keep")
            opener.assert_called_once()

    def test_dashboard_html_includes_safe_selection_fallback_and_full_part_view(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertIn("error.status===404 && selectedBatchName", html)
        self.assertIn("parts.items.slice(0,15)", html)
        self.assertIn("已结束记录", html)
        self.assertIn("open-cleanup-folder", html)
        self.assertIn("start-fy4b-official-upload", html)
        self.assertIn("preview-fy4b-official-upload", html)
        self.assertIn("fy4bPreviewPanel", html)
        self.assertIn("fy4bPreviewMappings", html)
        self.assertIn("FY4B 自动批次标识", html)
        self.assertIn("preparing_manifest", html)
        self.assertIn("复制路径", html)

    def test_dashboard_gates_never_delete_raw_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "GOES-16" / "ACMF" / "20240401" / "00" / "sample.nc"
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"raw-data-must-remain")
            manifests = root / "manifests"
            manifests.mkdir()
            (manifests / "manifest_inventory.csv").write_text(
                "target_time_utc,platform,service,product,collection_id,remote_type,bucket,"
                "remote_key_or_product_id,actual_start_time,actual_end_time,time_difference_seconds,"
                "size_bytes,status,local_path,note\n"
                "2024-04-01T00:00:00Z,GOES-16,GOES-16,ACMF,,s3,bucket,key,,,,20,found,"
                + str(raw)
                + ",ok\n",
                encoding="utf-8",
            )
            logs = root / "logs"
            logs.mkdir()
            (logs / "download_s3_range.log").write_text(
                "2026-08-08T00:00:00Z download_s3_range_start start=2024-04-01 "
                "end=2024-04-01 rows=1 skipped_existing=0 pending=1 max_workers=1\n"
                "2026-08-08T00:00:01Z 1/1 downloaded GOES-16 ACMF "
                "2024-04-01T00:00:00Z netcdf_ok\n",
                encoding="utf-8",
            )
            transfer = root / "transfer"
            transfer.mkdir()
            transfer_manifest = transfer / "geo_ring_cloud_transfer_20240401_20240401_manifest.json"
            transfer_manifest.write_text(
                json.dumps(
                    {
                        "status": "READY_FOR_XFTP_UPLOAD",
                        "file_count": 1,
                        "total_size_bytes": raw.stat().st_size,
                    }
                ),
                encoding="utf-8",
            )

            dashboard = DashboardState(root)
            self.assertEqual(dashboard.status()["overall_state"], "ready_for_xftp")
            dashboard.mark_xftp_complete()
            with self.assertRaisesRegex(RuntimeError, "尚未 PASS"):
                dashboard.approve_cleanup()
            (transfer / "server_verification.json").write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "verified_file_count": 1,
                        "failed_file_count": 0,
                        "results": [],
                    }
                ),
                encoding="utf-8",
            )
            marker = dashboard.approve_cleanup()
            self.assertTrue(marker["approval_only"])
            self.assertFalse(marker["delete_executed"])
            self.assertTrue(raw.is_file())
            self.assertEqual(raw.read_bytes(), b"raw-data-must-remain")

    def test_dashboard_progress_uses_full_inventory_not_only_started_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            manifests = root / "manifests"
            manifests.mkdir(parents=True)
            header = (
                "target_time_utc,platform,service,product,collection_id,remote_type,bucket,"
                "remote_key_or_product_id,actual_start_time,actual_end_time,time_difference_seconds,"
                "size_bytes,status,local_path,note\n"
            )
            (manifests / "manifest_inventory.csv").write_text(
                header
                + "2024-04-01T00:00:00Z,Himawari-9,Himawari-9,CMSK,,s3,b,k1,,,,10,found,h.nc,ok\n"
                + "2024-04-01T00:00:00Z,Meteosat-0deg,Meteosat-0deg,CLM,,eumetsat,,k2,,,,10,found,m0.nc,ok\n"
                + "2024-04-01T00:00:00Z,Meteosat-IODC,Meteosat-IODC,CLM,,eumetsat,,k3,,,,10,found,mi.nc,ok\n",
                encoding="utf-8",
            )
            (manifests / "manifest_downloaded.csv").write_text(
                header
                + "2024-04-01T00:00:00Z,Himawari-9,Himawari-9,CMSK,,s3,b,k1,,,,10,downloaded,h.nc,ok\n",
                encoding="utf-8",
            )
            (root / "transfer").mkdir()

            status = DashboardState(root).status()

        self.assertEqual(status["download"]["combined"]["overall_completed"], 1)
        self.assertEqual(status["download"]["combined"]["total"], 3)
        self.assertEqual(status["download"]["combined"]["remaining"], 2)
        self.assertEqual(status["download"]["combined"]["percent"], 33.33)
        self.assertEqual(status["download"]["combined"]["scope"], "full_inventory")
        by_platform = {row["platform"]: row for row in status["platforms"]}
        self.assertEqual(by_platform["Himawari-9"]["completed"], 1)
        self.assertEqual(by_platform["Meteosat-0deg"]["completed"], 0)

    def test_dashboard_does_not_mark_file_complete_failed_batch_as_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            manifests = root / "manifests"
            logs = root / "logs"
            transfer = root / "transfer"
            manifests.mkdir(parents=True)
            logs.mkdir()
            transfer.mkdir()
            header = (
                "target_time_utc,platform,service,product,collection_id,remote_type,bucket,"
                "remote_key_or_product_id,actual_start_time,actual_end_time,time_difference_seconds,"
                "size_bytes,status,local_path,note\n"
            )
            row = (
                "2024-05-06T00:00:00Z,Meteosat-0deg,Meteosat-0deg,CLM,,eumetsat,,"
                "product-id,,,,10,found,m0.zip,ok\n"
            )
            (manifests / "manifest_inventory.csv").write_text(
                header + row, encoding="utf-8"
            )
            (logs / "download_meteosat_range.log").write_text(
                "2026-08-14T09:00:00Z download_meteosat_range_start "
                "start=2024-05-06 end=2024-05-06 rows=1 skipped_existing=0 pending=1\n"
                "2026-08-14T09:00:01Z 1/1 downloaded Meteosat-0deg CLM "
                "2024-05-06T00:00:00Z zip_ok\n",
                encoding="utf-8",
            )
            (transfer / "batch_status.json").write_text(
                json.dumps({"status": "failed", "phase": "failed", "message": "retry warning"}),
                encoding="utf-8",
            )
            (transfer / "download_launcher_status.json").write_text(
                json.dumps({"status": "FAIL", "exit_code": 1, "message": "retry warning"}),
                encoding="utf-8",
            )

            status = DashboardState(root).status()

        self.assertEqual(status["download"]["combined"]["percent"], 100.0)
        self.assertEqual(
            status["download"]["combined"]["completion_state"],
            "files_complete_unfinalized",
        )
        download_stage = next(
            item for item in status["pipeline_stages"] if item["key"] == "download"
        )
        self.assertEqual(download_stage["status"], "warning")
        self.assertIn("最终清单", download_stage["detail"])

    def test_powershell_runner_tolerates_successful_native_stderr(self):
        script = (
            Path(__file__).resolve().parents[1] / "geo_ring_cloud_transfer_batch.ps1"
        ).read_text(encoding="utf-8-sig")
        function_body = script.split("function Invoke-DownloadPython", 1)[1].split(
            "function Clear-DownloadProxy", 1
        )[0]
        self.assertIn('$ErrorActionPreference = "Continue"', function_body)
        self.assertIn("$commandExitCode = $LASTEXITCODE", function_body)
        self.assertLess(
            function_body.index('$ErrorActionPreference = "Continue"'),
            function_body.index("$commandExitCode = $LASTEXITCODE"),
        )
        self.assertIn("if ($commandExitCode -ne 0)", function_body)

    def test_start_download_recovers_unowned_stale_control_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir) / "GEO_Cloud_2024_batches"
            existing = parent / "existing_batch"
            existing.mkdir(parents=True)
            target = parent / "20240401_20240401_h9"
            transfer = target / "transfer"
            transfer.mkdir(parents=True)
            lock = transfer / "batch_run.lock"
            lock.write_bytes(b"")
            dashboard = DashboardState(existing)
            fake_process = unittest.mock.Mock(pid=24682)
            fake_process.poll.return_value = None
            fake_process.wait.return_value = 0
            with patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen",
                return_value=fake_process,
            ), patch.object(
                DashboardState, "_watch_download_process"
            ), patch.object(
                DashboardState, "_active_download_task", return_value=None
            ):
                result = dashboard.start_download(
                    {
                        "start_date": "2024-04-01",
                        "end_date": "2024-04-01",
                        "platforms": ["Himawari-9"],
                        "inventory_workers": 8,
                        "download_workers": 4,
                        "adaptive_download": False,
                        "continuous_upload": False,
                    }
                )

            recovery = json.loads(
                (transfer / "stale_lock_recovery.json").read_text(encoding="utf-8")
            )
        self.assertTrue(result["stale_lock_recovered"])
        self.assertFalse(lock.exists())
        self.assertEqual(recovery["lock"]["size_bytes"], 0)
        self.assertFalse(recovery["automatic_delete"])

    def test_dashboard_html_is_chinese_control_surface(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertIn("GEO 数据搬运控制台", html)
        self.assertIn("七步门禁", html)
        self.assertIn("接管当前下载并自动上传", html)
        self.assertIn("/api/actions/start-auto-upload", html)
        self.assertIn("/api/actions/start-continuous-upload", html)
        self.assertIn('id="continuousUpload"', html)
        self.assertIn('id="adaptiveDownload"', html)
        self.assertIn("下载时单路上传", html)
        self.assertIn("2→3→4 路", html)
        self.assertIn('id="downloadParallelism"', html)
        self.assertIn('id="autoUploadParallelism"', html)
        self.assertIn('id="testEmailButton"', html)
        self.assertIn("/api/actions/send-test-email", html)
        self.assertIn('id="configureEmailButton"', html)
        self.assertIn("/api/actions/configure-email", html)
        self.assertIn("Windows DPAPI", html)
        self.assertIn("边下边传", html)
        self.assertIn("/api/actions/start-download", html)
        self.assertIn("清单并行数", html)
        self.assertIn("direct_only", html)
        self.assertIn("CLAAS-3（CMA/CTX/CPP）", html)
        self.assertIn("CLAAS_V003", html)
        self.assertIn("多任务中心", html)
        self.assertIn('id="downloadDrive"', html)
        self.assertIn("开启电脑通知", html)
        self.assertIn("已停止", html)
        self.assertIn("疑似卡住", html)
        self.assertIn("磁盘空间门禁未通过", html)
        self.assertIn("当前磁盘空间已满足（上次检查曾失败）", html)
        self.assertIn("上次因空间不足失败；当前空间已满足，可重新启动", html)
        self.assertNotIn("delete-local", html)

    def test_disk_gate_reports_exact_shortfall(self):
        payload = {
            "drive": "{}:{}".format("F", "\\"),
            "free_bytes": 173434687488,
            "needed_bytes_with_margin": 939260421938,
            "free_gib": 161.524,
            "needed_gib_with_margin": 874.754,
            "total_rows": 1392,
            "skipped_existing_rows": 0,
            "pending_rows": 1392,
        }
        gate = parse_disk_gate(
            "ERROR: RuntimeError: Not enough free space: {}".format(payload)
        )
        self.assertTrue(gate["exists"])
        self.assertFalse(gate["passes"])
        self.assertEqual(gate["shortfall_gib"], 713.23)
        self.assertEqual(gate["shortfall_label"], "713.230 GiB")

    def test_task_center_keeps_completed_upload_visible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            current = parent / "20240402_20240430_h9"
            previous = parent / "20240503_20240505_h9"
            for root in (current, previous):
                (root / "transfer").mkdir(parents=True)
            (current / "transfer" / "batch_status.json").write_text(
                json.dumps({"status": "running", "phase": "s3_download"}),
                encoding="utf-8",
            )
            (previous / "transfer" / "batch_status.json").write_text(
                json.dumps({"status": "complete", "phase": "ready_for_xftp"}),
                encoding="utf-8",
            )
            (previous / "transfer" / "auto_upload_status.json").write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "phase": "complete",
                        "file_count": 720,
                        "completed_files": 720,
                        "percent": 100,
                    }
                ),
                encoding="utf-8",
            )
            dashboard = DashboardState(current)
            tasks = dashboard.status()["tasks"]
        by_name = {row["batch_name"]: row for row in tasks}
        self.assertEqual(by_name[previous.name]["upload_status"], "PASS")
        self.assertEqual(by_name[previous.name]["upload_percent"], 100)

    def test_claas3_is_exposed_as_an_order_source_not_direct_download(self):
        config = ORDER_SOURCE_CONFIG["CLAAS3-0deg"]
        self.assertFalse(config["direct_download"])
        self.assertEqual(config["products"], ["CMA", "CTX", "CPP"])
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            root.mkdir()
            dashboard = DashboardState(root)
            status = dashboard.status()
        self.assertIn("CLAAS3-0deg", status["download_config"]["order_sources"])
        self.assertNotIn(
            "CLAAS3-0deg", status["download_config"]["available_platforms"]
        )

    def test_dashboard_starts_hidden_direct_only_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            existing = Path(temp_dir) / "existing_batch"
            existing.mkdir()
            dashboard = DashboardState(existing)
            fake_process = unittest.mock.Mock(pid=24680)
            fake_process.poll.return_value = None
            fake_process.wait.return_value = 0
            with patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen",
                return_value=fake_process,
            ) as popen, patch.object(
                DashboardState, "_watch_download_process"
            ):
                result = dashboard.start_download(
                    {
                        "start_date": "2024-04-02",
                        "end_date": "2024-04-30",
                        "platforms": ["GOES-16", "GOES-18"],
                        "inventory_workers": 12,
                        "download_workers": 6,
                        "refresh_inventory": False,
                        "continuous_upload": False,
                    }
                )
            self.assertEqual(result["network_mode"], "direct_only")
            self.assertEqual(result["inventory_workers"], 12)
            command = popen.call_args.args[0]
            self.assertIn("-InventoryWorkers", command)
            self.assertIn("12", command)
            self.assertIn("-DownloadWorkers", command)
            self.assertIn("-AdaptiveDownload", command)
            self.assertTrue(result["adaptive_download"])
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(environment["NO_PROXY"], "*")
            self.assertNotIn("HTTP_PROXY", environment)
            launcher = json.loads(
                (
                    Path(result["batch_root"])
                    / "transfer"
                    / "download_launcher_status.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(launcher["status"], "RUNNING")
            self.assertEqual(launcher["pid"], 24680)

    def test_dashboard_can_choose_download_drive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / "default" / "existing_batch"
            alternate_parent = root / "alternate" / "GEO_Cloud_2024_batches"
            existing.mkdir(parents=True)
            dashboard = DashboardState(existing)
            fake_process = unittest.mock.Mock(pid=24681)
            fake_process.poll.return_value = None
            fake_process.wait.return_value = 0
            drives = [
                {
                    "value": "Q:",
                    "batch_parent": str(alternate_parent),
                    "free_label": "900 GB",
                    "total_label": "1 TB",
                }
            ]
            with patch(
                "geo_ring_cloud_transfer_dashboard.available_download_drives",
                return_value=drives,
            ), patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen",
                return_value=fake_process,
            ), patch.object(DashboardState, "_watch_download_process"):
                result = dashboard.start_download(
                    {
                        "start_date": "2024-04-02",
                        "end_date": "2024-04-02",
                        "download_drive": "Q:",
                        "platforms": ["GOES-16"],
                        "inventory_workers": 8,
                        "download_workers": 4,
                        "continuous_upload": False,
                    }
                )
            self.assertEqual(Path(result["batch_root"]).parent, alternate_parent.resolve())

    def test_batch_queue_estimate_uses_calibrated_platform_profile(self):
        request = normalize_request(
            {
                "start_date": "2024-04-01",
                "end_date": "2024-04-30",
                "platforms": ["Himawari-9", "Meteosat-0deg", "Meteosat-IODC"],
            },
            ["Himawari-9", "Meteosat-0deg", "Meteosat-IODC"],
        )
        estimate = estimate_required_space(request)
        self.assertEqual(estimate["days"], 30)
        self.assertEqual(estimate["basis"], "adaptive_platform_product_v2")
        self.assertAlmostEqual(estimate["required_gib"], 939.95, places=2)
        self.assertEqual(estimate["fixed_overhead_gib"], 2.0)
        self.assertEqual(estimate["platform_safety_factor"]["Himawari-9"], 1.2)
        self.assertEqual(estimate["platform_safety_factor"]["Meteosat-0deg"], 1.3)
        self.assertEqual(
            estimate["catalogue_size_policy"]["eumetsat"],
            "enumeration_only_use_empirical_payload_rate",
        )

    def test_batch_queue_goes_estimate_reflects_current_product_volume(self):
        request = normalize_request(
            {
                "start_date": "2024-06-01",
                "end_date": "2024-06-10",
                "platforms": ["GOES-16", "GOES-18"],
            },
            ["GOES-16", "GOES-18"],
        )
        estimate = estimate_required_space(request)
        self.assertAlmostEqual(estimate["raw_gib"], 11.5, places=2)
        self.assertAlmostEqual(estimate["required_gib"], 16.95, places=2)

    def test_s3_inventory_refines_estimate_but_eumetsat_metadata_does_not(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            complete = root / "complete.nc"
            complete.write_bytes(b"x" * 100)
            inventory = root / "manifest_inventory.csv"
            inventory.write_text(
                "platform,remote_type,status,size_bytes,local_path\n"
                f"GOES-16,s3,found,100,{complete}\n"
                f"GOES-16,s3,found,1073741824,{root / 'pending.nc'}\n"
                f"Meteosat-0deg,eumetsat,found,565,{root / 'payload.zip'}\n",
                encoding="utf-8",
            )
            request = normalize_request(
                {
                    "start_date": "2024-06-01",
                    "end_date": "2024-06-01",
                    "platforms": ["GOES-16", "Meteosat-0deg"],
                },
                ["GOES-16", "Meteosat-0deg"],
            )
            refined = refine_estimate_from_inventory(
                estimate_required_space(request), inventory
            )
            self.assertEqual(refined["basis"], "trusted_inventory_pending_bytes_v2")
            self.assertEqual(refined["inventory_authoritative_platforms"], ["GOES-16"])
            self.assertAlmostEqual(refined["required_gib"], 3.332, places=3)
            self.assertEqual(
                refined["platform_estimates"]["Meteosat-0deg"]["estimate_source"]
                if "estimate_source" in refined["platform_estimates"]["Meteosat-0deg"]
                else "empirical",
                "empirical",
            )

    def test_waiting_queue_refreshes_persisted_v1_estimate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current_batch"
            current.mkdir()
            dashboard = DashboardState(current)
            request = normalize_request(
                {
                    "start_date": "2024-06-01",
                    "end_date": "2024-06-10",
                    "platforms": ["GOES-16", "GOES-18"],
                },
                ["GOES-16", "GOES-18"],
            )
            item = make_queue_item(
                request,
                {
                    "basis": "conservative_platform_day_v1",
                    "required_bytes": 528 * (1024**3),
                    "required_gib": 528,
                },
            )
            state = read_queue_state(dashboard.queue_state_path)
            state["items"].append(item)
            dashboard._save_queue_state(state)
            with patch.object(
                dashboard,
                "_active_download_task",
                return_value={"batch_name": "active"},
            ):
                dashboard.process_batch_queue_once()
            refreshed = read_queue_state(dashboard.queue_state_path)["items"][0]
            self.assertEqual(refreshed["estimate"]["basis"], "adaptive_platform_product_v2")
            self.assertAlmostEqual(refreshed["estimate"]["required_gib"], 16.95, places=2)

    def test_batch_queue_waits_for_active_download_without_creating_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current_batch"
            transfer = current / "transfer"
            transfer.mkdir(parents=True)
            (transfer / "batch_status.json").write_text(
                json.dumps({"status": "running", "phase": "s3_download"}),
                encoding="utf-8",
            )
            dashboard = DashboardState(current)
            with patch.object(
                dashboard,
                "_active_download_task",
                return_value={"batch_name": "current_batch"},
            ):
                item = dashboard.enqueue_download(
                    {
                        "start_date": "2024-05-01",
                        "end_date": "2024-05-01",
                        "platforms": ["GOES-16"],
                        "continuous_upload": False,
                    }
                )
            self.assertEqual(item["status"], "WAITING_ACTIVE_DOWNLOAD")
            self.assertFalse((root / item["target_batch_name"]).exists())
            stored = read_queue_state(dashboard.queue_state_path)
            self.assertFalse(stored["automatic_delete"])

    def test_batch_queue_waits_for_space_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current_batch"
            current.mkdir()
            dashboard = DashboardState(current)
            fake_usage = SimpleNamespace(total=1000, used=999, free=1)
            with patch(
                "geo_ring_cloud_transfer_dashboard.shutil.disk_usage",
                return_value=fake_usage,
            ):
                item = dashboard.enqueue_download(
                    {
                        "start_date": "2024-05-01",
                        "end_date": "2024-05-02",
                        "platforms": ["GOES-18"],
                        "continuous_upload": False,
                    }
                )
            self.assertEqual(item["status"], "WAITING_SPACE")
            self.assertGreater(item["gate"]["shortfall_bytes"], 0)
            cancelled = dashboard.cancel_queued_download(item["queue_id"])
            self.assertEqual(cancelled["status"], "CANCELLED")
            self.assertFalse(cancelled["automatic_delete"])

    def test_batch_queue_launches_once_when_gate_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current_batch"
            current.mkdir()
            dashboard = DashboardState(current)
            fake_process = unittest.mock.Mock(pid=24682)
            fake_process.poll.return_value = None
            fake_process.wait.return_value = 0
            fake_usage = SimpleNamespace(
                total=10 * (1024 ** 4), used=0, free=10 * (1024 ** 4)
            )
            with patch(
                "geo_ring_cloud_transfer_dashboard.shutil.disk_usage",
                return_value=fake_usage,
            ), patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen",
                return_value=fake_process,
            ) as popen, patch.object(DashboardState, "_watch_download_process"):
                item = dashboard.enqueue_download(
                    {
                        "start_date": "2024-05-03",
                        "end_date": "2024-05-03",
                        "platforms": ["GOES-16", "GOES-18"],
                        "continuous_upload": False,
                    }
                )
                dashboard.process_batch_queue_once()
            self.assertEqual(item["status"], "RUNNING")
            self.assertEqual(popen.call_count, 1)
            self.assertTrue((root / item["target_batch_name"] / "transfer").is_dir())

    def test_continuous_discovery_excludes_part_and_control_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_file = root / "GOES-16" / "ACMF" / "20240402" / "00" / "sample.nc"
            final_file.parent.mkdir(parents=True)
            final_file.write_bytes(b"complete")
            final_file.with_suffix(".nc.part").write_bytes(b"partial")
            control = root / "GOES-16" / "logs" / "20240402" / "debug.txt"
            control.parent.mkdir(parents=True)
            control.write_text("control", encoding="utf-8")

            discovered = discover_completed_files(
                root,
                "2024-04-01",
                "2024-04-30",
                ["GOES-16"],
            )

            self.assertEqual([row[1] for row in discovered], [final_file.resolve()])

    def test_windows_permission_error_uses_read_only_pid_fallback(self):
        with patch(
            "geo_ring_cloud_transfer_dashboard.os.kill",
            side_effect=PermissionError,
        ), patch("psutil.pid_exists", return_value=True):
            self.assertTrue(process_is_running(34056))

    def test_status_record_retries_transient_windows_lock(self):
        import os

        original_replace = os.replace

        def flaky_replace(source, target):
            flaky_replace.calls += 1
            if flaky_replace.calls == 1:
                raise PermissionError("temporary lock")
            return original_replace(source, target)

        flaky_replace.calls = 0
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "transfer" / "auto_upload_status.json"
            for writer, module_name in (
                (dashboard_write_json_atomic, "geo_ring_cloud_transfer_dashboard"),
                (uploader_write_json_atomic, "geo_ring_cloud_auto_uploader"),
            ):
                flaky_replace.calls = 0
                with patch(module_name + ".os.replace", side_effect=flaky_replace), patch(
                    module_name + ".time.sleep"
                ):
                    writer(target, {"status": "RUNNING"}, max_attempts=2)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["status"], "RUNNING")
                self.assertEqual(flaky_replace.calls, 2)

    def test_dashboard_marks_dead_uploader_as_stopped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir()
            (transfer / "auto_upload_status.json").write_text(
                json.dumps(
                    {
                        "status": "RUNNING",
                        "phase": "streaming_upload",
                        "pid": 36000,
                        "updated_at": "2026-08-10T16:32:25Z",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "geo_ring_cloud_transfer_dashboard.process_is_running", return_value=False
            ):
                status = auto_upload_status(transfer)
            self.assertEqual(status["status"], "STOPPED")
            self.assertEqual(status["phase"], "stopped")
            self.assertFalse(status["process_alive"])
            self.assertIn("安全续传", status["error"])

    def test_dashboard_marks_live_uploader_with_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir()
            (transfer / "auto_upload_status.json").write_text(
                json.dumps(
                    {
                        "status": "RUNNING",
                        "phase": "streaming_upload",
                        "pid": 36000,
                        "updated_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=16)
                        ).isoformat().replace("+00:00", "Z"),
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "geo_ring_cloud_transfer_dashboard.process_is_running", return_value=True
            ):
                status = auto_upload_status(transfer)
            self.assertEqual(status["status"], "STALLED")
            self.assertTrue(status["process_alive"])
            self.assertGreater(status["status_age_seconds"], 15 * 60)

    def test_dead_download_launcher_is_reported_as_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            transfer = root / "transfer"
            transfer.mkdir(parents=True)
            (transfer / "download_launcher_status.json").write_text(
                json.dumps(
                    {
                        "status": "RUNNING",
                        "pid": 99999999,
                        "message": "后台下载进程已经启动。",
                    }
                ),
                encoding="utf-8",
            )
            dashboard = DashboardState(root)
            status = dashboard.status()
        self.assertEqual(status["overall_state"], "launch_failed")
        self.assertEqual(status["launcher_status"]["status"], "FAIL")
        self.assertEqual(status["pipeline_stages"][0]["status"], "fail")

    def test_recent_part_keeps_orphaned_child_download_reported_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "batch"
            transfer = root / "transfer"
            transfer.mkdir(parents=True)
            part = root / "Himawari-9" / "CMSK" / "sample.nc.part"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"still-growing")
            (transfer / "batch_status.json").write_text(
                json.dumps({"status": "running", "phase": "s3_download"}),
                encoding="utf-8",
            )
            (transfer / "download_launcher_status.json").write_text(
                json.dumps({"status": "RUNNING", "pid": 99999999}),
                encoding="utf-8",
            )
            dashboard = DashboardState(root)
            status = dashboard.status()
        self.assertEqual(status["overall_state"], "running")
        self.assertEqual(status["launcher_status"]["status"], "RUNNING")
        self.assertTrue(status["launcher_status"]["detached_child_activity"])

    def test_finished_launcher_never_treats_recycled_pid_as_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            transfer = Path(temp_dir) / "transfer"
            transfer.mkdir(parents=True)
            (transfer / "download_launcher_status.json").write_text(
                json.dumps(
                    {
                        "status": "FAIL",
                        "pid": 11384,
                        "exit_code": 1,
                        "finished_at": "2026-08-10T13:27:20Z",
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "geo_ring_cloud_transfer_dashboard.process_is_running",
                return_value=True,
            ) as probe:
                status = download_launcher_status(transfer, {"status": "failed"})
        self.assertFalse(status["process_alive"])
        probe.assert_not_called()

    def test_auto_upload_manifest_is_remapped_to_dedicated_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "GOES-16" / "ACMF" / "20240401" / "00" / "sample.nc"
            local.parent.mkdir(parents=True)
            local.write_bytes(b"immutable")
            source = root / "transfer" / "source_manifest.json"
            source.parent.mkdir()
            source.write_text(
                json.dumps(
                    {
                        "status": "READY_FOR_XFTP_UPLOAD",
                        "batch_id": "20240401_20240401",
                        "server_root": "/data04/1/dhr",
                        "files": [
                            {
                                "local_path": str(local),
                                "remote_path": "/data04/1/dhr/GOES16/Cloud/GOES-16/ACMF/20240401/00/sample.nc",
                                "size_bytes": local.stat().st_size,
                                "sha256": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            progress = []
            output, payload = build_auto_upload_manifest(
                source,
                PurePosixPath("/data04/1/dhr/geo_ring_cloud_auto_upload"),
                progress_callback=progress.append,
            )
            self.assertTrue(output.is_file())
            self.assertEqual(payload["status"], "READY_FOR_AUTOMATED_SFTP_UPLOAD")
            self.assertEqual(
                payload["files"][0]["remote_path"],
                "/data04/1/dhr/geo_ring_cloud_auto_upload/GOES16/Cloud/GOES-16/ACMF/20240401/00/sample.nc",
            )
            self.assertEqual(
                payload["files"][0]["sha256"], hashlib.sha256(b"immutable").hexdigest()
            )
            self.assertEqual(progress[-1]["preflight_completed_files"], 1)
            self.assertEqual(progress[-1]["preflight_file_count"], 1)
            self.assertEqual(progress[-1]["preflight_percent"], 100.0)
            self.assertFalse(payload["deletion_policy"]["automatic_delete"])

    def test_fy4b_official_import_creates_control_batch_without_copying_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "GEO_Cloud_2024_batches" / "current_batch"
            root.mkdir(parents=True)
            source = Path(temp_dir) / "FY4B_official"
            raw = (
                source
                / "FY4B-_AGRI--_N_DISK_1050E_L2-_CLM-_MULT_"
                "NOM_20240401080000_20240401081459_4000M_V0001.NC"
            )
            raw.parent.mkdir(parents=True)
            raw.write_bytes(b"official-client-data")
            (source / "partial.part").write_bytes(b"incomplete")
            identity = Path(temp_dir) / "id_ed25519"
            identity.write_text("key", encoding="utf-8")
            dashboard = DashboardState(
                root,
                ssh_target="dhr@example",
                identity_file=identity,
                auto_upload_root="/data04/1/dhr/geo_ring_cloud_auto_upload",
                allowed_server_parent="/data04/1/dhr",
            )
            fake_process = unittest.mock.Mock(pid=24683)
            with patch(
                "geo_ring_cloud_transfer_dashboard.subprocess.Popen",
                return_value=fake_process,
            ):
                result = dashboard.start_fy4b_official_upload(
                    {"source_path": str(source)}
                )
            batch = root.parent / "fy4b_20240401_20240401_clm"
            manifest = json.loads(
                (batch / "transfer" / "geo_ring_cloud_transfer_fy4b_20240401_20240401_clm_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result["batch_name"], "fy4b_20240401_20240401_clm")
            self.assertFalse(result["resumed"])
            self.assertEqual(manifest["file_count"], 1)
            self.assertEqual(manifest["files"][0]["local_path"], str(raw.resolve()))
            self.assertEqual(manifest["files"][0]["product"], "CLM")
            self.assertEqual(manifest["files"][0]["nominal_time"], "20240401080000")
            self.assertEqual(
                manifest["files"][0]["remote_path"],
                "/data04/1/dhr/geo_ring_cloud_auto_upload/FY4B/CLM/20240401/08/"
                "FY4B-_AGRI--_N_DISK_1050E_L2-_CLM-_MULT_"
                "NOM_20240401080000_20240401081459_4000M_V0001.NC",
            )
            self.assertFalse((batch / "CLM").exists())
            self.assertEqual(raw.read_bytes(), b"official-client-data")
            self.assertFalse(manifest["deletion_policy"]["automatic_delete"])

    def test_fy4b_batch_label_includes_sorted_product_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "GEO_Cloud_2024_batches" / "current_batch"
            source = Path(temp_dir) / "FY4B_official"
            root.mkdir(parents=True)
            source.mkdir()
            dashboard = DashboardState(root)
            label = dashboard._fy4b_batch_label(
                source,
                [
                    {"product": "CTH", "nominal_time": "20240402000000"},
                    {"product": "CLM", "nominal_time": "20240401000000"},
                    {"product": "CTT", "nominal_time": "20240401010000"},
                    {"product": "CLM", "nominal_time": "20240402000000"},
                ],
            )
            self.assertEqual(label, "20240401_20240402_clm-cth-ctt")

    def test_fy4b_official_preview_rejects_ambiguous_nc_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "GEO_Cloud_2024_batches" / "current_batch"
            root.mkdir(parents=True)
            source = Path(temp_dir) / "FY4B_official"
            source.mkdir()
            (source / "not_an_official_fy4b_file.NC").write_bytes(b"unsafe")
            identity = Path(temp_dir) / "id_ed25519"
            identity.write_text("key", encoding="utf-8")
            dashboard = DashboardState(
                root,
                ssh_target="dhr@example",
                identity_file=identity,
                auto_upload_root="/data04/1/dhr/geo_ring_cloud_auto_upload",
                allowed_server_parent="/data04/1/dhr",
            )
            with self.assertRaisesRegex(RuntimeError, "无法安全判断变量、日期和小时"):
                dashboard.preview_fy4b_official_upload({"source_path": str(source)})

    def test_auto_upload_root_must_be_a_dedicated_child(self):
        validate_server_root(
            PurePosixPath("/data04/1/dhr/geo_ring_cloud_auto_upload"),
            PurePosixPath("/data04/1/dhr"),
        )
        with self.assertRaises(ValueError):
            validate_server_root(
                PurePosixPath("/data04/1/dhr"), PurePosixPath("/data04/1/dhr")
            )
        with self.assertRaises(ValueError):
            validate_server_root(
                PurePosixPath("/data04/other"), PurePosixPath("/data04/1/dhr")
            )

    def test_sftp_quote_rejects_newlines(self):
        self.assertEqual(sftp_quote("drive:/batch/file.nc"), '"drive:/batch/file.nc"')
        with self.assertRaises(ValueError):
            sftp_quote("bad\npath")

    def test_windows_ssh_children_use_no_window_flag(self):
        if sys.platform == "win32":
            self.assertNotEqual(subprocess_creation_flags(), 0)
            startupinfo = subprocess_startupinfo()
            self.assertIsNotNone(startupinfo)
            self.assertEqual(startupinfo.wShowWindow, subprocess.SW_HIDE)
        else:
            self.assertEqual(subprocess_creation_flags(), 0)
            self.assertIsNone(subprocess_startupinfo())

    def test_goes_inventory_platform_filter(self):
        target = datetime(2024, 4, 1, tzinfo=timezone.utc)
        with patch.object(geo_cloud_downloader, "list_s3_objects", return_value=[]):
            rows = geo_cloud_downloader.inventory_goes(
                Path("batch"), object(), target, {"GOES-16"}
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["platform"] for row in rows}, {"GOES-16"})

    def test_goes_daily_inventory_lists_remote_prefix_once(self):
        day = datetime(2024, 4, 1, tzinfo=timezone.utc)
        with patch.object(geo_cloud_downloader, "get_s3_client", return_value=object()), patch.object(
            geo_cloud_downloader, "list_s3_objects", return_value=[]
        ) as listing:
            rows = geo_cloud_downloader.inventory_goes_day(
                Path("batch"), day, "GOES-16", "ABI-L2-ACMF"
            )
        self.assertEqual(listing.call_count, 1)
        self.assertEqual(len(rows), 24)
        self.assertEqual({row["product"] for row in rows}, {"ACMF"})

    def test_inventory_cache_fingerprint_ignores_worker_tuning(self):
        request_four = geo_cloud_downloader.inventory_request(
            "combined_daily_inventory",
            "2024-04-01",
            "2024-04-30",
            {"GOES-16", "GOES-18"},
            4,
        )
        request_twelve = geo_cloud_downloader.inventory_request(
            "combined_daily_inventory",
            "2024-04-01",
            "2024-04-30",
            {"GOES-18", "GOES-16"},
            12,
        )
        self.assertEqual(request_four["fingerprint"], request_twelve["fingerprint"])
        changed = geo_cloud_downloader.inventory_request(
            "combined_daily_inventory",
            "2024-04-02",
            "2024-04-30",
            {"GOES-16", "GOES-18"},
            4,
        )
        self.assertNotEqual(request_four["fingerprint"], changed["fingerprint"])

    def test_inventory_cache_requires_matching_row_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "manifest_inventory.csv"
            csv_path.write_text(
                ",".join(geo_cloud_downloader.MANIFEST_FIELDS) + "\n" + "," * 14 + "\n",
                encoding="utf-8",
            )
            request = geo_cloud_downloader.inventory_request(
                "combined_daily_inventory",
                "2024-04-01",
                "2024-04-01",
                {"GOES-16"},
                8,
            )
            lineage_path = root / "manifest_inventory.lineage.json"
            lineage_path.write_text(
                json.dumps(
                    {"inventory_fingerprint": request["fingerprint"], "row_count": 1}
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                geo_cloud_downloader.inventory_cache_matches(csv_path, lineage_path, request)
            )
            lineage_path.write_text(
                json.dumps(
                    {"inventory_fingerprint": request["fingerprint"], "row_count": 2}
                ),
                encoding="utf-8",
            )
            self.assertFalse(
                geo_cloud_downloader.inventory_cache_matches(csv_path, lineage_path, request)
            )

    def test_s3_range_download_resumes_existing_part(self):
        payload = (b"0123456789abcdef" * 70000) + b"end"

        class FakeS3:
            def __init__(self):
                self.ranges = []

            def get_object(self, **kwargs):
                start_text, end_text = kwargs["Range"].removeprefix("bytes=").split("-")
                start, end = int(start_text), int(end_text)
                self.ranges.append((start, end))
                return {"Body": io.BytesIO(payload[start : end + 1])}

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "GOES-16" / "ACMF" / "20240401" / "00" / "sample.nc"
            target.parent.mkdir(parents=True)
            partial = target.with_name(target.name + ".part")
            partial.write_bytes(payload[:100])
            row = {
                "local_path": str(target),
                "bucket": "test-bucket",
                "remote_key_or_product_id": "sample-key",
                "size_bytes": str(len(payload)),
            }
            client = FakeS3()
            with patch.object(geo_cloud_downloader, "validate_file", return_value=(True, "ok")):
                success, note = geo_cloud_downloader.download_s3_row(client, row, range_mib=1)
            self.assertTrue(success)
            self.assertEqual(client.ranges[0][0], 100)
            self.assertIn("resumed_from=100", note)
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(partial.exists())

    def test_prepare_and_local_verify(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "GOES-16" / "ACMF" / "20240401" / "00" / "sample.nc"
            data.parent.mkdir(parents=True)
            data.write_bytes(b"immutable-test-data")
            output = root / "transfer"
            manifest = prepare_manifest(
                root,
                output,
                PurePosixPath("/server/data/dhr"),
                "2024-04-01",
                "2024-04-01",
                {"GOES-16"},
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "READY_FOR_XFTP_UPLOAD")
            self.assertEqual(payload["file_count"], 1)
            self.assertFalse(payload["deletion_policy"]["automatic_delete"])
            self.assertIn("sha256", payload["generating_script_state"])
            self.assertIn(
                "commit_represents_script", payload["generating_script_state"]
            )
            self.assertEqual(
                payload["code_commit_scope"], "repository_head_at_manifest_write"
            )
            self.assertEqual(
                payload["files"][0]["remote_path"],
                "/server/data/dhr/GOES16/Cloud/GOES-16/ACMF/20240401/00/sample.nc",
            )
            report = output / "local_verification.json"
            self.assertEqual(verify_manifest(manifest, report, "local"), 0)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["status"], "PASS")

    def test_partial_file_blocks_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            partial = root / "GOES-18" / "ACMF" / "20240401" / "00" / "sample.nc.part"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"partial")
            with self.assertRaisesRegex(RuntimeError, "part file"):
                prepare_manifest(
                    root,
                    root / "transfer",
                    PurePosixPath("/server/data/dhr"),
                    "2024-04-01",
                    "2024-04-01",
                    {"GOES-18"},
                )

    def test_control_partial_file_does_not_block_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "GOES-18" / "ACMF" / "20240401" / "00" / "sample.nc"
            data.parent.mkdir(parents=True)
            data.write_bytes(b"complete-data")
            control_partial = root / "transfer" / "auto_upload_status.json.part"
            control_partial.parent.mkdir(parents=True)
            control_partial.write_bytes(b"stale-control-state")

            manifest = prepare_manifest(
                root,
                root / "transfer",
                PurePosixPath("/server/data/dhr"),
                "2024-04-01",
                "2024-04-01",
                {"GOES-18"},
            )

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "READY_FOR_XFTP_UPLOAD")
            self.assertEqual(payload["file_count"], 1)
            self.assertTrue(control_partial.is_file())


if __name__ == "__main__":
    unittest.main()
