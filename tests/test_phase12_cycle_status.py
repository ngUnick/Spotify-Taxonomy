from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
import _bootstrap  # noqa:F401
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.domain.ingestion import ScanStatus
from spotify_playlist_taxonomy.workflows.phase12 import Phase12Service

ROOT=Path(__file__).resolve().parents[1]

class CycleStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.connection=connect_database(Path(self.temp.name)/"app.db"); apply_migrations(self.connection); self.bundle=load_phase4_bundle(ROOT/"config"/"application.yaml"); sync_taxonomy(self.connection,self.bundle.taxonomy); self.service=Phase12Service(self.connection,self.bundle)
    def tearDown(self): self.connection.close(); self.temp.cleanup()
    def test_status_is_local_deterministic_and_nonmutating(self):
        before=self.connection.total_changes; first=self.service.status(); second=self.service.status(); self.assertEqual(first,second); self.assertEqual(before,self.connection.total_changes); self.assertEqual("no_active_binding",first.account_state); self.assertTrue(any(item.name=="addition_recovery" for item in first.categories)); self.assertFalse(hasattr(self.service,"run")); self.assertFalse(hasattr(self.service,"resume"))
    def test_limit_is_bounded_and_invalid_limit_is_rejected(self):
        self.assertTrue(all(len(item.identifiers)<=1 for item in self.service.status(1).categories))
        with self.assertRaises(ValueError): self.service.status(0)
    def test_incomplete_ingestion_uses_only_active_sources_and_preserves_disabled_history(self):
        store=Phase3Store(self.connection); binding=store.bind_account("binding.synthetic", "account.synthetic", "user.synthetic")
        store.register_liked_source("source.disabled", binding.binding_id)
        store.start_scan(scan_id="scan.disabled", source_id="source.disabled", binding_id=binding.binding_id, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        store.mark_scan("scan.disabled", binding.binding_id, ScanStatus.FAILED, "synthetic_failure")
        store.disable_source("source.disabled", binding.binding_id)
        store.register_liked_source("source.active", binding.binding_id)
        store.start_scan(scan_id="scan.active", source_id="source.active", binding_id=binding.binding_id, starting_snapshot_id=None, starting_total=None, initial_page_signature=None, digest_version="synthetic.v1")
        store.mark_scan("scan.active", binding.binding_id, ScanStatus.FAILED, "synthetic_failure")
        report={item.name:item for item in self.service.status().categories}["incomplete_ingestion"]
        self.assertEqual(1, report.count); self.assertEqual(("scan.active",), report.identifiers)
        retained=self.connection.execute("SELECT status FROM source_scans WHERE scan_id='scan.disabled'").fetchone()[0]
        self.assertEqual("failed", retained)

if __name__=="__main__": unittest.main()
