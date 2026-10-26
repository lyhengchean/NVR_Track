import os
import tempfile
import unittest
from pathlib import Path
from app.report_lock import acquire_report_lock


class LockTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'Uses Linux flock')
    def test_second_report_cannot_hold_same_lock(self):
        with tempfile.TemporaryDirectory() as root:
            first=acquire_report_lock(Path(root),'nvr-01')
            self.assertIsNotNone(first)
            try:
                self.assertIs(acquire_report_lock(Path(root),'nvr-01'),False)
            finally:
                first.close()
            second=acquire_report_lock(Path(root),'nvr-01')
            self.assertIsNot(second,False)
            second.close()
