import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack,
    IdentityProvider,
    MatchState,
    PlatformTrackIdentity,
)
from spotify_playlist_taxonomy.domain.policies import assess_track_match


class CanonicalMatchingInvariantTests(unittest.TestCase):
    def test_identical_normalized_title_artist_does_not_force_canonical_identity(self) -> None:
        studio = CanonicalTrack(
            "track.synthetic.studio", "Neon Orbit", ("The Invented Signals",)
        )
        live = CanonicalTrack(
            "track.synthetic.live", "Neon Orbit", ("The Invented Signals",)
        )
        left = PlatformTrackIdentity(
            "identity.synthetic.left",
            IdentityProvider.SPOTIFY,
            "  NEON—ORBIT ",
            ("The Invented Signals",),
            provider_track_id="spotify.synthetic.001",
        )
        right = PlatformTrackIdentity(
            "identity.synthetic.right",
            IdentityProvider.USER_IMPORT,
            "Neon Orbit",
            ("the invented signals",),
        )

        result = assess_track_match(left, right)

        self.assertNotEqual(studio.canonical_track_id, live.canonical_track_id)
        self.assertEqual(MatchState.AMBIGUOUS, result.state)
        self.assertLess(result.confidence, 1.0)
        self.assertIn("normalized_title_artist_match_only", result.basis)

    def test_conflicting_version_labels_require_confirmation(self) -> None:
        left = PlatformTrackIdentity(
            "identity.synthetic.clean",
            IdentityProvider.USER_IMPORT,
            "Paper Satellites",
            ("Example Avenue",),
            version_label="Clean",
        )
        right = PlatformTrackIdentity(
            "identity.synthetic.explicit",
            IdentityProvider.OTHER_PLATFORM,
            "Paper Satellites",
            ("Example Avenue",),
            version_label="Explicit",
        )

        self.assertEqual(
            MatchState.USER_CONFIRMATION_REQUIRED,
            assess_track_match(left, right).state,
        )

    def test_same_provider_stable_id_is_strong_identity_evidence(self) -> None:
        left = PlatformTrackIdentity(
            "identity.synthetic.a",
            IdentityProvider.SPOTIFY,
            "Synthetic Song",
            ("Invented Artist",),
            provider_track_id="spotify.synthetic.same",
        )
        right = PlatformTrackIdentity(
            "identity.synthetic.b",
            IdentityProvider.SPOTIFY,
            "Synthetic Song (Remastered)",
            ("Invented Artist",),
            provider_track_id="spotify.synthetic.same",
        )

        self.assertEqual(MatchState.CONFIRMED, assess_track_match(left, right).state)


if __name__ == "__main__":
    unittest.main()
