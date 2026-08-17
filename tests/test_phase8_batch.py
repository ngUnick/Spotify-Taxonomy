from __future__ import annotations
import json, tempfile, unittest
from pathlib import Path
import _bootstrap  # noqa:F401
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack
from spotify_playlist_taxonomy.persistence import SQLiteStateStore,apply_migrations,connect_database,sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase8 import Phase8Service,SCHEMA_VERSION
ROOT=Path(__file__).resolve().parents[1]
class Phase8BatchTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.db=connect_database(Path(self.temp.name)/'app.db'); apply_migrations(self.db); self.bundle=load_phase4_bundle(ROOT/'config'/'application.yaml'); sync_taxonomy(self.db,self.bundle.taxonomy); SQLiteStateStore(self.db).save_canonical_track(CanonicalTrack('canonical.synthetic.batch','Synthetic Batch',('Invented',))); self.service=Phase8Service(self.db,self.bundle,Path(self.temp.name)/'outbound')
 def tearDown(self):self.db.close();self.temp.cleanup()
 def test_create_is_bounded_idempotent_and_minimized(self):
  bid,path=self.service.create(('canonical.synthetic.batch',)); bid2,path2=self.service.create(('canonical.synthetic.batch',)); self.assertEqual((bid,path),(bid2,path2)); doc=json.loads(path.read_text()); self.assertEqual(SCHEMA_VERSION,doc['schema_version']); self.assertEqual(self.bundle.taxonomy.taxonomy_version,doc['taxonomy_version']); self.assertNotIn('title',json.dumps(doc).casefold()); self.assertNotIn('spotify',json.dumps(doc).casefold()); self.assertEqual('awaiting_result',self.service.show(bid)[0]['state'])
 def test_invalid_batch_ids_fail_closed(self):
  with self.assertRaises(ValueError):self.service.create(())
  with self.assertRaises(ValueError):self.service.create(('unknown.synthetic',))
if __name__=='__main__':unittest.main()
