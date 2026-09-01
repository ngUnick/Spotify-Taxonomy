from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import RoutingOutcome, load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, CanonicalTrack
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, current_schema_version, sync_taxonomy
from spotify_playlist_taxonomy.persistence.database import load_migrations, utc_now
from spotify_playlist_taxonomy.workflows.genre_routing import (
    ROUTING_BATCH_MAX_DECISIONS,
    ROUTING_BATCH_SCHEMA_VERSION,
    GenreRoutingBatchValidationError,
    GenreRoutingService,
    parse_genre_routing_batch,
)
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase9 import Phase9Service


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class GenreRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        self.assertEqual(21, apply_migrations(self.connection))
        self.bundle = load_phase4_bundle(CONFIG)
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.first = "canonical.synthetic.routing.first"
        self.second = "canonical.synthetic.routing.second"
        store = SQLiteStateStore(self.connection)
        for canonical_id in (self.first, self.second):
            store.save_canonical_track(CanonicalTrack(canonical_id, "Synthetic", ("Invented",)))
        self.routing = GenreRoutingService(self.connection, self.bundle)
        self.review = Phase7Service(self.connection, self.bundle)
        self.phase9 = Phase9Service(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _document(self, *decisions: dict[str, object]) -> dict[str, object]:
        return {"schema": ROUTING_BATCH_SCHEMA_VERSION, "decisions": list(decisions)}

    def _count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM genre_routing_events").fetchone()[0]

    def _derived_language(self, value: str) -> None:
        self.connection.execute(
            "INSERT INTO phase6_characteristic_derivations VALUES (?, ?, 'vocals_lyrics.lyrical_language_profile', ?, 'enum', 'deterministic_rule', .9, 'synthetic.v1', ?, ?, ?, 'current', 'synthetic', '2026-01-01T00:00:00Z')",
            ("derivation.synthetic.routing", self.first, json.dumps(value), self.bundle.vocabulary.vocabulary_version, self.bundle.digest, "input.synthetic.routing"),
        )
        self.connection.commit()

    def test_schema_nineteen_migrates_an_existing_schema_eighteen_database(self) -> None:
        path = Path(self.temp.name) / "schema-eighteen.db"
        connection = connect_database(path)
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for migration in load_migrations()[:18]:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utc_now()),
            )
        connection.commit()
        self.assertEqual(18, current_schema_version(connection))
        self.assertEqual(21, apply_migrations(connection))
        self.assertIn(
            "genre_routing_events",
            {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")},
        )
        connection.close()

    def test_valid_greek_and_foreign_imports_are_canonical_specific_and_append_only(self) -> None:
        result = self.routing.apply_batch(parse_genre_routing_batch(self._document(
            {"canonical_id": self.first, "route": "greek", "note": "synthetic Greek route"},
            {"canonical_id": self.second, "route": "foreign"},
        )))
        self.assertEqual((2, 2, 1, 1), (result.decision_count, result.canonical_track_count, result.greek_count, result.foreign_count))
        self.assertEqual(RoutingOutcome.GREEK, self.routing.show(self.first).route)
        self.assertEqual(RoutingOutcome.FOREIGN, self.routing.show(self.second).route)
        self.routing.apply_batch(parse_genre_routing_batch(self._document(
            {"canonical_id": self.first, "route": "foreign"},
        )))
        self.assertEqual(3, self._count())
        self.assertEqual(RoutingOutcome.FOREIGN, self.routing.show(self.first).route)

    def test_strict_input_validation_occurs_before_any_write(self) -> None:
        invalid = (
            self._document({"canonical_id": self.first, "route": "other"}),
            self._document({"canonical_id": self.first, "route": "greek"}, {"canonical_id": self.first, "route": "foreign"}),
            self._document({"canonical_id": "canonical.synthetic.missing", "route": "greek"}),
            {"schema": "genre-routing-batch.v0", "decisions": []},
            {"schema": ROUTING_BATCH_SCHEMA_VERSION, "decisions": [{"canonical_id": self.first, "route": "greek", "unexpected": "x"}]},
        )
        for document in invalid:
            with self.subTest(document=document):
                before = self._count()
                with self.assertRaises((GenreRoutingBatchValidationError, ValueError)):
                    self.routing.apply_batch(parse_genre_routing_batch(document))
                self.assertEqual(before, self._count())
        accepted = self._document(*[
            {"canonical_id": f"canonical.synthetic.{index}", "route": "greek"}
            for index in range(ROUTING_BATCH_MAX_DECISIONS)
        ])
        self.assertEqual(ROUTING_BATCH_MAX_DECISIONS, len(parse_genre_routing_batch(accepted)))
        with self.assertRaises(GenreRoutingBatchValidationError):
            parse_genre_routing_batch(self._document(*[
                {"canonical_id": f"canonical.synthetic.{index}", "route": "greek"}
                for index in range(ROUTING_BATCH_MAX_DECISIONS + 1)
            ]))

    def test_database_failure_rolls_back_entire_batch(self) -> None:
        self.connection.execute(
            "CREATE TRIGGER synthetic_routing_failure BEFORE INSERT ON genre_routing_events "
            "WHEN NEW.canonical_track_id='canonical.synthetic.routing.second' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic routing failure'); END"
        )
        self.connection.commit()
        decisions = parse_genre_routing_batch(self._document(
            {"canonical_id": self.first, "route": "greek"},
            {"canonical_id": self.second, "route": "foreign"},
        ))
        with self.assertRaisesRegex(ValueError, "could not be applied"):
            self.routing.apply_batch(decisions)
        self.assertEqual(0, self._count())

    def test_explicit_route_requires_genre_include_and_outranks_derived_route(self) -> None:
        self.routing.apply_batch(parse_genre_routing_batch(self._document(
            {"canonical_id": self.first, "route": "foreign"},
        )))
        self.assertEqual((), self.phase9.desired(self.first).memberships)
        self._derived_language("greek")
        self.review.decide(self.first, "genre.pop", AssignmentDecision.INCLUDE)
        state = self.phase9.desired(self.first)
        self.assertEqual(("output.genre.pop.foreign",), tuple(item.output_target_id for item in state.memberships))
        self.assertEqual("explicit human genre routing: foreign", state.memberships[0].reason)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM phase7_review_events WHERE logical_playlist_id!='genre.pop'").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM phase8_codex_proposals").fetchone()[0])

    def test_derived_fallback_and_unresolved_behavior_are_preserved_without_explicit_route(self) -> None:
        self.review.decide(self.first, "genre.pop", AssignmentDecision.INCLUDE)
        self._derived_language("greek")
        self.assertEqual("output.genre.pop.greek", self.phase9.desired(self.first).memberships[0].output_target_id)
        self.connection.execute("DELETE FROM phase6_characteristic_derivations")
        self.connection.commit()
        state = self.phase9.desired(self.first)
        self.assertEqual((), state.memberships)
        self.assertEqual("genre.pop", state.unresolved[0].semantic_specification_id)

    def test_stale_explicit_route_is_ignored_and_cli_is_local(self) -> None:
        self.connection.execute(
            "INSERT INTO genre_routing_events VALUES ('genre-routing.synthetic.stale', ?, 'foreign', 'user_explicit', 'genre-variant-routing.v0', 'stale-digest', NULL, '2026-01-01T00:00:00Z')",
            (self.first,),
        )
        self.connection.commit()
        self._derived_language("greek")
        self.review.decide(self.first, "genre.pop", AssignmentDecision.INCLUDE)
        self.assertEqual("stale", self.routing.show(self.first).state)
        self.assertEqual("output.genre.pop.greek", self.phase9.desired(self.first).memberships[0].output_target_id)
        data_dir = Path(self.temp.name) / "runtime"
        (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db")
        self.connection.backup(target)
        target.close()
        batch = Path(self.temp.name) / "routing.json"
        batch.write_text(json.dumps(self._document({"canonical_id": self.second, "route": "foreign"})), encoding="utf-8")
        result = CliRunner().invoke(app, ["routing", "batch", "import", str(batch), "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("status: applied", result.output)
        shown = CliRunner().invoke(app, ["routing", "show", self.second, "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, shown.exit_code, shown.output)
        self.assertIn("explicit_route: foreign", shown.output)


if __name__ == "__main__":
    unittest.main()
