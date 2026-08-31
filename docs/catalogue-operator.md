# Catalogue operator runbook

This runbook is for a supervised Codex session operating the local Spotify Taxonomy application. Codex is the
external operator; the application contains no Codex/OpenAI integration, daemon, scheduler, or autonomous
classifier. Runtime data, credentials, and provider responses remain outside Git.

## Fresh-session prompt

> Resume Spotify catalogue completion from the current persisted state. Follow the repository catalogue-operator
> runbook and continue until quota, an unresolved safety condition, a structural error, or completion.

After a quota cooldown, the operator may simply say: `Resume.`

## State machine

1. Start with the local-only command below. It performs no authentication, provider call, or mutation.

   ```powershell
   uv run --offline spotify-taxonomy catalogue operator status
   ```

2. `next_action: recovery_verify` has priority over every other action. Run exactly the displayed confirmation
   through the existing recovery verifier:

   ```powershell
   uv run spotify-taxonomy populate recovery verify <TARGET_ID> <CANONICAL_ID> RECOVER:<TARGET_ID>:<CANONICAL_ID>
   ```

   Never retry the ambiguous addition. If read-back confirms membership, use the existing recovery result and then
   resume the persisted catalogue run. If it confirms absence, or `next_action: blocked_manual` is shown, remain
   fail closed and use only an explicit supported recovery procedure. A quota response stops work immediately; record
its Retry-After and run ID. For a `recovery_required` catalogue run, status scopes recovery identities to that
run's persisted canonical-ID set and reports unrelated population recoveries separately. When the current run itself
has several recovery records, status deliberately exposes no guessed pair.

3. For `next_action: resume_population`, run exactly the displayed token. Do not reconstruct canonical IDs:

   ```powershell
   uv run spotify-taxonomy catalogue run resume <RUN_ID> --confirm RESUME:<RUN_ID>
   ```

   `paused_quota` is resumable after the provider cooldown. `recovery_required` is never resumed directly.

4. For `next_action: generate_worklist`, obtain at most 250 local unreviewed records:

   ```powershell
   uv run --offline spotify-taxonomy catalogue classify worklist --limit 250 --unreviewed-only
   ```

   Codex may construct a `catalogue-classification-batch.v1` file with explicit Phase 7 decisions and optional
   explicit Genre routing, then atomically import it:

   ```powershell
   uv run --offline spotify-taxonomy catalogue classify batch import <CLASSIFICATION_BATCH.json>
   ```

   Use `taxonomy.v2` and preserve canonical recording/version specificity. Vibe and Activity targets are unsplit.
   Add Greek/Foreign only for defensible language routing when a Genre is included; routing never determines Genre.
   Do not hallucinate from insufficient information. Where existing Phase 7 semantics permit, an explicit `abstain`
   is preferable to repeatedly cycling a genuinely insufficient record.

5. Preview the corresponding guarded population run:

   ```powershell
   uv run spotify-taxonomy catalogue run preview <CLASSIFICATION_BATCH.json>
   ```

   If the sole blocker is an unprovisioned configured target, use the separate exact-ID managed provisioning workflow,
   never name adoption or arbitrary playlist registration, then preview again. Stop if provisioning is ambiguous or
   recovery-required.

6. Apply only with the `catalogue_run_id` confirmation shown by the preview. It is distinct from `batch_plan_id`:

   ```powershell
   uv run spotify-taxonomy catalogue run apply <CLASSIFICATION_BATCH.json> --confirm APPLY:<CATALOGUE_RUN_ID>
   ```

   The application remains addition-only, uses exact account/provider mappings, journals before every one-track
   write, and performs mandatory provider read-back. A quota preserves confirmed work and stops. An ambiguous
   dispatched addition stops later writes and returns to step 2; it is never automatically retried or cleaned up.

7. On `next_action: complete`, no persisted run needs attention and no unreviewed classification work remains.
   Otherwise repeat this state machine. Stop only for quota, unresolved safety/recovery, a structural error, or
   completion.

## Status truthfulness

`catalogue operator status` reports only durable local facts. Confirmed and unresolved addition-attempt counts are
available for the selected run. `run_recovery_identity_count` is scoped to that run;
`other_population_recovery_count` reports separate unresolved addition records without letting them obscure an exact
current-run recovery. `already_present`, `blocked`, and `not_attempted` batch rows are deliberately not persisted, so
status labels them unavailable rather than reconstructing or guessing them from historic transcripts.
