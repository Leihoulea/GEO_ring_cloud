from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer


CODE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CODE_DIR.parents[2]
TIME_RUNS_ROOT = PROJECT_ROOT / "geo_ring_cloud_stage1_time_runs"
sys.path.insert(0, str(CODE_DIR))

from geo_ring_cloud_claas3_adapter import discover_files, read_product, select_for_time  # noqa: E402
from path_config import CLAAS3_ROOT  # noqa: E402


@unittest.skipUnless(CLAAS3_ROOT.exists(), f"CLAAS-3 root not available: {CLAAS3_ROOT}")
class RealClaas3IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records, cls.duplicates = discover_files(CLAAS3_ROOT)

    def test_scoped_cadence_day_night_and_month_end(self) -> None:
        scoped = [item for item in self.records if "2024-03-05" <= item.nominal_time[:10] <= "2024-03-31"]
        self.assertEqual(sum(item.product == "CMA" for item in scoped), 2592)
        self.assertEqual(sum(item.product == "CTX" for item in scoped), 648)
        self.assertEqual(sum(item.product == "CPP" for item in scoped), 648)
        for target in ("2024-03-05T00:00:00Z", "2024-03-15T12:00:00Z", "2024-03-31T23:00:00Z"):
            for product in ("CMA", "CTX", "CPP"):
                selected, delta = select_for_time(self.records, product, target)
                self.assertIsNotNone(selected)
                self.assertEqual(delta, 0.0)

    def test_projection_orientation_center_and_off_disc_corner(self) -> None:
        record, _ = select_for_time(self.records, "CTX", "2024-03-15T12:00:00Z")
        bundle = read_product(record.path)
        x = bundle.arrays["projection_x"]
        y = bundle.arrays["projection_y"]
        attrs = bundle.metadata["geostationary_projection_attrs"]
        self.assertGreater(float(x[-1]), float(x[0]))
        self.assertLess(float(y[-1]), float(y[0]))
        h = float(attrs["perspective_point_height"])
        geos = CRS.from_proj4(
            f"+proj=geos +h={h} +lon_0={attrs['longitude_of_projection_origin']} "
            f"+a={attrs['semi_major_axis']} +b={attrs['semi_minor_axis']} "
            f"+sweep={attrs['sweep_angle_axis']} +units=m +no_defs"
        )
        transform = Transformer.from_crs(geos, CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs"), always_xy=True)
        center = len(x) // 2
        lon_center, lat_center = transform.transform(float(x[center] * h), float(y[center] * h))
        lon_corner, lat_corner = transform.transform(float(x[0] * h), float(y[0] * h))
        self.assertLess(abs(lon_center), 0.1)
        self.assertLess(abs(lat_center), 0.1)
        self.assertFalse(np.isfinite(lon_corner) and np.isfinite(lat_corner))

    def test_cpp_nighttime_variable_masks_remain_independent(self) -> None:
        record, _ = select_for_time(self.records, "CPP", "2024-03-05T00:00:00Z")
        bundle = read_product(record.path)
        phase = bundle.arrays["fusion_valid_mask_cloud_phase"].astype(bool)
        cot = bundle.arrays["fusion_valid_mask_cloud_optical_thickness"].astype(bool)
        self.assertGreater(np.count_nonzero(phase), 0)
        self.assertGreater(np.count_nonzero(phase & ~cot), 0)

    def test_operational_baseline_bitwise_regression_for_four_legacy_runs(self) -> None:
        run_tags = ("20240306_1300", "20240317_1200", "20240324_1200", "20240329_1300")
        products = (
            "fused_cloud_mask.npz",
            "fused_cloud_top_height_km.npz",
            "source_map_cloud_mask.npz",
            "source_map_cloud_top_height_km.npz",
        )
        for tag in run_tags:
            legacy_root = TIME_RUNS_ROOT / tag / "fused_best_source"
            current_root = TIME_RUNS_ROOT / f"claas3_epic_{tag}" / "operational_baseline" / "fused_best_source"
            self.assertTrue(legacy_root.is_dir(), f"missing legacy run: {legacy_root}")
            self.assertTrue(current_root.is_dir(), f"missing profile run: {current_root}")
            for product in products:
                with self.subTest(time_tag=tag, product=product):
                    with np.load(legacy_root / product, allow_pickle=False) as legacy, np.load(
                        current_root / product, allow_pickle=False
                    ) as current:
                        for key in ("data", "valid_mask"):
                            self.assertEqual(legacy[key].dtype, current[key].dtype)
                            self.assertEqual(legacy[key].shape, current[key].shape)
                            self.assertEqual(
                                legacy[key].tobytes(order="C"),
                                current[key].tobytes(order="C"),
                                f"bitwise regression: {tag} {product} {key}",
                            )


if __name__ == "__main__":
    unittest.main()
