"""Identity-scoped, normalized external entity evidence; no matching authority."""
from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass

_MBID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

@dataclass(frozen=True, slots=True)
class IdentityExternalEntityEvidence:
    evidence_id: str; identity_id: str; provider: str; entity_type: str; external_id: str | None
    lookup_state: str; lookup_method: str; query_fingerprint: str; adapter_version: str
    retrieved_at: str; fresh_until: str | None; ambiguity_count: int | None; ambiguity_digest: str | None

def normalize_external_entity_id(provider: str, entity_type: str, value: str | None) -> str | None:
    if value is None: return None
    normalized = value.strip().lower()
    if provider == 'musicbrainz' and entity_type == 'recording' and not _MBID.fullmatch(normalized):
        raise ValueError('MusicBrainz recording ID must be a UUID')
    return normalized

class IdentityExternalEvidenceStore:
    def __init__(self, connection: sqlite3.Connection): self.connection = connection
    def upsert(self, item: IdentityExternalEntityEvidence) -> None:
        external_id = normalize_external_entity_id(item.provider, item.entity_type, item.external_id)
        self.connection.execute("""INSERT INTO identity_external_entity_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(identity_id,provider,entity_type) DO UPDATE SET evidence_id=excluded.evidence_id,external_id=excluded.external_id,lookup_state=excluded.lookup_state,lookup_method=excluded.lookup_method,query_fingerprint=excluded.query_fingerprint,adapter_version=excluded.adapter_version,retrieved_at=excluded.retrieved_at,fresh_until=excluded.fresh_until,ambiguity_count=excluded.ambiguity_count,ambiguity_digest=excluded.ambiguity_digest""", (item.evidence_id,item.identity_id,item.provider,item.entity_type,external_id,item.lookup_state,item.lookup_method,item.query_fingerprint,item.adapter_version,item.retrieved_at,item.fresh_until,item.ambiguity_count,item.ambiguity_digest))
        self.connection.commit()
    def for_identities(self, identity_ids: tuple[str, ...]) -> tuple[IdentityExternalEntityEvidence, ...]:
        if not identity_ids: return ()
        marks=','.join('?' for _ in identity_ids)
        return tuple(IdentityExternalEntityEvidence(*row) for row in self.connection.execute(f"SELECT * FROM identity_external_entity_evidence WHERE identity_id IN ({marks}) ORDER BY identity_id,provider,entity_type", identity_ids))
