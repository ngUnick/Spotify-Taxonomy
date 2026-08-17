# Playlist write-safety model

## Roles are capabilities, not names

- **SOURCE**: existing reference playlist; readable when authorized, never mutable.
- **INBOX**: Liked Songs or a configured Shazam/discovery source; readable and process-tracked, never cleaned
  up in V1. Liked Songs is a library source, not an ordinary playlist.
- **MANAGED**: a playlist created or deliberately registered for this system. This role is necessary but not
  sufficient for a write.

Phase 3 registration grants read/ingestion intent only. `collaborative=true` does not prove that the current
user is a collaborator and never implies ownership or write authority. Non-owned registered playlists may be
ingested only when the read adapter can actually access their items. Account unbind is denied until every
registered ingestion source has been deliberately disabled.

Unknown/unregistered targets are read-only. Display names and taxonomy labels never confer authority. A
playlist named “Rock” is not writable unless its stable Spotify ID is explicitly registered as managed.
Likewise, future Greek/Foreign display names confer no authority. The later managed-playlist phase must bind
each registration to its stable `output_target_id`, not infer a target from its name or from the parent
semantic `logical_playlist_id` alone.

## Guarded write path

Classification has no write capability. Later writes must pass all of these independent gates:

1. Effective assignments have resolved user precedence and are approved desired state.
2. A planner with no mutation client produces an idempotent dry-run diff.
3. The user approves the specific, versioned change set.
4. The executor accepts only a signed/identified approved operation record, not arbitrary commands.
5. It resolves the stable target ID and confirms the registration is MANAGED.
6. It fetches live metadata and verifies ownership/current target existence; a stale/renamed target is
   resolved by ID, and ownership mismatch stops the operation.
7. It rechecks that the mutation kind is allowed and current state still supports the diff.
8. It performs a bounded write, honors rate limits, reconciles ambiguous failures before retrying, and
   verifies resulting membership/snapshot before marking success.

Every missing or ambiguous condition denies the write. OAuth scopes alone never establish authorization.

## V1 mutation allowlist

Only these operations may eventually be enabled:

- create a new managed playlist after preview and approval;
- add missing items to an existing, registered, owned managed playlist after preview and approval.

The following remain prohibited even if Spotify grants a scope: delete/clear a playlist, replace its entire
contents, remove/reorder items, change details/visibility, mutate source/inbox playlists, or infer ownership
from a name. Early managed playlists should default private only if the user chooses that configuration;
visibility must never be changed implicitly.

## Retry and idempotency principles

The planner compares approved desired membership with live/current membership and emits only missing items.
Operations receive stable IDs and recorded item sets. A retry checks whether the prior request may already
have succeeded before resubmitting. Item additions obey the documented 100-item maximum and use smaller
recoverable batches where appropriate. A partial success records verified items so only unresolved items
remain. Snapshot changes trigger reconciliation, not blind replay.

`429` responses honor `Retry-After`; quota exhaustion stops. Backoff includes only normal engineering jitter
where needed to prevent synchronized retries, never timing intended to disguise automation.

## Required safety tests

Phase 0 tests cover denial for SOURCE, INBOX, unknown, unregistered, unowned, unapproved, missing, and
prohibited-operation targets; the fully approved managed addition path; and absence of mutation methods on
the classifier port. Future phases must add dry-run-zero-writes, duplicate prevention, pagination, 429,
ambiguous timeout reconciliation, partial failure, stale target, snapshot, and fake-client integration tests
before the guarded executor is allowed to contact a real account.
