import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import app as nava


class EducationSSOTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = handle.name
        handle.close()
        self.old_db_path = nava.DB_PATH
        nava.DB_PATH = self.db_path
        self.secret = "test-secret-that-is-long-enough-for-sso-123456"
        self.env = patch.dict(os.environ, {"NAVA_EDU_SSO_SECRET": self.secret})
        self.env.start()
        nava.init_db()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "INSERT INTO users(username,password_hash,name,bio,hue,created_at) VALUES(?,?,?,?,?,?)",
                ("amir_goli", "not-used-for-sso", "Amir", "", 120, time.time())
            )
            db.commit()
        self.client = nava.app.test_client()

    def tearDown(self):
        self.env.stop()
        nava.DB_PATH = self.old_db_path
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def make_token(self, username="amir_goli", jti="nonce-one"):
        payload = {"sub": username, "iss": "goli-abad-education",
                   "exp": int(time.time()) + 60, "jti": jti}
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        signature = base64.urlsafe_b64encode(
            hmac.new(self.secret.encode(), encoded.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        return encoded + "." + signature

    def test_valid_token_logs_in_existing_account(self):
        response = self.client.get("/sso/education?token=" + self.make_token())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["username"], "amir_goli")

    def test_token_cannot_be_reused(self):
        token = self.make_token()
        self.assertEqual(self.client.get("/sso/education?token=" + token).status_code, 302)
        self.assertEqual(self.client.get("/sso/education?token=" + token).status_code, 409)

    def test_unknown_account_is_not_created(self):
        response = self.client.get("/sso/education?token=" + self.make_token("missing_user"))
        self.assertEqual(response.status_code, 403)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE username='missing_user'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
