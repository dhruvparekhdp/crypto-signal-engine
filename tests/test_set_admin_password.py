"""The only way to rotate the admin password, since the UI has no screen for it."""
import unittest

from scripts.set_admin_password import check


class TestPasswordRules(unittest.TestCase):
    def test_short_and_all_digit_passwords_are_refused(self):
        self.assertIsNotNone(check("short"))
        self.assertIsNotNone(check("919876543210"))

    def test_a_long_mixed_password_is_accepted(self):
        self.assertIsNone(check("correct horse battery"))


if __name__ == "__main__":
    unittest.main()
