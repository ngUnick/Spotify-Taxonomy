# Repository instructions

These rules apply throughout this repository.

## Phase boundaries

- Stop at the phase requested by the user. Never begin a later phase implicitly.
- Phase 1 is local-only: no OAuth, network providers, live account access, real classification,
  ingestion workflows, or Spotify mutations.

## Credentials and user data

- Never expose, prompt with, log, persist in ordinary files, or commit credentials, access tokens,
  refresh tokens, client secrets, cookies, or raw authorization responses.
- Never commit real runtime catalogues, account/API dumps, playlist exports, classification batches,
  research caches, database files, or logs containing user data.
- Unit tests and documentation examples must use synthetic data. Real title/artist identity may enter
  Codex only through a deliberate runtime classification workflow in an approved later phase.
- Runtime state belongs outside the repository, under an appropriate per-user Windows application-data
  directory. Persist only fields justified by the data-retention policy.

## Architecture and provenance

- Keep canonical track identity and classification state independent of Spotify mappings.
- Never merge canonical tracks solely because normalized title and artist strings match. Preserve
  version ambiguity and require stronger evidence or user confirmation.
- Keep platform/identity provenance, external research provenance, characteristic derivation origin,
  proposal origin, and user-decision origin as separate concepts.
- The dependency direction is evidence -> derived characteristics -> playlist proposals. Derived
  characteristics must never depend on existing playlist proposals.
- Classification providers must not import or invoke Spotify mutation code. Spotify synchronization
  must not depend on Codex-specific implementation details.
- Codex outputs must be structured, compact, versioned, and reference evidence IDs. Ask for concise
  justification summaries, not extensive reasoning.
- Preserve external research provenance and cache only normalized, useful, terms-compatible material.

## Decisions and safety

- Explicit user decisions always override generated proposals. User overrides/explicit decisions outrank
  approvals, and approvals outrank rule/Codex proposals.
- SOURCE playlists are immutable. INBOX sources are read-only in V1 and processed items remain present.
- Playlist names never authorize writes. Only stable IDs explicitly registered as MANAGED may be writable,
  after live ownership validation and through the guarded write path.
- Classification never writes to Spotify. The path is approved desired state -> dry-run diff -> approval ->
  guarded addition-only execution -> verification.
- Do not add deletion, clearing, replacement, removal, reorder, source mutation, or visibility changes to V1.
- Never implement fake-human timing, anti-detection behavior, account/proxy rotation, fingerprint changes,
  CAPTCHA bypass, security-control bypass, or other enforcement evasion.

## Development and verification

- Keep ordinary unit tests offline, deterministic, and synthetic; no live service calls or credentials.
- Maintain safety-invariant tests whenever domain policies change.
- Keep dependencies limited to those approved for the active phase and keep `uv.lock` current.
- Run from the repository root:

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
```
