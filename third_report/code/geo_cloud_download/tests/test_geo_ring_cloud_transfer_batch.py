import json
import io
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

from geo_ring_cloud_transfer_batch import prepare_manifest, verify_manifest  # noqa: E402
from geo_ring_cloud_transfer_dashboard import (  # noqa: E402
    DashboardState,
    HTML_PATH,
    ORDER_SOURCE_CONFIG,
)
from geo_ring_cloud_auto_uploader import (  # noqa: E402
    build_auto_upload_manifest,
    sftp_quote,
    subprocess_creation_flags,
    validate_server_root,
)
import geo_cloud_downloader  # noqa: E402


class TransferBatchTests(unittest.TestCase):
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

    def test_dashboard_html_is_chinese_control_surface(self):
        html = HTML_PATH.read_text(encoding="utf-8")
        self.assertIn("GEO 数据搬运控制台", html)
        self.assertIn("七步门禁", html)
        self.assertIn("开始／续传自动上传", html)
        self.assertIn("/api/actions/start-auto-upload", html)
        self.assertIn("/api/actions/start-download", html)
        self.assertIn("清单并行数", html)
        self.assertIn("direct_only", html)
        self.assertIn("CLAAS-3（CMA/CTX/CPP）", html)
        self.assertIn("CLAAS_V003", html)
        self.assertNotIn("delete-local", html)

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
                    }
                )
            self.assertEqual(result["network_mode"], "direct_only")
            self.assertEqual(result["inventory_workers"], 12)
            command = popen.call_args.args[0]
            self.assertIn("-InventoryWorkers", command)
            self.assertIn("12", command)
            self.assertIn("-DownloadWorkers", command)
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
                                "sha256": "placeholder",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output, payload = build_auto_upload_manifest(
                source,
                PurePosixPath("/data04/1/dhr/geo_ring_cloud_auto_upload"),
            )
            self.assertTrue(output.is_file())
            self.assertEqual(payload["status"], "READY_FOR_AUTOMATED_SFTP_UPLOAD")
            self.assertEqual(
                payload["files"][0]["remote_path"],
                "/data04/1/dhr/geo_ring_cloud_auto_upload/GOES16/Cloud/GOES-16/ACMF/20240401/00/sample.nc",
            )
            self.assertFalse(payload["deletion_policy"]["automatic_delete"])

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
        else:
            self.assertEqual(subprocess_creation_flags(), 0)

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


if __name__ == "__main__":
    unittest.main()
