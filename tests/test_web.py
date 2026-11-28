import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import web
from profile_store import ProfileStore

USER, PASSWORD = "admin", "s3cret-pass"
IP = "2401:4900:9864:a48c::2"


def auth(user=USER, password=PASSWORD):
    token = base64.b64encode("{}:{}".format(user, password).encode()).decode()
    return "Basic " + token


class FakeServer:
    """Stands in for server.py's module list and actions."""

    def __init__(self, store):
        self.modules = {"EC200U_001": {"host": IP, "port": 4059}}
        self.busy = None
        self.reads = []
        self.scans = []
        self.store = store

    def device_status(self, name):
        return {"name": name, **self.modules[name], "busy": self.busy}

    def set_module(self, name, host, port):
        self.modules[name] = {"host": host, "port": port}

    def delete_module(self, name):
        return self.modules.pop(name, None) is not None

    def start_read(self, name):
        if self.busy:
            return self.busy
        self.reads.append(name)
        return None

    def start_scan(self, name):
        if self.busy:
            return self.busy
        self.scans.append(name)
        return None

    def backend(self):
        return web.Backend(
            modules=self.modules, device_status=self.device_status,
            set_module=self.set_module, delete_module=self.delete_module,
            start_read=self.start_read, start_scan=self.start_scan,
            store=self.store)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProfileStore(os.path.join(self.tmp.name, "test.sqlite3"))
        self.server = FakeServer(self.store)
        self.lines = []
        self.app = web.App(self.server.backend(), USER, PASSWORD, self.lines.append)
        web.BAD_PASSWORD_DELAY = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, target, body=None, headers=None, authorization=None):
        hdrs = {"authorization": authorization if authorization is not None else auth()}
        raw = b""
        if body is not None:
            raw = json.dumps(body).encode()
            hdrs["content-type"] = "application/json"
        hdrs.update(headers or {})
        response = asyncio.run(self.app.handle(method, target, hdrs, raw, "test"))
        data = None
        if response.content_type == "application/json" and response.body:
            data = json.loads(response.body)
        return response, data

    # --- auth ---

    def test_no_password_means_no_app(self):
        with self.assertRaises(ValueError):
            web.App(self.server.backend(), USER, "", print)

    def test_login_required(self):
        response, _ = self.call("GET", "/api/devices", authorization="")
        self.assertEqual(response.status, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_wrong_password_rejected_and_logged(self):
        response, _ = self.call("GET", "/", authorization=auth(password="nope"))
        self.assertEqual(response.status, 401)
        self.assertTrue(any("wrong password" in line for line in self.lines))

    def test_wrong_user_rejected(self):
        response, _ = self.call("GET", "/api/devices", authorization=auth(user="root"))
        self.assertEqual(response.status, 401)

    def test_page_served(self):
        response, _ = self.call("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"EC200U Bridge", response.body)

    def test_post_needs_json_content_type(self):
        response, _ = self.call("POST", "/api/devices/EC200U_001/read",
                                headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status, 415)

    def test_security_headers(self):
        response, _ = self.call("GET", "/api/devices")
        raw = web.encode_response(response)
        self.assertIn(b"Content-Security-Policy: default-src 'self'", raw)
        self.assertIn(b"X-Frame-Options: DENY", raw)

    # --- devices ---

    def test_list_devices(self):
        response, data = self.call("GET", "/api/devices")
        self.assertEqual(response.status, 200)
        self.assertEqual([d["name"] for d in data["devices"]], ["EC200U_001"])

    def test_add_device(self):
        response, data = self.call("POST", "/api/devices",
                                   {"name": "EC200U_002", "host": "[2401:db8::5]", "port": 4059})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.server.modules["EC200U_002"], {"host": "2401:db8::5", "port": 4059})

    def test_add_device_rejects_bad_input(self):
        for body in ({"name": "bad name!", "host": IP},
                     {"name": "X", "host": "not-an-ip"},
                     {"name": "X", "host": "10.0.0.1"},
                     {"name": "X", "host": IP, "port": 70000}):
            response, _ = self.call("POST", "/api/devices", body)
            self.assertEqual(response.status, 400, body)
        self.assertEqual(list(self.server.modules), ["EC200U_001"])

    def test_delete_device(self):
        response, _ = self.call("DELETE", "/api/devices/EC200U_001")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.server.modules, {})

    def test_unknown_device_404(self):
        response, _ = self.call("POST", "/api/devices/NOPE/read", {})
        self.assertEqual(response.status, 404)

    # --- reads ---

    def test_read_now(self):
        response, _ = self.call("POST", "/api/devices/EC200U_001/read", {})
        self.assertEqual(response.status, 202)
        self.assertEqual(self.server.reads, ["EC200U_001"])

    def test_read_now_busy_409(self):
        self.server.busy = "a read is already running"
        response, data = self.call("POST", "/api/devices/EC200U_001/read", {})
        self.assertEqual(response.status, 409)
        self.assertIn("already running", data["error"])

    def test_reading_latest_and_history(self):
        self.store.save_reading("EC200U_001", [("Instantaneous", "Voltage", "1.0.12.7.0.255", "231.2 V")])
        self.store.save_reading("EC200U_001", [], "module disconnected during read")
        response, data = self.call("GET", "/api/devices/EC200U_001/reading")
        self.assertEqual(response.status, 200)
        self.assertEqual(data["latest"]["values"][0][3], "231.2 V")   # failed read skipped
        self.assertEqual([r["ok"] for r in data["history"]], [False, True])

    # --- profiles ---

    def add_profile(self):
        self.store.upsert_profiles("EC200U_001", [{
            "obis": "1.0.99.1.0.255", "class_id": 7, "class_name": "GXDLMSProfileGeneric",
            "profile_type": "load", "description": "Load profile", "enabled": 1,
        }])
        self.store.save_rows("EC200U_001", "1.0.99.1.0.255", [
            {"capture_time": "2026-11-28T10:00:00", "values": [{"position": 1, "value": "a"}]},
            {"capture_time": "2026-11-28T10:30:00", "values": [{"position": 1, "value": "b"}]},
        ], 2)

    def test_profiles_list_toggle_and_rows(self):
        self.add_profile()
        _, data = self.call("GET", "/api/devices/EC200U_001/profiles")
        self.assertEqual(data["profiles"][0]["obis"], "1.0.99.1.0.255")

        response, _ = self.call("POST", "/api/devices/EC200U_001/profiles/1.0.99.1.0.255",
                                {"enabled": False})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.store.get_profile("EC200U_001", "1.0.99.1.0.255")["enabled"], 0)

        _, data = self.call("GET", "/api/devices/EC200U_001/profiles/1.0.99.1.0.255/rows?limit=1")
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["capture_time"], "2026-11-28T10:30:00")  # newest first

    def test_bad_obis_rejected(self):
        response, _ = self.call("GET", "/api/devices/EC200U_001/profiles/x;drop/rows")
        self.assertEqual(response.status, 400)

    def test_toggle_needs_boolean(self):
        self.add_profile()
        response, _ = self.call("POST", "/api/devices/EC200U_001/profiles/1.0.99.1.0.255",
                                {"enabled": "no"})
        self.assertEqual(response.status, 400)


class HttpParsingTests(unittest.TestCase):
    def parse(self, raw):
        async def run():
            reader = asyncio.StreamReader(limit=web.MAX_HEADER_BYTES)
            reader.feed_data(raw)
            reader.feed_eof()
            return await web.read_request(reader)
        return asyncio.run(run())

    def test_parses_request(self):
        method, target, headers, body = self.parse(
            b"POST /api/devices HTTP/1.1\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}")
        self.assertEqual((method, target, body), ("POST", "/api/devices", b"{}"))
        self.assertEqual(headers["content-type"], "application/json")

    def test_body_too_large(self):
        with self.assertRaises(OverflowError):
            self.parse(b"POST / HTTP/1.1\r\nContent-Length: 999999\r\n\r\n")


if __name__ == "__main__":
    unittest.main()
