# Project handoff

## Current repository state

- Repository: `C:\Users\USER\Desktop\Projects\Spotify-Taxonomy`
- Development branch: `dev` at `7fda6777ae2d456fa501cd224f10297e6915d6ee`
- `main` and `origin/main`: `80d6b8fcc61e7e9972d9da7a5185a2ab8a8c3236`
- `dev` is one fast-forward commit ahead of `main`; the working tree is clean.
- Schema: 19
- Taxonomy: `taxonomy.v2`, with 36 reviewed semantic specifications and 51 concrete output targets.

## Implemented operational path

Phases 0–12 / V1 are implemented. The repository maintains account-scoped source ingestion and portable
canonical identity, conservative matching with explicit human confirmation, bounded evidence and research
workflows, deterministic derivation/classification, Phase 7 effective review state, supervised Phase 8 file
exchange, Phase 9 desired state, managed-target provisioning, and guarded one-track population.

V1 end-to-end operation has been demonstrated under supervision: a human explicit include creates desired
membership, an exact managed target is verified, and guarded population adds one canonical track at a time
with provider read-back. Batch 01 completed safe memberships while ambiguous provider mappings remained
blocked. Batch 02 added explicit Foreign routing for included Genre semantics and populated only safe
memberships. Managed Genre, Vibe, and Activity targets have been provisioned as needed.

## Latest features

- `spotify-taxonomy review batch import <FILE>` imports 1–250 fully validated, atomic Phase 7 explicit human
  decisions in `phase7-review-batch.v1` format. It uses the same review-event, precedence, history, and
  idempotence rules as individual review decisions.
- `spotify-taxonomy routing batch import <FILE>` imports 1–250 fully validated, atomic explicit `greek` or
  `foreign` Genre-routing decisions in `genre-routing-batch.v1` format. `spotify-taxonomy routing show
  <CANONICAL_ID>` reports its local current state.
- Explicit Genre routing is canonical-recording scoped and takes precedence over current derived routing.
  It is separate from semantic classification: it is consulted only after an effective included `genre.*`
  decision and cannot create Genre membership by itself.
- `catalogue classify worklist --limit 1..100 --unreviewed-only` is a local account-scoped selection surface.
  `populate batch preview CANONICAL_ID...` and `populate batch apply CANONICAL_ID... --confirm APPLY:PLAN_ID`
  make a bounded up-to-100-canonical population run practical while retaining the existing per-track attempt,
  provider read-back, recovery, and fail-closed mapping guarantees.

## Current execution direction

Continue with small human-reviewed batches, inspect effective/desired state, and populate only one verified
target + one canonical at a time. Do not make broad external-provider research the default scaling strategy,
and do not relax canonical matching safety. Unresolved or ambiguous Spotify mappings remain fail closed.

`sync plan` is a local dry-run surface and does not currently have authoritative managed-membership snapshots.
It is therefore not required before population. `populate preview` is the live, exact-ID authoritative
preflight for a selected population operation; `populate apply` remains explicitly confirmed and addition-only.

## Continuing safety boundary

SOURCE and INBOX inputs are immutable. Playlist names never confer authority. Only exact, durably registered,
policy-confirmed MANAGED targets may be written. There is no removal, replacement, reordering, bulk,
background, or automatic matching/population path. Runtime databases, credentials, provider responses,
exports, logs, and real music/account metadata remain outside Git.

## Next repository action

The schema-19 explicit Genre-routing commit is clean on `dev` and may be fast-forwarded to `main` only with
explicit authorization. Real routing imports and guarded population remain separate supervised operator
checkpoints.
