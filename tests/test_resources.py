"""Resource quota fixtures do not inspect the host's real cgroup state."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from drivetheater import resources


class CgroupCpuLimitTests(unittest.TestCase):
    def test_reads_cgroup_v2_quota(self):
        with tempfile.TemporaryDirectory(prefix='drivetheater-cgroup-') as tmp:
            root = Path(tmp)
            (root / 'cpu.max').write_text('250000 100000\n')
            self.assertEqual(resources.cpu_limit(root), 2.5)

    def test_reads_legacy_cgroup_quota(self):
        with tempfile.TemporaryDirectory(prefix='drivetheater-cgroup-') as tmp:
            cpu = Path(tmp) / 'cpu'
            cpu.mkdir()
            (cpu / 'cpu.cfs_quota_us').write_text('150000')
            (cpu / 'cpu.cfs_period_us').write_text('100000')
            self.assertEqual(resources.cpu_limit(Path(tmp)), 1.5)

    def test_unlimited_or_invalid_quota_has_no_limit(self):
        with tempfile.TemporaryDirectory(prefix='drivetheater-cgroup-') as tmp:
            root = Path(tmp)
            (root / 'cpu.max').write_text('max 100000\n')
            self.assertIsNone(resources.cpu_limit(root))


class MediaThreadBudgetTests(unittest.TestCase):
    def test_threads_respect_quota_and_encoder_slots(self):
        with patch.object(resources.os, 'cpu_count', return_value=8), \
             patch.object(resources.os, 'sched_getaffinity', return_value=set(range(6)), create=True), \
             patch.object(resources, 'cpu_limit', return_value=3.5), \
             patch.dict(os.environ, {'DT_ENCODER_SLOTS': '2'}, clear=False):
            self.assertEqual(resources.media_cpus(), 3.5)
            self.assertEqual(resources.media_threads(), 1)

    def test_invalid_media_cpu_override_does_not_bypass_cgroup_limit(self):
        with patch.object(resources.os, 'cpu_count', return_value=8), \
             patch.object(resources.os, 'sched_getaffinity', return_value=set(range(8)), create=True), \
             patch.object(resources, 'cpu_limit', return_value=2.0), \
             patch.dict(os.environ, {'DT_MEDIA_CPUS': 'not-a-number'}, clear=False):
            self.assertEqual(resources.media_cpus(), 2.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
