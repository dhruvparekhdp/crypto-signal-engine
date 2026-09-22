"""
A backup that is committed must not carry credentials.

`backups/*.json.gz` is deliberately not gitignored — committing the dump is
the whole point of it. This repository is public. Those two facts together
mean a dump of `admin_auth` publishes the operator's password hash, its salt
and a live session token to anyone who clones, and the token alone is enough
to act as the operator until the password changes.

The redaction therefore belongs at the dump, where it cannot be skipped, not
in a habit of remembering to edit the file afterwards.
"""
import gzip
import json
import unittest
from pathlib import Path

from scripts.backup_neon import REDACTED_COLUMNS

_BACKUP = Path(__file__).resolve().parent.parent / "backups" / "neon_backup_latest.json.gz"


class TestRedactionPolicy(unittest.TestCase):
    def test_every_admin_credential_column_is_listed(self):
        """
        Pinned against the model, not a copy of the list: a column added to
        AdminAuth later — a recovery code, an API key — would otherwise start
        being published with nothing to notice it.
        """
        from storage.models import AdminAuth

        cols = {c.name for c in AdminAuth.__table__.columns}
        secret = {c for c in cols
                  if any(w in c for w in ("hash", "salt", "token", "secret", "password"))}
        self.assertTrue(secret, "AdminAuth has no credential-shaped column; update this test")
        self.assertEqual(secret - REDACTED_COLUMNS["admin_auth"], set())


class TestTheCommittedBackup(unittest.TestCase):
    """The file already in the repository, not a hypothetical future one."""

    def setUp(self):
        if not _BACKUP.exists():
            self.skipTest("no committed backup in this checkout")
        with gzip.open(_BACKUP, "rt") as f:
            self.data = json.load(f)

    def test_it_carries_no_admin_credentials(self):
        for row in self.data.get("admin_auth", []):
            for col in REDACTED_COLUMNS["admin_auth"]:
                self.assertIsNone(row.get(col), f"{col} is still in the committed backup")

    def test_it_still_carries_the_data_it_exists_for(self):
        """Redaction must not have quietly emptied the useful tables."""
        self.assertGreater(len(self.data.get("crypto_signal_log", [])), 0)
