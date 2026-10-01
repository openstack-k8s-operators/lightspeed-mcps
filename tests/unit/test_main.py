import unittest
from unittest.mock import MagicMock, patch

from rhos_ls_mcps import auth, main, settings


class ServerInitializationTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.Settings()
        self.security = auth.SecurityConfig()
        self.enterContext(patch.object(main.mcp_logging, "init_logging"))
        self.enterContext(patch.object(main.utils, "init_process_pool"))
        self.enterContext(
            patch.object(
                main.auth_module, "get_auth_settings", return_value=self.security
            )
        )
        self.servers = [MagicMock(name="openstack"), MagicMock(name="openshift")]
        self.mcp_server = self.enterContext(
            patch.object(main, "MCPServer", side_effect=self.servers)
        )
        self.osc_initialize = self.enterContext(patch.object(main.osc, "initialize"))
        self.oc_initialize = self.enterContext(patch.object(main.oc, "initialize"))

    def test_initializes_enabled_openstack_and_openshift_servers(self):
        osp, ocp = main.initialize(self.config)
        self.assertIs(osp, self.servers[0])
        self.assertIs(ocp, self.servers[1])
        self.mcp_server.assert_any_call(
            "rhoso-tools", auth_server_provider=None, auth=None, token_verifier=None
        )
        self.mcp_server.assert_any_call(
            "ocp-tools", auth_server_provider=None, auth=None, token_verifier=None
        )
        self.osc_initialize.assert_called_once_with(osp)
        self.oc_initialize.assert_called_once_with(ocp)

    def test_does_not_create_disabled_service_server(self):
        self.config.openstack.enabled = False
        osp, ocp = main.initialize(self.config)
        self.assertIsNone(osp)
        self.assertIs(ocp, self.servers[0])
        self.osc_initialize.assert_not_called()

    def test_rejects_configuration_with_no_enabled_services(self):
        self.config.openstack.enabled = False
        self.config.openshift.enabled = False
        with self.assertRaisesRegex(RuntimeError, "Both OpenStack and OpenShift"):
            main.initialize(self.config)


class MainEntrypointTests(unittest.TestCase):
    def test_passes_tls_configuration_to_uvicorn(self):
        config = settings.Settings(
            debug=True,
            tls={
                "ssl_certfile": "cert.pem",
                "ssl_keyfile": "key.pem",
                "ssl_keyfile_password": "password",
                "ssl_ca_certs": "ca.pem",
            },
        )
        log_config = {"formatters": {"access": {}, "default": {}}}
        with (
            patch.object(main.settings, "load_config", return_value=config),
            patch.object(main.uvicorn.config, "LOGGING_CONFIG", log_config),
            patch.object(main.uvicorn, "run") as run,
        ):
            main.main()
        self.assertEqual(run.call_args.kwargs["log_level"], "debug")
        self.assertEqual(run.call_args.kwargs["ssl_certfile"], "cert.pem")
        self.assertEqual(run.call_args.kwargs["ssl_cert_reqs"], main.ssl.CERT_REQUIRED)
