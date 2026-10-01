import json
import unittest

from rhos_ls_mcps import extra_endpoints


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_healthy_status(self):
        response = await extra_endpoints.health(None)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), {"status": "ok"})

    def test_registers_get_health_route(self):
        routes = extra_endpoints.get_routes()
        self.assertEqual(routes[0].path, "/health")
        self.assertIn("GET", routes[0].methods)
