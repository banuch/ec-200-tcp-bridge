# =========================================
# WINTEK SOLUTIONS
# EC200U-CN UDP HEARTBEAT TO THE VM
# =========================================
#
# Upload to the module as /usr/heartbeat.py next to /usr/main.py.
#
# Every interval_s seconds the module sends one small UDP packet to the VM
# so the server knows it is alive and learns its current IPv6 address. It
# uses its own UDP socket, never the bridge connection, and is driven from
# the bridge main loop (no threads): call maybe(ipv6) often, send(ipv6) to
# force a beat now.
#
# Packet: one JSON line, then "|" and the hex HMAC-SHA256 of the JSON bytes
# when a key is loaded. Fields:
#   id         device id
#   seq        beat number since boot (starts at 1)
#   up         seconds since boot
#   ip         current IPv6 address
#   csq        signal quality 0..31 (99 = unknown)
#   client     1 if the VM is connected to the bridge now
#   meter_age  seconds since the meter last sent a byte (-1 = never)
#
# The key is NOT kept in code: upload it as key_file (one line, same text
# as the server's heartbeat key). Without it beats are sent unsigned, and
# the server shows them but will not update the module's address from them.
#
# Nothing here raises into the caller: a lost beat must not disturb the
# bridge.

import usocket
import net
import ubinascii
import uhashlib
import ujson
import utime


def hmac_sha256(key, msg):
    # QuecPython has no hmac module; standard HMAC (RFC 2104) on sha256
    if len(key) > 64:
        key = uhashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = uhashlib.sha256(bytes([b ^ 0x36 for b in key]) + msg).digest()
    return uhashlib.sha256(bytes([b ^ 0x5C for b in key]) + inner).digest()


def load_key(path):
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            print("Heartbeat key loaded")
            return key.encode()
    except Exception:
        pass
    print("No", path, "- heartbeats will be unsigned")
    return None


def read_csq():
    try:
        csq = net.csqQueryPoll()
    except Exception as e:
        print("Heartbeat csq error:", e)
        return 99
    return csq if isinstance(csq, int) else 99


class Heartbeat:

    def __init__(self, device_id, host, port, interval_s, key_file):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.interval_ms = interval_s * 1000
        self.key = load_key(key_file)
        self.sock = None
        self.sock_ip = None         # own IPv6 the socket is bound to
        self.seq = 0
        self.last = None            # ticks_ms of the last beat attempt
        self.boot_ms = utime.ticks_ms()
        self.client = False         # VM connected to the bridge now
        self.last_meter_rx = None   # ticks_ms of the last byte from the meter

    # --- status updates from the bridge ---

    def set_client(self, connected):
        self.client = bool(connected)

    def meter_rx(self):
        self.last_meter_rx = utime.ticks_ms()

    # --- sending ---

    def packet(self, ipv6):
        now = utime.ticks_ms()
        up = utime.ticks_diff(now, self.boot_ms) // 1000

        if self.last_meter_rx is None:
            meter_age = -1
        else:
            meter_age = utime.ticks_diff(now, self.last_meter_rx) // 1000

        payload = ujson.dumps({
            "id": self.device_id,
            "seq": self.seq,
            "up": up,
            "ip": ipv6,
            "csq": read_csq(),
            "client": 1 if self.client else 0,
            "meter_age": meter_age,
        }).encode()

        if self.key:
            sig = ubinascii.hexlify(hmac_sha256(self.key, payload))
            return payload + b"|" + sig
        return payload

    def send(self, ipv6):
        # The only pattern that works on the EC200U (heartbeat_route_test.py):
        # bind the UDP socket to the module's own IPv6 address, connect it,
        # then send(). Without the bind send() fails with EHOSTUNREACH, and
        # sendto() fails with "invalid arguments". The socket is reused for
        # every beat and rebuilt when the module's address changes.
        self.last = utime.ticks_ms()
        self.seq += 1
        step = "packet"

        try:
            data = self.packet(ipv6)

            if self.sock is not None and self.sock_ip != ipv6:
                self.close()

            if self.sock is None:
                step = "socket"
                self.sock = usocket.socket(usocket.AF_INET6, usocket.SOCK_DGRAM)
                step = "bind"
                self.sock.bind((ipv6, 0))
                self.sock_ip = ipv6
                step = "connect"
                self.sock.connect((self.host, self.port))

            step = "send"
            self.sock.send(data)
            print("[%d ms] HEARTBEAT #%d sent (%d bytes)" % (
                utime.ticks_ms(), self.seq, len(data)))

        except Exception as e:
            print("Heartbeat failed at %s: %s %s" % (step, type(e).__name__, e))
            self.close()

    def maybe(self, ipv6):
        # Send if a beat is due
        if self.last is None or utime.ticks_diff(
                utime.ticks_ms(), self.last) >= self.interval_ms:
            self.send(ipv6)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass
        self.sock = None
        self.sock_ip = None
