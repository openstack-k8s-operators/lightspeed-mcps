import unittest
from unittest.mock import mock_open, patch

from pydantic import ValidationError

from rhos_ls_mcps import settings


class TLSSettingsTests(unittest.TestCase):
    def test_requires_certificate_when_tls_dependents_are_set(self):
        with self.assertRaises(ValidationError):
            settings.TLSSettings(ssl_keyfile="key.pem")

    def test_accepts_complete_tls_settings(self):
        tls = settings.TLSSettings(ssl_certfile="cert.pem", ssl_keyfile="key.pem")
        self.assertEqual(tls.ssl_certfile, "cert.pem")


class ConfigLoadingTests(unittest.TestCase):
    def test_uses_defaults_when_config_file_is_missing(self):
        with (
            patch.object(settings, "CONFIG", None),
            patch.object(settings.os.path, "exists", return_value=False),
        ):
            config = settings.load_config()
        self.assertEqual(config.port, 8080)

    def test_loads_values_from_yaml_file(self):
        with (
            patch.object(settings, "CONFIG", None),
            patch.object(settings.os.path, "exists", return_value=True),
            patch("builtins.open", mock_open(read_data="port: 9000\n")),
        ):
            config = settings.load_config()
        self.assertEqual(config.port, 9000)
