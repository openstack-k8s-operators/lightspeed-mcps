import unittest

from rhos_ls_mcps import oc_defaults


class DefaultCommandListTests(unittest.TestCase):
    def test_includes_safe_read_commands(self):
        self.assertIn("get", oc_defaults.DEFAULT_ALLOWED_COMMANDS)
        self.assertIn("auth can-i", oc_defaults.DEFAULT_ALLOWED_COMMANDS)

    def test_includes_sensitive_commands_in_block_list(self):
        self.assertIn("config", oc_defaults.DEFAULT_BLOCKED_COMMANDS)
        self.assertIn("logout", oc_defaults.DEFAULT_BLOCKED_COMMANDS)
