"""Tenancy and roles at the HTTP boundary, from verified Clerk organization claims only."""
import os
import unittest
from unittest.mock import patch

import auth
import server
import test_access as access

CALL = {"agentId": "orchestrator", "action": "tool", "target": "mock:search.read", "spanId": "s", "intent": "read"}


class Tenancy(unittest.TestCase):
    # Borrow the signed-test-JWT fixtures without re-running AccessControl's own tests.
    setUpClass = classmethod(access.AccessControl.setUpClass.__func__)
    setUp = access.AccessControl.setUp
    token = access.AccessControl.token
    headers = access.AccessControl.headers

    def org(self, subject, role, org="acme", legacy=False):
        claims = {"org_id": org, "org_role": f"org:{role}"} if legacy else {"o": {"id": org, "rol": role}}
        return self.headers(subject, **claims)

    def create(self, headers, status=201):
        response = self.client.post("/api/swarm/sessions", json={}, headers=headers)
        self.assertEqual(response.status_code, status, response.text)
        return response.json().get("sessionId")

    def test_personal_tenants_keep_owner_only_access(self):
        me = self.client.get("/api/swarm/me", headers=self.owner).json()
        self.assertEqual((me["tenant"], me["role"]), ("user:owner", "admin"))
        self.assertEqual(self.client.get(self.path, headers=self.other).status_code, 404)

    def test_organization_members_share_sessions_by_role(self):
        admin, member, auditor = self.org("ana", "admin"), self.org("ben", "member"), self.org("cy", "auditor")
        session = self.create(admin)
        path = f"/api/swarm/sessions/{session}"
        for headers in (admin, member, auditor):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 200)
            self.assertIn(session, [r["sessionId"] for r in self.client.get("/api/swarm/sessions", headers=headers).json()])
        self.assertEqual(self.client.post(path + "/evaluate", json=CALL, headers=member).status_code, 200)
        # Unmapped organization roles become viewers: read, never act.
        self.assertEqual(self.client.get("/api/swarm/me", headers=auditor).json()["permissions"], ["session.read"])
        self.assertEqual(self.client.post(path + "/evaluate", json=CALL, headers=auditor).status_code, 403)
        self.create(auditor, status=403)
        self.assertEqual(self.client.delete(path, headers=auditor).status_code, 403)

    def test_operators_delete_only_their_own_sessions(self):
        admin, member = self.org("ana", "admin"), self.org("ben", "member")
        admins = self.create(admin)
        members = self.create(member)
        self.assertEqual(self.client.delete(f"/api/swarm/sessions/{admins}", headers=member).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/swarm/sessions/{members}", headers=member).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/swarm/sessions/{admins}", headers=admin).status_code, 204)

    def test_tenants_are_isolated(self):
        session = self.create(self.org("ana", "admin"))
        path = f"/api/swarm/sessions/{session}"
        for headers in (self.org("eve", "admin", org="globex"), self.headers("ana"), self.owner):
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
                self.assertNotIn(session, [r["sessionId"] for r in
                                           self.client.get("/api/swarm/sessions", headers=headers).json()])
        # The same user's personal tenant is separate from their organization's.
        self.assertNotIn(self.id, [r["sessionId"] for r in
                                   self.client.get("/api/swarm/sessions", headers=self.org("owner", "admin")).json()])

    def test_legacy_claims_and_role_mapping(self):
        legacy = self.org("ben", "member", legacy=True)
        self.assertEqual(self.client.get("/api/swarm/me", headers=legacy).json()["tenant"], "org:acme")
        self.assertEqual(self.client.get("/api/swarm/me", headers=legacy).json()["role"], "operator")
        with patch.dict(os.environ, {"SWARM_ORG_ROLES": "member=viewer,auditor=operator"}):
            self.assertEqual(self.client.get("/api/swarm/me", headers=self.org("cy", "auditor")).json()["role"], "operator")
            self.assertEqual(self.client.get("/api/swarm/me", headers=legacy).json()["role"], "viewer")

    def test_tenancy_ignores_unverified_shapes(self):
        self.assertEqual(auth.tenancy({"o": "acme"}), ("", "admin"))
        self.assertEqual(auth.tenancy({"org_id": ""}), ("", "admin"))
        self.assertEqual(auth.tenancy({"o": {"id": "acme", "rol": "owner"}}), ("org:acme", "viewer"))

    def test_session_quota_is_per_tenant(self):
        with patch("live.PER_TENANT", 2):
            admin = self.org("ana", "admin")
            self.create(admin)
            self.create(admin)
            self.create(admin, status=429)
            self.create(self.org("eve", "admin", org="globex"))


if __name__ == "__main__":
    unittest.main()
