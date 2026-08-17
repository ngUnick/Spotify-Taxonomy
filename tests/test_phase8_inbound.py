from __future__ import annotations

import copy
import unittest

import _bootstrap  # noqa:F401

from spotify_playlist_taxonomy.workflows.phase8_inbound import (
    RESULT_SCHEMA_VERSION,
    InboundResultValidationError,
    OutboundBatchContext,
    parse_inbound_result,
    validate_inbound_result,
)


class Phase8InboundValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.outbound = {
            "batch_id": "batch.synthetic",
            "taxonomy_version": "taxonomy.v2",
            "bundle_digest": "bundle.synthetic",
            "items": [{
                "item_id": "item.001",
                "canonical_ref": "canonical.synthetic",
                "evidence": [{"evidence_id": "evidence.001", "evidence_type": "synthetic", "value": "synthetic"}],
                "characteristics": [{"characteristic_id": "characteristic.001", "name": "synthetic", "value": "synthetic"}],
                "deterministic_proposals": [],
            }],
            "specifications": [{"logical_playlist_id": "genre.pop", "specification_version": "spec.genre.pop.v1"}],
        }
        self.context = OutboundBatchContext.from_phase8a_export(
            batch_metadata={
                "batch_id": "batch.synthetic",
                "outbound_digest": "outbound.synthetic",
                "taxonomy_version": "taxonomy.v2",
                "bundle_digest": "bundle.synthetic",
            },
            outbound_document=self.outbound,
            output_target_ids=frozenset({"output.genre.pop.greek"}),
        )

    def _result(self) -> dict[str, object]:
        return {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "batch_id": "batch.synthetic",
            "outbound_batch_digest": "outbound.synthetic",
            "taxonomy_version": "taxonomy.v2",
            "specification_bundle_digest": "bundle.synthetic",
            "items": [{
                "item_id": "item.001",
                "classifications": [{
                    "semantic_specification_id": "genre.pop",
                    "decision": "include",
                    "justification_summary": "Synthetic evidence supports the semantic classification.",
                    "evidence_ids": ["evidence.001"],
                    "characteristic_ids": ["characteristic.001"],
                }],
            }],
        }

    def _assert_rejected(self, result: dict[str, object]) -> None:
        with self.assertRaises(InboundResultValidationError):
            validate_inbound_result(parse_inbound_result(result), self.context)

    def test_valid_synthetic_result_passes_without_context_mutation(self) -> None:
        before = self.context
        validated = validate_inbound_result(parse_inbound_result(self._result()), self.context)
        self.assertEqual("batch.synthetic", validated.batch_id)
        self.assertIs(before, self.context)

    def test_binding_and_item_failures_are_rejected(self) -> None:
        for field, value in (
            ("batch_id", "batch.other"),
            ("outbound_batch_digest", "outbound.other"),
            ("taxonomy_version", "taxonomy.other"),
            ("specification_bundle_digest", "bundle.other"),
        ):
            with self.subTest(field=field):
                result = self._result()
                result[field] = value
                self._assert_rejected(result)
        result = self._result()
        result["items"][0]["item_id"] = "item.unknown"
        self._assert_rejected(result)
        result = self._result()
        result["items"].append(copy.deepcopy(result["items"][0]))
        self._assert_rejected(result)

    def test_semantic_and_reference_failures_are_rejected(self) -> None:
        cases = (
            ("semantic_specification_id", "genre.unknown"),
            ("semantic_specification_id", "output.genre.pop.greek"),
            ("evidence_ids", ["evidence.unknown"]),
            ("characteristic_ids", ["characteristic.unknown"]),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                result = self._result()
                result["items"][0]["classifications"][0][field] = value
                self._assert_rejected(result)
        result = self._result()
        result["items"][0]["classifications"].append(copy.deepcopy(result["items"][0]["classifications"][0]))
        self._assert_rejected(result)

    def test_schema_policy_failures_are_rejected(self) -> None:
        cases = (
            ("decision", "unsupported"),
            ("justification_summary", "x" * 281),
            ("reasoning", "hidden reasoning"),
            ("user_override", "include"),
            ("spotify_playlist_id", "spotify.synthetic"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                result = self._result()
                result["items"][0]["classifications"][0][field] = value
                self._assert_rejected(result)
        result = self._result()
        result["unexpected"] = "extra"
        self._assert_rejected(result)


if __name__ == "__main__":
    unittest.main()
