# Data retention and provenance policy

Status: mandatory Phase 0 baseline. Runtime schema and exact time-to-live values are Phase 1 decisions.
Data minimization is field-by-field: every persisted field needs an operational, audit, matching, review,
or recovery purpose.

## Storage locations

Source code and synthetic fixtures live in Git. Real runtime data lives outside the repository under an
appropriate `%LOCALAPPDATA%` project directory, with user-only filesystem permissions where supported.
Credentials live in protected credential storage, separately from SQLite. Backups containing user data are
explicit, local, protected, and never committed.

## Permitted durable data

| Category | Minimum useful fields / reason | Retention principle |
| --- | --- | --- |
| Canonical track | local ID, display title/artists, ambiguity state | while referenced; needed for portable classification |
| Identity mapping | provider, provider track ID, minimal title/artists, match state/confidence/basis | while syncing/matching; preserve distinct versions |
| Playlist registration | logical ID, stable platform ID, role, ownership validation state | while configured; authorizes/guards sync |
| Research evidence | normalized fact/tag/short description, source/kind, reference ID/URL, confidence, retrieval/freshness status | while useful and permitted; refresh/expire by provider policy |
| Derived characteristic | value, origin, evidence IDs, confidence, version, concise summary | while referenced; supports reproducibility |
| Batch/item state | input fingerprint, schema/taxonomy/spec versions, status, attempts, compact error category | until completion plus practical audit window |
| Proposal/history | decision, origin, confidence, references, versions, review state | retain superseded records for audit/reclassification |
| User decision | include/exclude, origin, sequence/version, active/superseded state | durable until user changes/deletes it |
| Calibration selection | explicitly selected example and polarity | until deselected or taxonomy retired |
| Sync operation | target IDs, requested/verified item IDs, approval, outcome, snapshots, retry state | practical audit/recovery window |

## Prohibited durable data

Do not persist raw Spotify response archives, access/refresh tokens, client secrets, unnecessary account or
profile data, artwork, audio, previews, audio analysis/features, listening-history profiles, Spotify
recommendations/genre inference, popularity, arbitrary album metadata, copied webpages, large review
passages, hidden model reasoning, or arbitrary debug dumps. Logs avoid title/artist by default and use local
IDs/counts; diagnostic disclosure must be deliberate and temporary.

Real Spotify identities may be processed by Codex only in the deliberate runtime classification workflow.
They must not become repository fixtures or documentation examples. Tests use invented people, tracks,
platform IDs, evidence, and playlists.

## Provenance rules

Provenance domains must not be collapsed:

1. **Identity provenance** links an identity/mapping to Spotify, MusicBrainz, user import/entry, or another
   platform.
2. **Research provenance** records external source type, evidence kind, source record/URL where useful,
   retrieval time/freshness, and normalized content.
3. **Derivation origin** records whether a characteristic came from a deterministic rule, Codex
   interpretation, or explicit user assertion, plus evidence references and derivation version.
4. **Proposal origin** records which classifier/rule proposed a playlist decision, plus evidence and
   characteristic references.
5. **Decision origin** records approval, override, explicit user decision, or operational state.

Community tags, sourced facts, editorial descriptions, inferences, and subjective judgments remain visibly
different. Never label an inference as a sourced fact. Store a short normalized assertion and attribution,
not a copied page.

## Lifecycle and user control

Phase 1 must make backup/restore and deletion practical. A user can invalidate research, supersede decisions,
deselect calibration examples, remove provider mappings, and purge runtime state. Deletion must preserve only
the minimal tombstone/idempotency marker justified by safety, if any. Provider-specific terms may require
shorter retention and take precedence. Cached evidence includes freshness/status so stale data can be
refreshed without repeatedly querying every track.

Errors and telemetry are local by default. No cloud analytics are planned for V1. Secret values are redacted
before error persistence or display.
