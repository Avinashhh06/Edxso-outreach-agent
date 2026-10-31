import unittest

try:
    from fastapi.testclient import TestClient
except Exception:  # fastapi/httpx not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class ApiTests(unittest.TestCase):
    def test_health_and_pipeline(self):
        from outreach.api import app
        c = TestClient(app)
        self.assertTrue(c.get("/api/health").json()["ok"])
        r = c.post("/api/pipeline/run", json={})
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(c.get("/api/influencers").json()), 49)
        self.assertEqual(c.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
