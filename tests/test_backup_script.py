"""
scripts/backup.sh makes a backup only its user can read: it holds the API keys (.env,
data/api_keys.env, data/secret_key). Run in a copy of a made-up project in a temporary folder -
never the real data.

    venv/bin/python -m unittest discover tests -p test_backup_script.py
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'backup.sh'


@unittest.skipUnless(shutil.which('bash') and shutil.which('tar') and shutil.which('curl'), 'needs bash, tar and curl')
class BackupIsPrivate(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.project, self.home = self.folder / 'project', self.folder / 'home'
        (self.project / 'scripts').mkdir(parents=True)
        (self.project / 'data').mkdir()
        self.home.mkdir()
        shutil.copy(SCRIPT, self.project / 'scripts' / 'backup.sh')
        (self.project / '.env').write_text('GEMINI_API_KEY=secret\n')
        (self.project / 'data' / 'api_keys.env').write_text('OPENAI_API_KEY=secret\n')
        (self.project / 'data' / 'secret_key').write_text('abc')

    def run_script(self):
        environment = {**os.environ, 'HOME': str(self.home), 'SCANNER_URL': 'http://127.0.0.1:1'}   # no server: files are snapshot on their own
        return subprocess.run(['bash', str(self.project / 'scripts' / 'backup.sh')], env=environment,
                              capture_output=True, text=True, cwd=str(self.project), umask=0o022)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_the_folder_and_the_archive_are_the_owners_alone(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        backups = self.home / 'scanner-backups'
        archives = list(backups.glob('scanner-backup-*.tar.gz'))
        self.assertEqual(len(archives), 1)
        self.assertEqual(self.mode(backups), 0o700)
        self.assertEqual(self.mode(archives[0]), 0o600)

    def test_an_older_backup_in_the_folder_is_tightened_too(self):
        backups = self.home / 'scanner-backups'
        backups.mkdir()
        old = backups / 'scanner-backup-20200101_000000.tar.gz'
        old.write_bytes(b'x')
        old.chmod(0o644)
        backups.chmod(0o755)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.mode(old), 0o600)
        self.assertEqual(self.mode(backups), 0o700)


if __name__ == '__main__':
    unittest.main()
