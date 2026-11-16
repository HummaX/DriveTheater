"""Synthetic local fixtures only: no network, provider or application server."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from drivetheater.cache_safety import CacheConflict, CacheManager, safe_walk
from drivetheater.db import Database, utcnow
from drivetheater.feature_schema import migrate
from drivetheater.storage_features import StorageService

SAFE_DELETE = os.name == 'nt' or (hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='dt-storage-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.media = self.root / 'cache'
        self.media.mkdir()
        self.db = Database(self.root / 'db.sqlite3')
        self.db.initialize()
        with self.db.transaction() as conn:
            migrate(conn)
            for name in ('alice', 'bob'):
                conn.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)', (name, 'unused', utcnow()))
            conn.execute("INSERT INTO sources(id,name,kind) VALUES('source','Private source','http')")
        self.service = StorageService(self.db, self.media)

    def video(self, identifier='one', payload=b'original'):
        directory = self.media / identifier
        directory.mkdir()
        original = directory / 'source.bin'
        original.write_bytes(payload)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO videos(id,title,source_id,source_kind,remote_id,state,local_path,asset_dir,size_bytes,updated_at) VALUES(?,?,?,'http',?,'cached',?,?,999,?)", (identifier, 'Private title ' + identifier, 'source', identifier, str(original), str(directory), utcnow()))
        return original

    def update(self, **changes):
        with self.db.transaction() as conn:
            conn.execute('UPDATE videos SET ' + ','.join(key + '=?' for key in changes) + " WHERE id='one'", tuple(changes.values()))

    def test_artifacts_use_observed_file_sizes_not_remote_size(self):
        original = self.video(payload=b'x' * 10)
        (original.parent / 'hls').mkdir()
        (original.parent / 'hls/segment.ts').write_bytes(b'x' * 20)
        (original.parent / 'preview.mp4').write_bytes(b'x' * 30)
        (original.parent / 'caption.vtt').write_bytes(b'x' * 40)
        (original.parent / 'sprites.vtt').write_bytes(b'x' * 5)
        result = self.service.inventory()
        self.assertEqual(result['items'][0]['artifacts'], {'original': 10, 'derived': 20, 'previews': 35, 'captions': 40})
        self.assertEqual(result['totals']['logical_bytes'], 105)
        self.assertEqual(result['totals']['file_count'], 5)

    def test_hardlinks_count_physical_and_reclaim_once(self):
        first, second = self.video('one', b'x' * 5000), self.video('two', b'x')
        second.unlink()
        try:
            os.link(first, second)
        except OSError:
            self.skipTest('Hard links are unavailable')
        inventory = self.service.inventory()
        self.assertEqual(inventory['totals']['logical_bytes'], 10000)
        self.assertEqual(inventory['totals']['unique_bytes'], 5000)
        self.assertEqual(inventory['totals']['shared_inode_count'], 1)
        self.assertEqual(inventory['items'][0]['reclaimable_bytes'], 0)
        plan = self.service.create_cleanup_plan('alice', ['one', 'two'])
        self.assertEqual(plan['shared_inode_count'], 1)
        self.assertEqual(plan['reclaimable_bytes'], inventory['totals']['physical_bytes'])

    def test_cross_category_hardlink_physical_totals_are_additive(self):
        original = self.video(payload=b'x' * 8192)
        os.link(original, original.parent / 'preview.mp4')
        result = self.service.inventory()
        self.assertEqual(sum(item['physical_bytes'] for item in result['artifacts'].values()), result['totals']['physical_bytes'])
        self.assertEqual(result['artifacts']['previews']['physical_bytes'], 0)
        self.assertEqual(result['artifacts']['previews']['logical_bytes'], 8192)

    @unittest.skipUnless(SAFE_DELETE, 'Safe handle-relative removal unavailable')
    def test_multi_title_plan_reclaims_shared_inode_only_after_last_link(self):
        first, second = self.video('one'), self.video('two')
        second.unlink()
        os.link(first, second)
        expected = self.service.inventory()['totals']['physical_bytes']
        plan = self.service.create_cleanup_plan('alice', ['one', 'two'])
        result = self.service.execute_cleanup_plan('alice', plan['plan_id'])
        self.assertEqual(result['removed'], 2)
        self.assertEqual(result['freed_bytes'], expected)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_missing_original_still_allows_safe_recovery_cleanup(self):
        original = self.video()
        original.unlink()
        remaining = original.parent / 'preview.mp4'
        remaining.write_bytes(b'preview')
        self.assertEqual(self.service.inventory()['totals']['logical_bytes'], 7)
        reservation = self.service.cache.reserve('one')
        self.assertEqual(len(reservation['files']), 1)
        with patch('drivetheater.cache_safety._safe_unlink', side_effect=OSError('synthetic rollback')):
            with self.assertRaises(OSError):
                self.service.cache.remove_reserved(reservation)
        self.assertTrue(remaining.exists())

    def test_outside_paths_rejected_without_touching_external_content(self):
        self.video()
        external = self.root / 'secret'
        external.write_text('do not touch')
        self.update(local_path=str(external))
        with self.assertRaises(CacheConflict):
            self.service.cache.reserve('one')
        self.assertEqual(external.read_text(), 'do not touch')

    def test_plan_bound_to_owner_expiring_single_use_even_when_stale(self):
        self.video()
        plan = self.service.create_cleanup_plan('alice', ['one'])
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('bob', plan['plan_id'])
        self.update(content_sha256='a' * 64)
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT consumed FROM cleanup_plans WHERE id=?', (plan['plan_id'],)).fetchone()[0], 1)
        expired = self.service.create_cleanup_plan('alice', ['one'])
        with self.db.transaction() as conn:
            conn.execute('UPDATE cleanup_plans SET expires_at=0 WHERE id=?', (expired['plan_id'],))
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', expired['plan_id'])

    def test_same_size_file_replacement_and_new_artifact_invalidate_snapshot(self):
        original = self.video()
        plan = self.service.create_cleanup_plan('alice', ['one'])
        original.write_bytes(b'changed!')
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])
        plan = self.service.create_cleanup_plan('alice', ['one'])
        (original.parent / 'new.vtt').write_bytes(b'new')
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])

    def test_path_change_and_new_shared_hash_reference_invalidate_plan(self):
        original = self.video()
        self.update(content_sha256='a' * 64)
        plan = self.service.create_cleanup_plan('alice', ['one'])
        self.video('two')
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET content_sha256=? WHERE id='two'", ('a' * 64,))
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])
        plan = self.service.create_cleanup_plan('alice', ['one'])
        self.update(local_path=str(original.parent / 'preview.mp4'))
        with self.assertRaises((CacheConflict, OSError)):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])

    def test_lease_or_running_job_blocks_even_after_preview(self):
        self.video()
        plan = self.service.create_cleanup_plan('alice', ['one'])
        self.update(active_until=int(time.time()) + 100)
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])
        self.update(active_until=0)
        plan = self.service.create_cleanup_plan('alice', ['one'])
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO jobs(id,video_id,title,state,phase,updated_at) VALUES('job','one','private','queued','download',?)", (utcnow(),))
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])

    def test_active_shared_hash_peer_blocks_reservation(self):
        self.video('one')
        self.video('two')
        with self.db.transaction() as conn:
            conn.execute('UPDATE videos SET content_sha256=?', ('b' * 64,))
            conn.execute("UPDATE videos SET active_until=? WHERE id='two'", (int(time.time()) + 100,))
        with self.assertRaises(CacheConflict):
            self.service.cache.reserve('one')

    def test_active_inode_peer_without_hash_blocks_reservation(self):
        one, two = self.video('one'), self.video('two')
        two.unlink()
        try:
            os.link(one, two)
        except OSError:
            self.skipTest('Hard links are unavailable')
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET active_until=? WHERE id='two'", (int(time.time()) + 100,))
        with self.assertRaises(CacheConflict):
            self.service.cache.reserve('one')

    def test_shared_path_cannot_remove_unselected_rows_artifacts(self):
        one = self.video()
        self.video('two')
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET local_path=?,asset_dir=? WHERE id='two'", (str(one), str(one.parent)))
        with self.assertRaises(CacheConflict):
            self.service.cache.reserve('one')
        self.assertTrue(one.exists())

    def test_outside_and_symlink_paths_rejected(self):
        original = self.video()
        external = self.root / 'secret'
        external.write_text('do not touch')
        self.update(local_path=str(external))
        with self.assertRaises(CacheConflict):
            self.service.cache.reserve('one')
        self.update(local_path=str(original))
        try:
            (original.parent / 'unsafe').symlink_to(external)
        except OSError:
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(CacheConflict):
            self.service.inventory()
        self.assertEqual(external.read_text(), 'do not touch')

    def test_walk_bound_and_invalid_policy(self):
        original = self.video()
        with self.assertRaises(CacheConflict):
            safe_walk(original.parent, self.media, maximum=1)
        for keep in (1, True, '7', -7):
            with self.assertRaises(ValueError):
                self.service.set_policy('title', 'one', keep)

    def test_policies_off_by_default_and_only_explicit_maintenance_enforces(self):
        self.video()
        self.assertEqual(self.service.policies(), [])
        self.assertEqual(self.service.maintenance()['removed'], 0)
        self.service.set_policy('source', 'source', 7)
        self.assertTrue((self.media / 'one/source.bin').exists())
        self.service.set_policy('title', 'one', None)  # Explicit title off overrides source.
        self.update(updated_at='2000-01-01T00:00:00Z')
        self.assertEqual(self.service.maintenance()['removed'], 0)

    def test_delete_after_watch_requires_all_recorded_viewers(self):
        self.video()
        self.service.set_policy('title', 'one', None, True)
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO video_user_state(username,video_id,watched) VALUES('alice','one',1),('bob','one',0)")
        with patch.object(self.service.cache, 'reserve') as reserve:
            self.service.maintenance()
            reserve.assert_not_called()
        with self.db.transaction() as conn:
            conn.execute("UPDATE video_user_state SET watched=1 WHERE username='bob'")
        with patch.object(self.service.cache, 'reserve', return_value={}) as reserve, patch.object(self.service.cache, 'remove_reserved', return_value={'removed': 1, 'freed_bytes': 8}):
            self.assertEqual(self.service.maintenance()['removed'], 1)
            reserve.assert_called_once_with('one', automatic=True)

    def test_integrity_explicit_local_provider_and_mismatch(self):
        original = self.video()
        with patch('drivetheater.storage_features.hashlib.sha256', side_effect=AssertionError('GET must not hash')):
            self.assertEqual(self.service.integrity_status('one')['status'], 'not_checked')
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        self.assertEqual(self.service.verify_integrity('one')['status'], 'locally_computed')
        self.update(content_sha256=digest)
        self.assertEqual(self.service.verify_integrity('one')['status'], 'local_match')
        self.assertEqual(self.service.verify_integrity('one', provider_sha256=digest)['status'], 'provider_verified')
        self.assertEqual(self.service.verify_integrity('one', provider_sha256='0' * 64)['status'], 'mismatch')
        with self.assertRaises(CacheConflict):
            self.service.verify_integrity('one', max_bytes=1)

    def test_failure_before_unlink_restores_original_row(self):
        original = self.video()
        reservation = self.service.cache.reserve('one')
        with patch('drivetheater.cache_safety._safe_unlink', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.service.cache.remove_reserved(reservation)
        with self.db.connect() as conn:
            row = conn.execute("SELECT state,local_path FROM videos WHERE id='one'").fetchone()
        self.assertEqual(row['state'], 'cached')
        self.assertEqual(row['local_path'], str(original))

    def test_synthetic_unlink_success_exercises_finalization_and_hardlinks(self):
        # Exercise platform-independent transaction/finalization logic while the
        # actual no-follow unlink path has its own platform-gated tests below.
        one, two = self.video('one'), self.video('two')
        two.unlink()
        os.link(one, two)
        self.service.verify_integrity('one')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO caption_cues VALUES('one','track',0,0,1,'private',0)")
            conn.execute("INSERT INTO probe_diagnostics VALUES('one','{}',0)")
        def unlink(root, item):
            current = root / item['path']
            physical = item['physical'] if current.stat().st_nlink == 1 else 0
            current.unlink()
            return physical
        plan = self.service.create_cleanup_plan('alice', ['one'])
        with patch('drivetheater.cache_safety._safe_unlink', side_effect=unlink):
            result = self.service.execute_cleanup_plan('alice', plan['plan_id'])
        self.assertEqual(result['freed_bytes'], 0)
        self.assertFalse(one.exists())
        self.assertEqual(two.read_bytes(), b'original')
        self.assertEqual(self.service.integrity_status('one')['status'], 'not_checked')
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM caption_cues').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM probe_diagnostics').fetchone()[0], 0)
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])

    def test_partial_unlink_failure_invalidates_but_keeps_recoverable_files(self):
        original = self.video()
        (original.parent / 'a.vtt').write_bytes(b'caption')
        self.service.verify_integrity('one')
        reservation = self.service.cache.reserve('one')
        count = 0
        def fail_second(root, item):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('synthetic partial failure')
            (root / item['path']).unlink()
            return item['physical']
        with patch('drivetheater.cache_safety._safe_unlink', side_effect=fail_second):
            with self.assertRaises(OSError):
                self.service.cache.remove_reserved(reservation)
        self.assertTrue(original.exists())
        with self.db.connect() as conn:
            row = conn.execute("SELECT state,playback_url,content_sha256 FROM videos WHERE id='one'").fetchone()
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['playback_url'])
        self.assertIsNone(row['content_sha256'])
        self.assertEqual(self.service.integrity_status('one')['status'], 'not_checked')

    @unittest.skipUnless(SAFE_DELETE, 'No-follow directory-relative unlink unavailable')
    def test_cleanup_removes_only_reserved_files_and_invalidates_indexes(self):
        original = self.video()
        self.service.verify_integrity('one')
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO caption_cues VALUES('one','track',0,0,1,'private',0)")
        plan = self.service.create_cleanup_plan('alice', ['one'])
        result = self.service.execute_cleanup_plan('alice', plan['plan_id'])
        self.assertEqual(result['removed'], 1)
        self.assertFalse(original.exists())
        self.assertEqual(self.service.integrity_status('one')['status'], 'not_checked')
        with self.assertRaises(CacheConflict):
            self.service.execute_cleanup_plan('alice', plan['plan_id'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM caption_cues').fetchone()[0], 0)

    @unittest.skipUnless(SAFE_DELETE, 'No-follow directory-relative unlink unavailable')
    def test_deleting_one_hardlink_preserves_other_copy_and_frees_zero(self):
        one, two = self.video('one'), self.video('two')
        two.unlink()
        os.link(one, two)
        result = self.service.cache.remove_reserved(self.service.cache.reserve('one'))
        self.assertEqual(result['freed_bytes'], 0)
        self.assertEqual(two.read_bytes(), b'original')

    @unittest.skipIf(SAFE_DELETE, 'Only tests unsupported-runtime fail-closed behavior')
    def test_unsupported_destructive_runtime_fails_closed(self):
        original = self.video()
        with self.assertRaises(CacheConflict):
            self.service.cache.remove_reserved(self.service.cache.reserve('one'))
        self.assertTrue(original.exists())


if __name__ == '__main__':
    unittest.main()
