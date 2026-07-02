from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.errors import ValidationError
from biohub_esm_lib.routing import RouteRequest, route_request


class RoutingTests(unittest.TestCase):
    def test_atlas_discovery_and_bulk(self) -> None:
        self.assertEqual(route_request(RouteRequest(task="atlas")).route, "atlas-api")
        self.assertEqual(
            route_request(RouteRequest(task="atlas", bulk_dataset=True)).route, "atlas-s3"
        )
        private = route_request(RouteRequest(task="atlas", private=True))
        self.assertEqual(private.route, "atlas-s3")
        self.assertIn("do not send", " ".join(private.rationale))

    def test_modest_managed_defaults(self) -> None:
        esmc = route_request(RouteRequest(task="esmc"))
        self.assertEqual((esmc.route, esmc.model, esmc.credential), ("biohub", "esmc-600m-2024-12", "ESM_API_KEY"))
        fold = route_request(RouteRequest(task="fold"))
        self.assertEqual((fold.route, fold.model), ("biohub", "esmfold2-fast-2026-05"))

    def test_msa_or_accuracy_selects_full(self) -> None:
        self.assertEqual(
            route_request(RouteRequest(task="fold", has_msa=True)).model,
            "esmfold2-2026-05",
        )
        self.assertEqual(
            route_request(RouteRequest(task="fold", accuracy_priority=True)).model,
            "esmfold2-2026-05",
        )

    def test_scale_out_uses_modal_without_esm_key(self) -> None:
        result = route_request(RouteRequest(task="fold", item_count=500))
        self.assertEqual(result.route, "modal")
        self.assertNotIn("ESM_API_KEY", result.credential or "")
        esmc = route_request(RouteRequest(task="esmc", item_count=500))
        self.assertEqual(esmc.model, "biohub/ESMC-600M")

    def test_private_and_custom_use_self_hosted(self) -> None:
        for field in ("private", "offline", "data_residency", "custom_model", "fine_tune", "sustained_workload"):
            result = route_request(RouteRequest(task="esmc", **{field: True}))
            self.assertEqual(result.route, "self-hosted", field)
            self.assertEqual(result.model, "biohub/ESMC-600M", field)

    def test_binder_never_routes_to_biohub(self) -> None:
        defaults = route_request(RouteRequest(task="binder-design"))
        private = route_request(RouteRequest(task="binder-design", private=True))
        self.assertEqual(defaults.route, "modal")
        self.assertEqual(private.route, "self-hosted")
        self.assertIsNone(defaults.model)
        self.assertIsNone(private.model)
        self.assertIn("84939b0e7441198d16d3b37c937d18c0637b9729", defaults.workflow or "")
        self.assertIn("experimental", " ".join(defaults.rationale))
        self.assertIn("not available", " ".join(defaults.rationale))
        self.assertTrue(any("1,000" in warning for warning in defaults.warnings))

    def test_invalid_item_count(self) -> None:
        with self.assertRaises(ValidationError):
            route_request(RouteRequest(task="fold", item_count=0))


if __name__ == "__main__":
    unittest.main()
