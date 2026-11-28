import hashlib
import json
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import heartbeat


def load_device_heartbeat():
    """Import device/heartbeat.py with CPython stand-ins for QuecPython modules."""
    import binascii
    import socket
    import time

    utime = types.SimpleNamespace(
        ticks_ms=lambda: int(time.monotonic() * 1000),
        ticks_diff=lambda a, b: a - b)
    stubs = {
        "usocket": socket,
        "net": types.SimpleNamespace(csqQueryPoll=lambda: 21),
        "ubinascii": binascii,
        "uhashlib": hashlib,
        "ujson": json,
        "utime": utime,
    }
    saved = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "device_heartbeat", os.path.join(ROOT, "device", "heartbeat.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


DEVICE = load_device_heartbeat()
KEY = b"0123456789abcdef0123456789abcdef"
IP = "2401:4900:9864:a48c::2"
NEW_IP = "2401:4900:9864:a48c::9"


def device_packet(key, ipv6, seq=1):
    hb = DEVICE.Heartbeat("EC200U_001", "::1", 4060, 300, "/no/such/file")
    hb.key = key
    hb.seq = seq
    return hb.packet(ipv6)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.modules = {"EC200U_001": {"host": IP, "port": 4059}}
        self.saved = 0
        self.lines = []

        def save():
            self.saved += 1

        self.monitor = heartbeat.HeartbeatMonitor(
            self.modules, save, self.lines.append, key=KEY)

    def test_device_signature_is_accepted(self):
        self.monitor.handle(device_packet(KEY, IP), IP)
        beat = self.monitor.beats["EC200U_001"]
        self.assertTrue(beat["signed"])
        self.assertEqual(beat["csq"], 21)
        self.assertEqual(beat["meter_age"], -1)

    def test_wrong_key_is_dropped(self):
        self.monitor.handle(device_packet(b"other key", IP), IP)
        self.assertNotIn("EC200U_001", self.monitor.beats)

    def test_signed_beat_from_new_address_updates_module(self):
        self.monitor.handle(device_packet(KEY, NEW_IP), NEW_IP)
        self.assertEqual(self.modules["EC200U_001"]["host"], NEW_IP)
        self.assertEqual(self.saved, 1)

    def test_unsigned_beat_never_changes_address(self):
        self.monitor.handle(device_packet(None, NEW_IP), NEW_IP)
        self.assertEqual(self.modules["EC200U_001"]["host"], IP)
        self.assertFalse(self.monitor.beats["EC200U_001"]["signed"])

    def test_address_not_changed_when_source_differs(self):
        self.monitor.handle(device_packet(KEY, NEW_IP), IP)
        self.assertEqual(self.modules["EC200U_001"]["host"], IP)

    def test_replayed_seq_is_dropped(self):
        self.monitor.handle(device_packet(KEY, IP, seq=5), IP)
        self.monitor.handle(device_packet(KEY, NEW_IP, seq=5), NEW_IP)
        self.assertEqual(self.modules["EC200U_001"]["host"], IP)

    def test_offline_after_missed_beats(self):
        self.monitor.handle(device_packet(KEY, IP), IP)
        at = self.monitor.beats["EC200U_001"]["at"]
        self.monitor.check_offline(now=at + 300 * 3 + 1)
        self.assertFalse(self.monitor.beats["EC200U_001"]["online"])
        self.assertIn("OFFLINE", self.monitor.state("EC200U_001"))


class FakeSocket:
    def __init__(self, calls):
        self.calls = calls

    def bind(self, addr):
        self.calls.append(("bind", addr))

    def connect(self, addr):
        self.calls.append(("connect", addr))

    def send(self, data):
        self.calls.append(("send", len(data)))

    def close(self):
        self.calls.append(("close",))


class DeviceSocketTests(unittest.TestCase):
    """The EC200U only sends UDP after bind(own IPv6) + connect()."""

    def setUp(self):
        self.calls = []
        self.real_usocket = DEVICE.usocket
        DEVICE.usocket = types.SimpleNamespace(
            AF_INET6=10, SOCK_DGRAM=2,
            socket=lambda family, kind: FakeSocket(self.calls))
        self.hb = DEVICE.Heartbeat("EC200U_001", "2401:db8::1", 4060, 300, "/no/such/file")

    def tearDown(self):
        DEVICE.usocket = self.real_usocket

    def test_binds_own_address_before_connect(self):
        self.hb.send(IP)
        self.hb.send(IP)
        self.assertEqual([c[0] for c in self.calls],
                         ["bind", "connect", "send", "send"])
        self.assertEqual(self.calls[0], ("bind", (IP, 0)))
        self.assertEqual(self.calls[1], ("connect", ("2401:db8::1", 4060)))

    def test_rebinds_when_address_changes(self):
        self.hb.send(IP)
        self.hb.send(NEW_IP)
        self.assertEqual([c[0] for c in self.calls],
                         ["bind", "connect", "send", "close", "bind", "connect", "send"])
        self.assertEqual(self.calls[4], ("bind", (NEW_IP, 0)))


if __name__ == "__main__":
    unittest.main()
