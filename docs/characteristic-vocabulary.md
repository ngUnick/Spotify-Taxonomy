# Controlled characteristic vocabulary

Phase 4 defines the explicitly human-reviewed `characteristics.v1` in `config/characteristics.yaml`. The
vocabulary is provider-neutral and reusable: characteristics describe musical evidence or judgments,
never membership in a particular playlist.

## Families

| Family | Purpose |
| --- | --- |
| `style` | Broad musical traditions supported by independent evidence |
| `work_context` | Documented screen, stage, or game-work function |
| `version` | Recording and release-version distinctions |
| `arrangement` | Instrumentation and acoustic/electronic arrangement |
| `vocals_lyrics` | Vocal prominence/style and independently evidenced themes |
| `cultural_context` | Platform-neutral project/scene and regional-cultural association |
| `tempo_rhythm` | Measured or perceived pace, groove, drive, and continuity |
| `energy_dynamics` | Energy, force, intensity, and dynamic behavior |
| `timbre_production` | Density, brightness, distortion, bass, and percussion |
| `affect_mood` | Subjective affect and emotional qualities |
| `atmosphere_space` | Spatial and environmental character |
| `structure_attention` | Repetition, complexity, change, and attention demand |
| `functional` | Reusable context suitability, movement, focus, and fatigue risk |

The 13 families contain 58 characteristics. Each definition declares a stable ID, value type and controlled
values or numeric bounds, normalization aliases where justified, epistemic role, version sensitivity, and
permitted evidence kinds.

## Semantics and boundaries

- Absence always means unknown; it is neither positive nor negative evidence.
- Contradictory assertions remain available to downstream review instead of being silently collapsed.
- Evidence provenance is required independently of later derivation or proposal origin.
- Vocal presence, lyrical themes, and sonic affect are separate characteristics.
- `arrangement.substantive_acoustic_idiom` records an evidence-backed, version-sensitive acoustic
  performance or songwriting identity independently of acoustic/electronic balance. It prevents acoustic
  sound generation alone from becoming a Folk/Acoustic decision.
- Lyrical language profile, primary project/scene association, and regional musical association are
  separate, version-sensitive inputs to downstream Greek/Foreign routing; none establishes Genre.
- No characteristic encodes a playlist decision, provider-specific audio feature, or Spotify signal.
- Phase 4 validates definitions only. It does not derive values for real or synthetic catalogue tracks.

The strict loader rejects duplicate IDs or values, invalid family prefixes, malformed ordinal/numeric
schemas, aliases targeting unknown values, and unknown fields. Cross-document validation additionally
rejects specification operators or values that are incompatible with a characteristic's type.
