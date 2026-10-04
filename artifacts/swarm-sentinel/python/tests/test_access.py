"""Access-control regressions using locally generated test JWTs, never real records."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import auth
import server
from live import CallInput, SessionInput, SessionStore

ISSUER = "https://test-issuer.invalid"
ORIGIN = "https://test-app.invalid"
CALL = {"agentId": "orchestrator", "action": "tool", "target": "mock:fs.read",
        "spanId": "test-span", "detail": "private-test-marker", "intent": "read"}


class AccessControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.config = patch.object(auth, "auth_config", return_value=(ISSUER, {ORIGIN}))
        self.keys = patch.object(auth, "key_client", return_value=SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.key.public_key())))
        self.config.start()
        self.keys.start()
        self.addCleanup(self.config.stop)
        self.addCleanup(self.keys.stop)
        self.stores = patch.multiple(server, SESSIONS=SessionStore(), DEMOS=SessionStore(limit=2))
        self.stores.start()
        self.addCleanup(self.stores.stop)
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.owner = self.headers("owner")
        self.other = self.headers("other")
        result = self.client.post("/api/swarm/sessions", json={}, headers=self.owner)
        self.assertEqual(result.status_code, 201, result.text)
        self.id = result.json()["sessionId"]
        self.path = f"/api/swarm/sessions/{self.id}"

    def token(self, subject="owner", **changes):
        now = int(time.time())
        claims = dict(sub=subject, sid="session-test", iss=ISSUER, azp=ORIGIN,
                      iat=now, nbf=now - 1, exp=now + 120)
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test"})

    def headers(self, subject="owner", **changes):
        return {"Authorization": f"Bearer {self.token(subject, **changes)}"}

    def test_anonymous_and_spoofed_identity_are_denied_everywhere(self):
        requests = [("GET", "/api/swarm/sessions", None),
                    ("POST", "/api/swarm/sessions", {}),
                    ("GET", self.path, None), ("GET", self.path + "/stream", None),
                    ("POST", self.path + "/evaluate", CALL), ("DELETE", self.path, None)]
        for method, path, body in requests:
            for headers in ({}, {"X-User-Id": "owner", "X-Session-Owner": "owner"}):
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(self.client.request(method, path, json=body, headers=headers).status_code, 401)
        self.assertIsNotNone(server.SESSIONS.get(self.id))

    def test_real_agent_labels_do_not_grant_access(self):
        result = self.client.post("/api/swarm/sessions", headers=self.owner,
                                  json={"policy": "agents", "label": "Real LLM agents · fixture"})
        self.assertEqual(result.status_code, 201, result.text)
        session_id = result.json()["sessionId"]
        rows = self.client.get("/api/swarm/sessions", headers=self.owner).json()
        row = next(row for row in rows if row["sessionId"] == session_id)
        self.assertEqual(row["label"], "Real LLM agents · fixture")
        self.assertEqual(self.client.get("/api/swarm/sessions", headers=self.other).json(), [])
        self.assertEqual(self.client.get(f"/api/swarm/sessions/{session_id}", headers=self.other).status_code, 404)
        self.assertEqual(self.client.get(f"/api/swarm/demo/sessions/{session_id}").status_code, 404)

    def test_unrelated_owner_cannot_read_mutate_or_stream(self):
        self.assertEqual(self.client.get("/api/swarm/sessions", headers=self.other).json(), [])
        for method, suffix, body in [("GET", "", None), ("GET", "/stream", None),
                                     ("POST", "/evaluate", CALL), ("DELETE", "", None)]:
            with self.subTest(method=method, suffix=suffix):
                denied = self.client.request(method, self.path + suffix, json=body, headers=self.other)
                unknown = self.client.request(method, "/api/swarm/sessions/missing" + suffix, json=body, headers=self.other)
                self.assertEqual(denied.status_code, 404)
                self.assertEqual(denied.json(), unknown.json())
        self.assertEqual(len(server.SESSIONS.get(self.id).gateway.telemetry), 0)

    def test_owner_evaluation_snapshot_report_and_delete(self):
        result = self.client.post(self.path + "/evaluate", json=CALL, headers=self.owner)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["allowed"])
        rows = self.client.get("/api/swarm/sessions", headers=self.owner).json()
        self.assertEqual([row["sessionId"] for row in rows], [self.id])
        response = self.client.get(self.path, headers=self.owner)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("Cookie", response.headers["vary"])
        run = response.json()
        self.assertEqual(run["events"][0]["detail"], "private-test-marker")
        self.assertTrue(run["report"])
        self.assertEqual(self.client.delete(self.path, headers=self.owner).status_code, 204)
        self.assertEqual(self.client.get(self.path, headers=self.owner).status_code, 404)

    def test_signed_cookie_works_and_mutation_checks_origin(self):
        self.client.cookies.set("__session", self.token())
        self.assertEqual(self.client.get(self.path).status_code, 200)
        self.assertEqual(self.client.post(self.path + "/evaluate", json=CALL).status_code, 403)
        self.assertEqual(self.client.delete(self.path, headers={"Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.post(self.path + "/evaluate", json=CALL, headers={"Origin": ORIGIN}).status_code, 200)

    def test_bad_tokens_fail_closed(self):
        tokens = [self.token(exp=int(time.time()) - 1), self.token(iss="https://evil.invalid"),
                  self.token(azp="https://evil.invalid"), self.token(nbf=int(time.time()) + 60),
                  self.token(sub=""), self.token(sid=""), "not-a-jwt",
                  jwt.encode(dict(sub="owner", exp=time.time() + 60), "untrusted-test-key-not-a-secret!!", algorithm="HS256")]
        for token in tokens:
            with self.subTest(token_kind=token[:8]):
                self.assertEqual(self.client.get(self.path, headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        with patch.object(auth, "auth_config", side_effect=server.HTTPException(503, "Not configured")):
            self.assertEqual(self.client.get(self.path, headers=self.owner).status_code, 503)
        with patch.object(auth, "key_client", side_effect=jwt.PyJWKClientConnectionError("offline")):
            self.assertEqual(self.client.get(self.path, headers=self.owner).status_code, 503)

    def test_public_demo_is_isolated_and_read_only(self):
        with patch("demo.run_injection"):
            response = self.client.post("/api/swarm/demo/injection", json={"pause": 0})
        self.assertEqual(response.status_code, 202)
        demo_id = response.json()["sessionId"]
        demo_path = f"/api/swarm/demo/sessions/{demo_id}"
        self.assertIn("SYNTHETIC", self.client.get("/api/swarm/demo/sessions").json()[0]["label"])
        self.assertIn("Synthetic", self.client.get(demo_path).json()["provenance"])
        self.assertEqual(self.client.get(f"/api/swarm/demo/sessions/{self.id}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/swarm/demo/sessions/{self.id}/stream").status_code, 404)
        self.assertEqual(self.client.get(f"/api/swarm/sessions/{demo_id}", headers=self.owner).status_code, 404)
        self.assertEqual(self.client.post(demo_path + "/evaluate", json=CALL).status_code, 404)
        self.assertEqual(self.client.delete(demo_path).status_code, 405)
        for _ in range(4):
            server.DEMOS.create(SessionInput())
        self.assertIsNotNone(server.SESSIONS.get(self.id))

    def test_owner_fields_are_never_accepted_from_client(self):
        self.assertEqual(self.client.post("/api/swarm/sessions", headers=self.owner,
                                         json={"owner": "other"}).status_code, 422)
        self.assertEqual(server.SESSIONS.get(self.id).owner, "owner")

    def test_village_is_disabled_and_metadata_hidden_by_default(self):
        with patch("auth.os.environ", {}), patch.object(server.store, "connect") as connect:
            for headers in ({}, self.owner):
                sources = self.client.get("/api/swarm/sources", headers=headers)
                self.assertEqual(sources.json()["aiVillage"], {"available": False, "episodes": []})
            self.assertEqual(self.client.post("/api/swarm/simulate", json={"scenario": "ai-village", "episodeId": "test"},
                                             headers=self.owner).status_code, 403)
            self.assertEqual(self.client.post("/api/swarm/simulate", json={"scenario": "ai-village", "episodeId": "test"}).status_code, 401)
            connect.assert_not_called()

    def test_private_capacity_never_evicts_another_owner(self):
        with patch.object(server.SESSIONS, "limit", 1):
            response = self.client.post("/api/swarm/sessions", json={}, headers=self.other)
            self.assertEqual(response.status_code, 429)
            self.assertIsNotNone(server.SESSIONS.get(self.id))

    def test_enabled_village_still_requires_explicit_reader_approval(self):
        with patch("auth.os.environ", {"SWARM_ENABLE_VILLAGE": "true", "SWARM_VILLAGE_READERS": "owner"}), \
                patch.object(server.store, "connect", return_value=None) as connect:
            self.assertFalse(self.client.get("/api/swarm/sources", headers=self.other).json()["aiVillage"]["available"])
            self.assertEqual(self.client.post("/api/swarm/simulate",
                                             json={"scenario": "ai-village", "episodeId": "test"},
                                             headers=self.other).status_code, 403)
            connect.assert_not_called()
            # Only the approved reader reaches the store. A missing store still fails explicitly.
            self.assertEqual(self.client.post("/api/swarm/simulate",
                                             json={"scenario": "ai-village", "episodeId": "test"},
                                             headers=self.owner).status_code, 503)
            connect.assert_called_once()

    def test_live_stream_owner_updates_expiry_and_delete(self):
        async def verify():
            principal = auth.Principal("owner", time.time() + 120)
            response = await server.stream_session(self.id, 0, principal)
            body = response.body_iterator
            server.SESSIONS.get(self.id).evaluate(CallInput(**CALL))
            message = await anext(body)
            self.assertIn("private-test-marker", message)
            # Deletion closes an existing subscription instead of leaking retained data.
            server.SESSIONS.delete(self.id)
            self.assertIn("event: closed", await anext(body))
            expired = server.SESSIONS.create(SessionInput(), owner="owner")
            response = server._stream(server.SESSIONS, expired.id, 0, auth.Principal("owner", time.time() - 1))
            self.assertIn("event: auth-expired", await anext(response.body_iterator))
        asyncio.run(verify())


if __name__ == "__main__":
    unittest.main()