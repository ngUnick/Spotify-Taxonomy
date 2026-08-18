from io import BytesIO, TextIOWrapper
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import _render_cli_text, _safe_echo, app
from spotify_playlist_taxonomy.domain.ingestion import (
    CandidateReview,
    IdentityReview,
    MatchCandidate,
    MatchCandidateAction,
    MatchCandidateState,
    StagedSourceEntry,
)
from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack,
    IdentityProvider,
    PlatformTrackIdentity,
)
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class Phase3CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "runtime"
        result = RUNNER.invoke(app, [
            "db", "init", "--config", str(CONFIG), "--data-dir", str(self.data_dir)
        ])
        self.assertEqual(0, result.exit_code, result.output)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, *values: str) -> list[str]:
        return [*values, "--config", str(CONFIG), "--data-dir", str(self.data_dir)]

    def test_phase3_command_groups_are_exposed_without_network(self) -> None:
        spotify = RUNNER.invoke(app, ["spotify", "--help"])
        catalogue = RUNNER.invoke(app, ["catalogue", "--help"])
        self.assertEqual(0, spotify.exit_code, spotify.output)
        self.assertEqual(0, catalogue.exit_code, catalogue.output)
        for command in ("account", "source", "ingest"):
            self.assertIn(command, spotify.output)
        self.assertIn("match", catalogue.output)
        self.assertIn("link", catalogue.output)

    def test_local_status_and_review_commands_do_not_need_client_id(self) -> None:
        database = self.data_dir / "state" / "app.db"
        connection = connect_database(database)
        apply_migrations(connection)
        Phase3Store(connection).bind_account(
            "binding-review", "synthetic-review-account", "synthetic-review-user"
        )
        connection.close()
        account = RUNNER.invoke(app, self.args("spotify", "account", "status"))
        sources = RUNNER.invoke(app, self.args("spotify", "source", "list"))
        scans = RUNNER.invoke(app, self.args("spotify", "ingest", "status"))
        catalogue = RUNNER.invoke(app, self.args("catalogue", "status"))
        matches = RUNNER.invoke(app, self.args("catalogue", "match", "list"))
        links = RUNNER.invoke(app, self.args("catalogue", "link", "list"))
        self.assertEqual(0, account.exit_code, account.output)
        self.assertEqual(0, sources.exit_code, sources.output)
        self.assertEqual(0, scans.exit_code, scans.output)
        self.assertEqual(0, catalogue.exit_code, catalogue.output)
        self.assertEqual(0, matches.exit_code, matches.output)
        self.assertEqual(0, links.exit_code, links.output)
        self.assertIn("bound: true", account.output)
        self.assertIn("published_identities: 0", catalogue.output)

    def test_account_status_masks_persisted_identifiers(self) -> None:
        database = self.data_dir / "state" / "app.db"
        connection = connect_database(database)
        apply_migrations(connection)
        Phase3Store(connection).bind_account(
            "binding-synthetic", "synthetic-account-identifier", "synthetic-user-identifier"
        )
        connection.close()
        result = RUNNER.invoke(app, self.args("spotify", "account", "status"))
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("bound: true", result.output)
        self.assertNotIn("synthetic-account-identifier", result.output)
        self.assertNotIn("synthetic-user-identifier", result.output)

    def test_match_show_renders_link_existing_canonical_display_fields(self) -> None:
        identity = PlatformTrackIdentity(
            identity_id="spotify.synthetic-review",
            provider=IdentityProvider.SPOTIFY,
            provider_track_id="synthetic-provider-track",
            title="Synthetic Café 東京",
            artists=("Synthetic Artist Ω", "Second Artist"),
            version_label="acoustic",
        )
        candidate = MatchCandidate(
            candidate_id="candidate.synthetic-link",
            account_binding_id="binding-synthetic",
            identity_id=identity.identity_id,
            canonical_track_id="canonical.synthetic",
            action=MatchCandidateAction.LINK_EXISTING,
            state=MatchCandidateState.AMBIGUOUS,
            confidence=0.5,
            basis=("normalized_title_artist_match_only",),
            matcher_version="synthetic.v1",
        )
        canonical = CanonicalTrack(
            canonical_track_id="canonical.synthetic",
            display_title="Canonical Café 東京",
            display_artists=("Canonical Artist Ω", "Second Artist"),
        )
        peer = MatchCandidate(
            candidate_id="candidate.synthetic-create",
            account_binding_id="binding-synthetic",
            identity_id=identity.identity_id,
            canonical_track_id=None,
            action=MatchCandidateAction.CREATE_NEW,
            state=MatchCandidateState.USER_CONFIRMATION_REQUIRED,
            confidence=0.0,
            basis=("preserve_distinct_recording_option",),
            matcher_version="synthetic.v1",
        )
        with patch("spotify_playlist_taxonomy.cli._phase3_review") as review_factory:
            review_factory.return_value.candidate.return_value = CandidateReview(
                candidate, identity, canonical
            )
            review_factory.return_value.identity.return_value = IdentityReview(
                identity, None, (candidate, peer)
            )
            review_factory.return_value.linked_identity_ids_for_canonical.return_value = (
                "identity.synthetic.a", "identity.synthetic.b"
            )
            result = RUNNER.invoke(
                app,
                self.args("catalogue", "match", "show", candidate.candidate_id),
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("state: ambiguous", result.output)
        self.assertIn("identity_id: spotify.synthetic-review", result.output)
        self.assertIn("action: link_existing", result.output)
        self.assertIn("identity: Synthetic Café 東京 — Synthetic Artist Ω, Second Artist", result.output)
        self.assertIn("identity_version: acoustic", result.output)
        self.assertIn("canonical: Canonical Café 東京 — Canonical Artist Ω, Second Artist", result.output)
        self.assertIn("basis: normalized_title_artist_match_only", result.output)
        self.assertIn(
            "canonical_linked_identities: identity.synthetic.a, identity.synthetic.b",
            result.output,
        )
        self.assertIn(
            "open_peer: candidate.synthetic-create\taction=create_new\t"
            "state=user_confirmation_required\tcanonical=-",
            result.output,
        )
        self.assertNotIn("synthetic-provider-track", result.output)
        self.assertNotIn("binding-synthetic", result.output)

    def test_match_show_create_new_still_renders_without_canonical(self) -> None:
        identity = PlatformTrackIdentity(
            identity_id="spotify.synthetic-create",
            provider=IdentityProvider.SPOTIFY,
            provider_track_id="synthetic-create-track",
            title="Synthetic New Recording",
            artists=("Synthetic Artist",),
        )
        candidate = MatchCandidate(
            candidate_id="candidate.synthetic-create",
            account_binding_id="binding-synthetic",
            identity_id=identity.identity_id,
            canonical_track_id=None,
            action=MatchCandidateAction.CREATE_NEW,
            state=MatchCandidateState.USER_CONFIRMATION_REQUIRED,
            confidence=0.0,
            basis=("preserve_distinct_recording_option",),
            matcher_version="synthetic.v1",
        )
        with patch("spotify_playlist_taxonomy.cli._phase3_review") as review_factory:
            review_factory.return_value.candidate.return_value = CandidateReview(
                candidate, identity, None
            )
            review_factory.return_value.identity.return_value = IdentityReview(
                identity, None, (candidate,)
            )
            result = RUNNER.invoke(
                app,
                self.args("catalogue", "match", "show", candidate.candidate_id),
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("state: user_confirmation_required", result.output)
        self.assertIn("identity_id: spotify.synthetic-create", result.output)
        self.assertIn("action: create_new", result.output)
        self.assertIn("identity: Synthetic New Recording — Synthetic Artist", result.output)
        self.assertIn("identity_version: -", result.output)
        self.assertIn("canonical: -", result.output)
        self.assertIn("basis: preserve_distinct_recording_option", result.output)
        self.assertNotIn("synthetic-create-track", result.output)
        self.assertNotIn("binding-synthetic", result.output)
        self.assertNotIn("canonical_linked_identities:", result.output)

    def test_match_show_loads_open_peers_before_its_store_closes(self) -> None:
        database = self.data_dir / "state" / "app.db"
        connection = connect_database(database)
        apply_migrations(connection)
        store = Phase3Store(connection)
        binding = store.bind_account(
            "binding.synthetic", "account.synthetic", "user.synthetic"
        )
        source = store.register_liked_source("source.synthetic", binding.binding_id)
        identity = PlatformTrackIdentity(
            identity_id="identity.synthetic-pair",
            provider=IdentityProvider.SPOTIFY,
            provider_track_id="provider.synthetic-pair",
            title="Synthetic Pair",
            artists=("Synthetic Artist",),
        )
        store.start_scan(
            scan_id="scan.synthetic-pair", source_id=source.source_id,
            binding_id=binding.binding_id, starting_snapshot_id=None,
            starting_total=1, initial_page_signature=None, digest_version="synthetic.v1",
        )
        store.stage_page(
            scan_id="scan.synthetic-pair", binding_id=binding.binding_id,
            entries=(StagedSourceEntry(0, 0, identity),), next_cursor=None, next_offset=1,
        )
        store.publish_scan("scan.synthetic-pair", binding.binding_id, None)
        connection.execute(
            "INSERT INTO canonical_tracks VALUES ('canonical.synthetic-pair', 'Synthetic Pair', '2026-01-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO canonical_track_artists VALUES ('canonical.synthetic-pair', 0, 'Synthetic Artist')"
        )
        connection.commit()
        store.reconcile_published_identity(
            binding.binding_id, identity.identity_id, matcher_version="synthetic.v1",
            candidate_id="candidate.synthetic-link", canonical_id="canonical.unused",
            link_id="link.unused", event_id="event.unused",
        )
        for ordinal, identity_id in enumerate((
            "identity.synthetic-linked-b", "identity.synthetic-linked-a"
        )):
            linked_identity = PlatformTrackIdentity(
                identity_id=identity_id,
                provider=IdentityProvider.SPOTIFY,
                provider_track_id=f"provider.synthetic-linked-{ordinal}",
                title="Synthetic Pair",
                artists=("Synthetic Artist",),
            )
            scan_id = f"scan.synthetic-linked-{ordinal}"
            store.start_scan(
                scan_id=scan_id, source_id=source.source_id,
                binding_id=binding.binding_id, starting_snapshot_id=None,
                starting_total=1, initial_page_signature=None, digest_version="synthetic.v1",
            )
            store.stage_page(
                scan_id=scan_id, binding_id=binding.binding_id,
                entries=(StagedSourceEntry(0, 0, linked_identity),),
                next_cursor=None, next_offset=1,
            )
            store.publish_scan(scan_id, binding.binding_id, None)
            store.reconcile_published_identity(
                binding.binding_id, linked_identity.identity_id,
                matcher_version="synthetic.v1", candidate_id=f"candidate.linked-{ordinal}",
                canonical_id=f"canonical.unused-{ordinal}", link_id=f"link.unused-{ordinal}",
                event_id=f"event.unused-{ordinal}",
            )
            linked_candidate = next(
                item for item in store.candidates(binding.binding_id)
                if item.identity_id == linked_identity.identity_id
                and item.action is MatchCandidateAction.LINK_EXISTING
            )
            store.confirm_candidate(
                binding.binding_id, linked_candidate.candidate_id,
                canonical_id="canonical.synthetic-pair", create_new=False,
                link_id=f"link.synthetic-{ordinal}", event_id=f"event.synthetic-{ordinal}",
            )
        link = next(
            candidate for candidate in store.candidates(binding.binding_id)
            if candidate.action is MatchCandidateAction.LINK_EXISTING
        )
        create_new = next(
            candidate for candidate in store.candidates(binding.binding_id)
            if candidate.action is MatchCandidateAction.CREATE_NEW
        )
        connection.close()

        result = RUNNER.invoke(
            app, self.args("catalogue", "match", "show", link.candidate_id)
        )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("candidate_id: candidate.synthetic-link", result.output)
        self.assertIn("identity_id: identity.synthetic-pair", result.output)
        self.assertIn("action: link_existing", result.output)
        self.assertIn(
            "canonical_linked_identities: identity.synthetic-linked-a, "
            "identity.synthetic-linked-b",
            result.output,
        )
        self.assertIn(
            f"open_peer: {create_new.candidate_id}\taction=create_new\t"
            "state=user_confirmation_required\tcanonical=-",
            result.output,
        )

    def test_match_list_passes_bounded_state_filtered_limit_and_rejects_invalid_limit(self) -> None:
        with patch("spotify_playlist_taxonomy.cli._phase3_review") as review_factory:
            review_factory.return_value.candidates.return_value = ()
            result = RUNNER.invoke(
                app,
                self.args("catalogue", "match", "list", "--state", "ambiguous", "--limit", "25"),
            )
        self.assertEqual(0, result.exit_code, result.output)
        review_factory.return_value.candidates.assert_called_once_with(
            MatchCandidateState.AMBIGUOUS, 25
        )
        invalid = RUNNER.invoke(
            app, self.args("catalogue", "match", "list", "--limit", "0")
        )
        self.assertNotEqual(0, invalid.exit_code)

    def test_unicode_metadata_renders_unchanged_on_utf8_output(self) -> None:
        original = "Synthetic 💯 — Ελληνικά — Café — 東京"
        output = BytesIO()
        stream = TextIOWrapper(output, encoding="utf-8", errors="strict")
        try:
            _safe_echo(original, file=stream)
            stream.flush()
            rendered = output.getvalue().decode("utf-8")
        finally:
            stream.detach()
        self.assertEqual([original], rendered.splitlines())
        self.assertEqual("Synthetic 💯 — Ελληνικά — Café — 東京", original)

    def test_restrictive_windows_output_uses_readable_deterministic_fallback(self) -> None:
        original = "Synthetic 💯 — Ελληνικά — Café — 東京"
        output = BytesIO()
        stream = TextIOWrapper(output, encoding="cp1252", errors="strict")
        try:
            _safe_echo(original, file=stream)
            stream.flush()
            rendered = output.getvalue().decode("cp1252")
        finally:
            stream.detach()
        expected = _render_cli_text(original, "cp1252")
        self.assertEqual([expected], rendered.splitlines())
        self.assertIn(r"\U0001f4af", rendered)
        self.assertIn(r"\u0395", rendered)
        self.assertIn("Café", rendered)
        self.assertIn(r"\u6771\u4eac", rendered)
        self.assertEqual("Synthetic 💯 — Ελληνικά — Café — 東京", original)


if __name__ == "__main__":
    unittest.main()
