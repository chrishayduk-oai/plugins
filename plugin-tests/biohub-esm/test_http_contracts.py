from __future__ import annotations

import binascii
import json
import sys
import tempfile
import unittest
import io
import struct
import threading
import zipfile
import zlib
from datetime import datetime, timezone
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins" / "biohub-esm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from biohub_esm_lib.atlas import AtlasClient
from biohub_esm_lib.errors import APIError, SchemaDriftError, ValidationError
from biohub_esm_lib.http import BiohubClient, HTTPResponse, UrllibTransport, _retry_after


def response(status: int, payload: object = None, headers: dict[str, str] | None = None) -> HTTPResponse:
    if isinstance(payload, bytes):
        body = payload
    elif payload is None:
        body = b""
    else:
        body = json.dumps(payload).encode()
    return HTTPResponse(status=status, headers=headers or {}, body=body)


def zip_payload(name: str = "result.txt", content: bytes = b"ok") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return buffer.getvalue()


def png_payload() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x20\x40\x60"))
        + chunk(b"IEND", b"")
    )


class ScriptedTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers=None, body=None, timeout=120):
        self.calls.append(
            {"method": method, "url": url, "headers": headers or {}, "body": body, "timeout": timeout}
        )
        if not self.responses:
            raise AssertionError("unexpected HTTP call")
        return self.responses.pop(0)


