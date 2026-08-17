from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
import _bootstrap  # noqa: F401
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase6 import Phase6Service
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service

ROOT=Path(__file__).resolve().parents[1]
class Phase7ReviewTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.connection=connect_database(Path(self.temp.name)/'app.db'); apply_migrations(self.connection)
  self.bundle=load_phase4_bundle(ROOT/'config'/'application.yaml'); sync_taxonomy(self.connection,self.bundle.taxonomy)
  store=SQLiteStateStore(self.connection); store.save_canonical_track(CanonicalTrack('canonical.synthetic.review','Synthetic Signal',('Invented Act',)))
  store.save_evidence(ResearchEvidence('evidence.synthetic.review','canonical.synthetic.review',ResearchSource.WIKIDATA,EvidenceKind.SOURCED_FACT,json.dumps({'claim':'style_tradition','value':'pop'}),'https://example.invalid',.9,provider_id='wikidata',provider_version='v1',request_id='r',query_id='q',subject_fingerprint='a'*64,subject_scope='recording',subject_match='exact',evidence_type='style_tradition',claim_key='style_tradition',quality_band='high',normalized_schema_version='v1',normalizer_version='v1',retrieved_at='2026-01-01T00:00:00Z',fresh_until='2099-01-01T00:00:00Z',content_digest='b'*64))
  p6=Phase6Service(self.connection,self.bundle); p6.persist_derivations(p6.derive('canonical.synthetic.review')); p6.classify('canonical.synthetic.review'); self.service=Phase7Service(self.connection,self.bundle)
 def tearDown(self): self.connection.close(); self.temp.cleanup()
 def test_user_reject_overrides_generated_include_and_clear_reveals_it(self):
  before=self.service.effective('canonical.synthetic.review','genre.pop'); self.assertEqual('include',before.effective)
  rejected=self.service.decide('canonical.synthetic.review','genre.pop',AssignmentDecision.EXCLUDE,'synthetic review'); self.assertEqual('exclude',rejected.effective)
  cleared=self.service.clear('canonical.synthetic.review','genre.pop'); self.assertEqual('include',cleared.effective); self.assertEqual(2,len(self.service.history('canonical.synthetic.review','genre.pop')))
 def test_decision_is_idempotent_and_calibration_is_observational(self):
  self.service.decide('canonical.synthetic.review','genre.pop',AssignmentDecision.EXCLUDE)
  self.service.decide('canonical.synthetic.review','genre.pop',AssignmentDecision.EXCLUDE)
  self.assertEqual(1,len(self.service.history('canonical.synthetic.review','genre.pop')))
  row=self.service.calibration()[0]; self.assertEqual(1,row['include_rejected'])
 def test_independent_semantic_reviews_do_not_force_primary_genre(self):
  self.service.decide('canonical.synthetic.review','genre.pop',AssignmentDecision.INCLUDE)
  self.service.decide('canonical.synthetic.review','genre.rock',AssignmentDecision.EXCLUDE)
  self.assertEqual('include',self.service.effective('canonical.synthetic.review','genre.pop').effective)
  self.assertEqual('exclude',self.service.effective('canonical.synthetic.review','genre.rock').effective)
if __name__=='__main__': unittest.main()
