# Catalogue operator runbook

This runbook is for a supervised Codex session operating the local Spotify Taxonomy application. Codex is the
external operator; the application contains no Codex/OpenAI integration, daemon, scheduler, or autonomous
classifier. Runtime data, credentials, and provider responses remain outside Git.

## Fresh-session prompt

> Resume Spotify catalogue completion from the current persisted state. Follow the repository catalogue-operator
> runbook and continue until quota, an unresolved safety condition, a structural error, or completion.

After a quota cooldown, the operator may simply say: `Resume.`

## Resume means continuous completion

In this dedicated operator context, `Resume` means: resume from persisted state and **keep looping** through this
runbook until a true stop condition occurs. It is standing authorization for every routine action already permitted
here; the application's exact confirmation tokens still apply where required.

It never means one batch, one status transition, one recovery, one population run, or "make progress and report
back." A successful 250-track batch is not a completion condition. Before returning to the user, Codex must run:

```powershell
uv run --offline spotify-taxonomy catalogue operator status
```

and continue whenever `next_action` is routine executable work: `generate_worklist`, `resume_population`,
`recovery_verify`, `recovery_resolve_absent`, or a runbook-permitted managed-target provisioning step.

Codex may return only for active provider quota with a still-applicable Retry-After, an unresolved safety condition
that the supported recovery workflow cannot advance, a structural application error, actual catalogue completion, or
an independent live writer that this task cannot monitor. For `wait_for_active_writer` owned by this task, retain and
poll the mutation session until its actual subprocess exit, then rerun status; do not return while it is live.

## Single live mutation writer

Several Codex chats or shells may inspect the same runtime, but exactly one may execute a guarded catalogue or
managed-target mutation at a time. The application owns an OS-backed lock scoped to the runtime data directory for
the full preflight -> journal -> provider dispatch -> read-back -> durable-finalization sequence. `catalogue operator
status` remains local/read-only and reports `mutation_lock_state`; when it is `held`, its only safe next action is
`wait_for_active_writer`.

Always run operator status first. If another writer is live, stop and wait; do not invoke resume, apply, provisioning,
or recovery verification/resolution concurrently. After that writer exits, run status again and follow the persisted
state it reports.

When Codex returns a mutation command as a live asynchronous runner session, retain its session/PID and wait or poll
until the underlying subprocess has positively terminated. Wrapper/task completion without subprocess termination
evidence is insufficient. Never begin recovery verification while the population command that exposed that recovery
could still be running.

If a process crashes, the operating system releases its lock. That only permits a later operator to inspect persisted
state; it never proves an in-flight Spotify write failed and never authorizes a retry. Existing durable journal and
recovery procedures remain authoritative.

## Durable command receipts

For a catalogue-operator command whose result advances the state machine, prefer the local PowerShell wrapper. It
creates a unique receipt before starting one child process, captures that child's stdout and stderr, waits for its
actual termination, and records its observed exit code. Receipts are local operator artifacts under
`<RUNTIME_DATA_DIR>\operator-invocations`; they are not Git artifacts and do not contain secrets.

For example, wrap a persisted-run resume with the same runtime directory used by the child command:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/catalogue-operator-exec.ps1 `
  -DataDir <RUNTIME_DATA_DIR> -Purpose catalogue-run-resume `
  uv run spotify-taxonomy catalogue run resume <RUN_ID> --confirm RESUME:<RUN_ID> --data-dir <RUNTIME_DATA_DIR>
```

The wrapper writes `catalogue-operator-invocation-receipt.v1` metadata containing the invocation ID, sanitized command
and purpose, wrapper state (`starting`, `running`, `finished`, or `failed_to_start`), child PID, start/end timestamps,
observed exit code, and stdout/stderr paths. A fresh Codex session can inspect the latest receipt, or one exact
invocation, without a database or provider call:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/catalogue-operator-invocation-status.ps1 `
  -DataDir <RUNTIME_DATA_DIR>

powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/catalogue-operator-invocation-status.ps1 `
  -DataDir <RUNTIME_DATA_DIR> -InvocationId <INVOCATION_ID>
```

If the outer Codex command runner returns without usable output, first inspect this receipt. If the child remains live,
retain the process/session and wait or poll. If the wrapper recorded `finished`, read the captured streams and use the
recorded exit code. If the receipt is still incomplete and live-child status cannot be established, inspect the mutation
lock and `catalogue operator status`; stay fail-closed for mutations. A wrapper crash leaves the receipt incomplete:
it never fabricates termination or success. The receipt is observability only, never authority to retry a mutation or
to infer a Spotify write outcome; the existing journal, read-back, and recovery state remain authoritative.

For a local read-only/idempotent status or preview command, a retry may be considered only when the receipt establishes
that no successful completion or mutation could have occurred and the command's existing semantics permit it. Lost
outer-runner output alone never authorizes a mutation retry.

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
   resume the persisted catalogue run. If it confirms absence, run local operator status again. It will expose the
   following explicit local closure command when the exact verified-absent journal record is unambiguous:

   ```powershell
   uv run --offline spotify-taxonomy populate recovery resolve-absent <TARGET_ID> <CANONICAL_ID> RESOLVE_ABSENT:<TARGET_ID>:<CANONICAL_ID>
   ```

   This only records that the old acknowledged ambiguous addition was authoritatively absent and explicitly closed;
   it performs no provider action and never retries, deletes, or cleans up. Run operator status again afterward, then
   resume an eligible run through its separate guarded command. A quota response stops work immediately; record its
   Retry-After and run ID. For a `recovery_required` catalogue run, status scopes recovery identities to that
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

   Use `taxonomy.v2` and preserve canonical recording/version specificity. Do not create blanket 36-semantic
   abstention matrices: classify every defensible Genre/Vibe/Activity semantic, and multiple includes are valid.
   Vibe and Activity targets are unsplit. Add Greek/Foreign only for defensible language routing after a Genre is
   included; routing never determines Genre. Do not hallucinate from insufficient information; use `abstain` only
   where it is genuinely unsupported.

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

7. Run operator status again after every routine transition, including a successful batch. Do not return to the user
   after ordinary success. Continue this loop:

   ```text
   while true:
       operator status
       recovery action?      handle it; continue
       resumable run?        resume it; wait for actual subprocess exit; continue
       unreviewed work?      worklist -> recording-specific review -> import -> preview
                             -> provision one exact missing target only when safely permitted -> apply; wait; continue
       true stop condition?  return to user
   ```

   `next_action: complete` means no persisted run needs attention and no unreviewed classification work remains.
   Stop only for quota, unresolved safety/recovery, a structural error, actual completion, or an independent writer
   that cannot be monitored by this task.

## Status truthfulness

`catalogue operator status` reports only durable local facts. Confirmed and unresolved addition-attempt counts are
available for the selected run. `run_recovery_identity_count` is scoped to that run;
`other_population_recovery_count` reports separate unresolved addition records without letting them obscure an exact
current-run recovery. `already_present`, `blocked`, and `not_attempted` batch rows are deliberately not persisted, so
status labels them unavailable rather than reconstructing or guessing them from historic transcripts.
