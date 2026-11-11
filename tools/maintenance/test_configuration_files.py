import os
import tempfile
import unittest
from pathlib import Path
from configuration_files import copy_verified, read_regular, ConfigurationConflict


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.canonical = self.root / "original"
        self.original = b"synthetic original data"
        self.canonical.write_bytes(self.original)

    def tearDown(self):
        self.tmp.cleanup()

    def test_linked_files_refused(self):
        link = self.root / "link"
        os.link(self.canonical, link)
        with self.assertRaises(ConfigurationConflict):
            read_regular(self.canonical)
        link.unlink()
        link.symlink_to(self.canonical)
        with self.assertRaises(OSError):
            read_regular(link)

    def test_verified_backup_is_independent_and_never_overwritten(self):
        target = self.root / "backup"
        result = copy_verified(self.canonical, target)
        self.assertEqual(result["bytes"], len(self.original))
        self.canonical.write_bytes(b"new writes")
        self.assertEqual(target.read_bytes(), self.original)
        with self.assertRaises(FileExistsError):
            copy_verified(self.canonical, target)


if __name__ == "__main__":
    unittest.main()