class BiohubHTTPTests(unittest.TestCase):
    def test_retry_after_supports_http_date(self) -> None:
        now = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            _retry_after(
                {"retry-after": "Wed, 01 Jul 2026 00:00:30 GMT"}, now=now
            ),
            30.0,
        )
        self.assertEqual(
            _retry_after(
                {"retry-after": "Tue, 30 Jun 2026 23:59:00 GMT"}, now=now
            ),
            0.0,
        )
        self.assertIsNone(_retry_after({"retry-after": "Infinity"}, now=now))

    def test_incomplete_nonstream_response_is_normalized_as_partial(self) -> None:
        class PartialResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                raise IncompleteRead(b"partial", 10)

        transport = UrllibTransport()
        with patch.object(transport._opener, "open", return_value=PartialResponse()):
            with self.assertRaises(APIError) as caught:
                transport.request("GET", "https://example.test")
        self.assertEqual(caught.exception.kind, "network")
        self.assertTrue(caught.exception.partial)

    def test_redirect_never_forwards_authorization_to_another_origin(self) -> None:
        sink_requests: list[str | None] = []
        redirect_requests: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                sink_requests.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, format: str, *args: object) -> None:
                pass

        sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_url = f"http://127.0.0.1:{sink.server_address[1]}/sink"

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                redirect_requests.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", sink_url)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (sink, redirect)
        ]
        for thread in threads:
            thread.start()
        try:
            result = UrllibTransport().request(
                "POST",
                f"http://127.0.0.1:{redirect.server_address[1]}/start",
                headers={"authorization": "Bearer test-only-secret"},
                body=b"{}",
            )
        finally:
            for server in (redirect, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)
        self.assertEqual(result.status, 302)
        self.assertEqual(redirect_requests, ["Bearer test-only-secret"])
        self.assertEqual(sink_requests, [])

    def test_bearer_contract_and_success(self) -> None:
        transport = ScriptedTransport([response(200, {"logits": {"sequence": [1]}})])
        client = BiohubClient(token="secret-key", transport=transport)
        result = client.post("logits", {"model": "esmc-300m-2024-12"})
        self.assertIn("logits", result)
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://biohub.ai/api/v1/logits")
        self.assertEqual(call["headers"]["authorization"], "Bearer secret-key")

    def test_missing_auth_stops_before_network(self) -> None:
        transport = ScriptedTransport([])
        with self.assertRaisesRegex(APIError, "ESM_API_KEY is missing"):
            BiohubClient(token="", transport=transport).post("logits", {})
        self.assertEqual(transport.calls, [])

    def test_unsupported_model_stops_before_network(self) -> None:
        transport = ScriptedTransport([])
        with self.assertRaisesRegex(ValidationError, "managed model ID"):
            BiohubClient(token="x", transport=transport).post(
                "logits", {"model": "not-a-model"}
            )
        self.assertEqual(transport.calls, [])

    def test_nonfinite_outbound_payload_stops_before_network(self) -> None:
        transport = ScriptedTransport([])
        with self.assertRaisesRegex(ValidationError, "non-finite"):
            BiohubClient(token="x", transport=transport).post(
                "logits",
                {"model": "esmc-300m-2024-12", "temperature": float("nan")},
            )
        self.assertEqual(transport.calls, [])

    def test_nonfinite_request_timeouts_stop_before_network(self) -> None:
        for value in (float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                BiohubClient(token="x", transport=ScriptedTransport([]), timeout=value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AtlasClient(transport=ScriptedTransport([]), timeout=value)

    def test_credentials_cannot_be_redirected_to_a_custom_base_url(self) -> None:
        with self.assertRaisesRegex(ValidationError, "only be sent"):
            BiohubClient(token="secret-key", base_url="https://example.test")

    def test_auth_failure_redacts_echoed_header(self) -> None:
        transport = ScriptedTransport(
            [response(401, {"message": "Authorization Bearer secret-key rejected"})]
        )
        with self.assertRaises(APIError) as caught:
            BiohubClient(token="secret-key", transport=transport).post(
                "logits", {"model": "esmc-300m-2024-12"}
            )
        self.assertEqual(caught.exception.kind, "authentication")
        self.assertNotIn("secret-key", str(caught.exception))

    def test_rate_limit_and_credits_are_normalized(self) -> None:
        for status, kind in ((429, "rate-limit"), (402, "credits")):
            transport = ScriptedTransport([response(status, {"message": "try later"}, {"retry-after": "7"})])
            with self.assertRaises(APIError) as caught:
                BiohubClient(token="x", transport=transport).post(
                    "logits", {"model": "esmc-300m-2024-12"}
                )
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(caught.exception.retry_after, 7)

    def test_malformed_success_is_schema_drift(self) -> None:
        transport = ScriptedTransport([response(200, b"not-json")])
        with self.assertRaises(SchemaDriftError):
            BiohubClient(token="x", transport=transport).post(
                "fold_all_atom", {"model": "esmfold2-fast-2026-05"}
            )

    def test_nonfinite_provider_json_is_schema_drift(self) -> None:
        transport = ScriptedTransport([response(200, b'{"score":NaN}')])
        with self.assertRaises(SchemaDriftError):
            BiohubClient(token="x", transport=transport).post(
                "logits", {"model": "esmc-300m-2024-12"}
            )

    def test_nonfinite_error_body_still_normalizes_http_status(self) -> None:
        transport = ScriptedTransport([response(429, b'{"detail":NaN}')])
        with self.assertRaises(APIError) as caught:
            BiohubClient(token="x", transport=transport).post(
                "logits", {"model": "esmc-300m-2024-12"}
            )
        self.assertEqual(caught.exception.kind, "rate-limit")
        self.assertEqual(str(caught.exception), "provider returned HTTP 429")


class AtlasHTTPTests(unittest.TestCase):
    sequence = "MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG"

    def test_biological_inputs_cannot_be_redirected_to_a_custom_host(self) -> None:
        transport = ScriptedTransport([])
        with self.assertRaisesRegex(ValidationError, "canonical Biohub host"):
            AtlasClient(base_url="https://example.test", transport=transport)
        self.assertEqual(transport.calls, [])

    def test_search_contract_and_empty_hits(self) -> None:
        transport = ScriptedTransport(
            [response(200, {"query_sequence": self.sequence, "similar_proteins": [], "restricted_count": 0})]
        )
        result = AtlasClient(transport=transport).search(
            self.sequence, topk_results=3, include_cluster_info=True
        )
        self.assertEqual(result["similar_proteins"], [])
        call = transport.calls[0]
        self.assertNotIn("authorization", call["headers"])
        query = parse_qs(urlparse(call["url"]).query)
        self.assertEqual(query["topk_results"], ["3"])
        self.assertEqual(query["include_cluster_info"], ["true"])

    def test_search_schema_drift(self) -> None:
        transport = ScriptedTransport([response(200, {"hits": []})])
        with self.assertRaisesRegex(SchemaDriftError, "missing") as caught:
            AtlasClient(transport=transport).search(self.sequence)
        self.assertEqual(caught.exception.raw, {"hits": []})

    def test_response_identity_mismatches_are_schema_drift(self) -> None:
        cases = (
            (
                lambda client: client.search(self.sequence),
                response(
                    200,
                    {"query_sequence": "MKT", "similar_proteins": []},
                ),
            ),
            (
                lambda client: client.protein("a" * 32),
                response(200, {"protein_hash": "b" * 32}),
            ),
            (
                lambda client: client.cluster("a" * 32),
                response(
                    200,
                    {
                        "protein_hash": "b" * 32,
                        "cluster_size": 1,
                        "member_protein_hashes": ["b" * 32],
                    },
                ),
            ),
            (
                lambda client: client.feature(4),
                response(
                    200,
                    {"feature_index": 5, "label": "x", "description": "y"},
                ),
            ),
        )
        for invoke, provider_response in cases:
            with self.subTest(provider_response=provider_response), self.assertRaises(
                SchemaDriftError
            ):
                invoke(AtlasClient(transport=ScriptedTransport([provider_response])))

    def test_atlas_parameter_types_stop_before_network(self) -> None:
        cases = (
            lambda client: client.search(self.sequence, topk_results=True),
            lambda client: client.search(self.sequence, topk_features=1.5),
            lambda client: client.search(self.sequence, include_cluster_info=1),
            lambda client: client.search(
                self.sequence, cluster_pct_characterized_max=False
            ),
            lambda client: client.protein("a" * 32, fold_on_miss=1),
            lambda client: client.cluster("a" * 32, topk_features=True),
            lambda client: client.submit_batch(["a" * 32], include_structure=1),
            lambda client: client.submit_batch(["a" * 32], include_features={}),
        )
        for invoke in cases:
            transport = ScriptedTransport([])
            with self.subTest(invoke=invoke), self.assertRaises(ValidationError):
                invoke(AtlasClient(transport=transport))
            self.assertEqual(transport.calls, [])

    def test_partial_optional_protein_fields_are_valid(self) -> None:
        transport = ScriptedTransport([response(200, {"protein_hash": "a" * 32})])
        result = AtlasClient(transport=transport).protein("a" * 32)
        self.assertEqual(result, {"protein_hash": "a" * 32})

    def test_fold_on_miss_requires_matching_bounded_sequence(self) -> None:
        sequence = "MKT"
        import hashlib

        digest = hashlib.md5(sequence.encode(), usedforsecurity=False).hexdigest()
        transport = ScriptedTransport([response(200, {"protein_hash": digest})])
        client = AtlasClient(transport=transport)
        with self.assertRaises(ValidationError):
            client.protein(digest, fold_on_miss=True)
        client.protein(digest, fold_on_miss=True, sequence_for_fold=sequence)

    def test_cluster_and_feature_contracts(self) -> None:
        transport = ScriptedTransport(
            [
                response(200, {"protein_hash": "a" * 32, "cluster_size": 2, "member_protein_hashes": ["b" * 32]}),
                response(200, {"data": []}),
                response(200, {"feature_index": 4, "label": "helix", "description": "test"}),
            ]
        )
        client = AtlasClient(transport=transport)
        self.assertEqual(client.cluster("a" * 32)["cluster_size"], 2)
        self.assertEqual(client.features()["data"], [])
        self.assertEqual(client.feature(4)["label"], "helix")

    def test_batch_async_lifecycle_and_cancel(self) -> None:
        transport = ScriptedTransport(
            [
                response(202, {"status": "pending", "job_id": "job-1", "poll_url": "/job-1"}),
                response(202, {"status": "pending", "job_id": "job-1", "completed_count": 1, "total_count": 2}),
                response(
                    200,
                    {
                        "status": "completed",
                        "job_id": "job-1",
                        "download_url": "https://example.test/out.zip",
                    },
                ),
                response(204),
            ]
        )
        client = AtlasClient(transport=transport)
        status, job, _ = client.submit_batch(["a" * 32, "b" * 32])
        self.assertEqual((status, job["job_id"]), (202, "job-1"))
        self.assertEqual(client.batch_status("job-1")[1]["status"], "pending")
        self.assertEqual(client.batch_status("job-1")[1]["status"], "completed")
        client.cancel_batch("job-1")
        self.assertEqual(transport.calls[-1]["method"], "DELETE")
        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(
            payload["include_features"],
            {"protein_level": True, "per_residue": True},
        )

    def test_batch_synchronous_zip(self) -> None:
        transport = ScriptedTransport([response(200, zip_payload())])
        status, content, _ = AtlasClient(transport=transport).submit_batch(["a" * 32])
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b"PK"))

    def test_batch_and_thumbnail_binary_schema_drift(self) -> None:
        with self.assertRaises(SchemaDriftError):
            AtlasClient(transport=ScriptedTransport([response(200, b"not-zip")])).submit_batch(
                ["a" * 32]
            )
        with self.assertRaises(SchemaDriftError):
            AtlasClient(transport=ScriptedTransport([response(200, b"not-png")])).thumbnail(
                "a" * 32, "plddt"
            )
        valid = png_payload()
        for invalid in (valid[:8], valid[:-12], valid[:-1], valid[:40] + b"x" + valid[41:]):
            with self.subTest(size=len(invalid)), self.assertRaises(SchemaDriftError):
                AtlasClient(
                    transport=ScriptedTransport([response(200, invalid)])
                ).thumbnail("a" * 32, "plddt")

    def test_unknown_batch_status_is_schema_drift(self) -> None:
        transport = ScriptedTransport(
            [response(200, {"status": "mystery", "job_id": "job"})]
        )
        with self.assertRaises(SchemaDriftError):
            AtlasClient(transport=transport).batch_status("job")

    def test_completed_batch_requires_https_download_and_valid_counts(self) -> None:
        for payload in (
            {"status": "completed", "job_id": "job-1"},
            {
                "status": "completed",
                "job_id": "job-1",
                "download_url": "http://example.test/result.zip",
            },
            {
                "status": "completed",
                "job_id": "job-1",
                "download_url": "https://user:password@example.test/result.zip",
            },
            {
                "status": "completed",
                "job_id": "job-1",
                "download_url": "https:///result.zip",
            },
            {
                "status": "completed",
                "job_id": "job-1",
                "download_url": "https://localhost/result.zip",
            },
            {
                "status": "pending",
                "job_id": "job-1",
                "completed_count": 2,
                "total_count": 1,
            },
            {
                "status": "pending",
                "job_id": "job-1",
                "completed_count": "one",
            },
        ):
            with self.subTest(payload=payload):
                transport = ScriptedTransport([response(200, payload)])
                with self.assertRaises(SchemaDriftError):
                    AtlasClient(transport=transport).batch_status("job-1")

    def test_download_rejects_userinfo_before_transport(self) -> None:
        transport = ScriptedTransport([])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValidationError):
                AtlasClient(transport=transport).download(
                    "https://user:password@example.test/result.zip",
                    Path(directory) / "result.zip",
                )
        self.assertEqual(transport.calls, [])

    def test_expired_batch_is_retained_as_terminal_state(self) -> None:
        transport = ScriptedTransport([response(410)])
        status, result = AtlasClient(transport=transport).batch_status("job-1")
        self.assertEqual(status, 410)
        self.assertEqual(result, {"status": "expired", "job_id": "job-1"})

    def test_batch_job_id_is_path_safe(self) -> None:
        for job_id in (
            ".",
            "..",
            "...",
            "-",
            "../job",
            "job?token=secret",
            "job#fragment",
            "x" * 129,
        ):
            for operation in ("batch_status", "cancel_batch"):
                with self.subTest(job_id=job_id, operation=operation):
                    transport = ScriptedTransport([])
                    with self.assertRaises(ValidationError):
                        getattr(AtlasClient(transport=transport), operation)(job_id)
                    self.assertEqual(transport.calls, [])

    def test_resume_download_uses_range(self) -> None:
        archive = zip_payload()
        offset = 6
        transport = ScriptedTransport(
            [
                response(
                    206,
                    archive[offset:],
                    {"content-range": f"bytes {offset}-{len(archive) - 1}/{len(archive)}"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.zip"
            partial = Path(str(destination) + ".partial")
            partial.write_bytes(archive[:offset])
            artifact = AtlasClient(transport=transport).download(
                "https://example.test/result.zip", destination
            )
            self.assertEqual(destination.read_bytes(), archive)
        self.assertTrue(artifact["resumed"])
        self.assertEqual(artifact["http_status"], 206)
        self.assertEqual(transport.calls[0]["headers"]["range"], "bytes=6-")

    def test_resume_download_rejects_mismatched_content_range(self) -> None:
        archive = zip_payload()
        offset = 6
        transport = ScriptedTransport(
            [response(206, archive[offset:], {"content-range": f"bytes 0-{len(archive) - 1}/{len(archive)}"})]
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.zip"
            partial = Path(str(destination) + ".partial")
            partial.write_bytes(archive[:offset])
            with self.assertRaisesRegex(SchemaDriftError, "Content-Range"):
                AtlasClient(transport=transport).download(
                    "https://example.test/result.zip", destination
                )

    def test_resume_download_rejects_truncated_range_and_truncated_zip(self) -> None:
        archive = zip_payload()
        offset = 5
        transport = ScriptedTransport(
            [
                response(
                    206,
                    archive[offset : offset + 1],
                    {"content-range": f"bytes {offset}-{len(archive) - 1}/{len(archive)}"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.zip"
            Path(str(destination) + ".partial").write_bytes(archive[:offset])
            with self.assertRaises(SchemaDriftError):
                AtlasClient(transport=transport).download(
                    "https://example.test/result.zip", destination
                )
            self.assertFalse(destination.exists())

        truncated = archive[:-8]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.zip"
            with self.assertRaises(SchemaDriftError):
                AtlasClient(
                    transport=ScriptedTransport([response(200, truncated)])
                ).download("https://example.test/result.zip", destination)
            self.assertFalse(destination.exists())

    def test_download_rejects_non_zip_before_atomic_rename(self) -> None:
        transport = ScriptedTransport([response(200, b"not-a-zip")])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.zip"
            with self.assertRaisesRegex(SchemaDriftError, "zip"):
                AtlasClient(transport=transport).download(
                    "https://example.test/result.zip", destination
                )
            self.assertFalse(destination.exists())
            self.assertTrue(Path(str(destination) + ".partial").exists())

    def test_thumbnail_binary(self) -> None:
        transport = ScriptedTransport([response(200, png_payload())])
        result = AtlasClient(transport=transport).thumbnail("a" * 32, "plddt")
        self.assertTrue(result.body.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
