"""
UDP heartbeat listener for the EC200U modules (device/heartbeat.py).

Every module sends one small JSON packet every HEARTBEAT_INTERVAL seconds,
optionally followed by "|<hex HMAC-SHA256 of the JSON>". This module:

 - keeps the last beat of every device (id, seq, up, ip, csq, client,
   meter_age, plus at/src/signed/online),
 - marks a device offline after `missed` missed beats,
 - when a correctly signed, newer beat reports a new IPv6 address AND comes
   from that same address, updates the module list and saves it.

Unsigned beats are shown but never change an address; wrong signatures and
replayed seq values are dropped. The shared key is read from
BRIDGE_HB_KEY or the key file (never in git).

It does not import server.py: the server passes in its module dict and the
save/log functions.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import time

MAX_PACKET = 1024


def load_key(path):
    key = os.environ.get("BRIDGE_HB_KEY", "").strip()
    if not key and path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    return key.encode() or None


def same_ip(a, b):
    try:
        return (ipaddress.ip_address(str(a).split("%")[0])
                == ipaddress.ip_address(str(b).split("%")[0]))
    except ValueError:
        return False


class HeartbeatMonitor:

    def __init__(self, modules, save_modules, log, key=None,
                 interval=300, missed=3):
        self.modules = modules          # name -> {"host": ..., "port": ...}
        self.save_modules = save_modules
        self.log = log
        self.key = key
        self.interval = interval
        self.missed = missed
        self.beats = {}                 # device id -> last beat

    # --- packets ---

    def handle(self, data, src):
        """One UDP datagram from address src."""
        if len(data) > MAX_PACKET:
            return
        payload, sig = data, None
        if b"|" in data:
            payload, sig = data.rsplit(b"|", 1)
        try:
            beat = json.loads(payload.decode("utf-8"))
            name = beat["id"]
            if not isinstance(name, str):
                raise ValueError("id is not a string")
            seq, up = int(beat.get("seq", 0)), int(beat.get("up", 0))
        except (ValueError, KeyError, TypeError, AttributeError,
                UnicodeDecodeError) as error:
            self.log("Heartbeat from [{}] ignored: bad packet ({})".format(src, error))
            return

        signed = False
        if sig is not None and self.key:
            good = hmac.new(self.key, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(good.encode(), sig.strip().lower()):
                self.log("Heartbeat '{}' from [{}] ignored: wrong signature".format(
                    name, src))
                return
            signed = True

        prev = self.beats.get(name)
        rebooted = prev is not None and up < prev.get("up", 0)
        newer = prev is None or rebooted or seq > prev.get("seq", 0)
        if not newer:
            return                      # duplicate or replayed beat

        beat.update(at=time.time(), src=src, signed=signed, online=True)
        self.beats[name] = beat

        if prev is None or not prev.get("online"):
            self.log("Heartbeat: {} ONLINE from [{}]".format(name, src))
        elif rebooted:
            self.log("Heartbeat: {} rebooted (up {}s)".format(name, up))
        self.log("HEARTBEAT {} #{} up={}s csq={} client={} meter_age={} {}".format(
            name, seq, up, beat.get("csq"), beat.get("client"),
            beat.get("meter_age"), "signed" if signed else "UNSIGNED"))

        self._update_address(name, beat.get("ip"), src, signed)

    def _update_address(self, name, ip, src, signed):
        entry = self.modules.get(name)
        if entry is None:
            self.log("Heartbeat: {} is not a configured module "
                     "(setmodule {} {})".format(name, name, ip))
            return
        if not ip or same_ip(entry["host"], ip):
            return
        if not signed:
            self.log("Heartbeat: {} reports IPv6 {} (configured {}); unsigned, so "
                     "not changed: run setmodule {} {}".format(
                         name, ip, entry["host"], name, ip))
        elif not same_ip(src, ip):
            self.log("Heartbeat: {} reports IPv6 {} but came from [{}]; "
                     "not changed".format(name, ip, src))
        else:
            old = entry["host"]
            entry["host"] = str(ip).strip("[]")
            self.save_modules()
            self.log("Module {} IPv6 changed {} -> {} (from heartbeat, saved)".format(
                name, old, entry["host"]))

    # --- asyncio ---

    async def start(self, host="::", port=4060):
        """Open the UDP listener; returns the transport."""
        monitor = self

        class Protocol(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                try:
                    monitor.handle(data, addr[0])
                except Exception as error:  # one packet never stops the listener
                    monitor.log("Heartbeat from {} failed: {!r}".format(addr, error))

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Protocol, local_addr=(host, port), family=socket.AF_INET6)
        self.log("Heartbeat listening on UDP [{}]:{} ({})".format(
            host, port,
            "key loaded" if self.key else "NO KEY: beats accepted unsigned, "
            "addresses never auto-updated"))
        return transport

    async def watch(self, every=30):
        """Mark devices offline after `missed` missed beats (runs forever)."""
        while True:
            await asyncio.sleep(every)
            self.check_offline()

    def check_offline(self, now=None):
        now = time.time() if now is None else now
        limit = self.interval * self.missed
        for name, beat in self.beats.items():
            if beat.get("online") and now - beat["at"] > limit:
                beat["online"] = False
                self.log("Heartbeat: {} OFFLINE (no beat for {}s)".format(
                    name, int(now - beat["at"])))

    # --- console ---

    def state(self, name):
        beat = self.beats.get(name)
        if beat is None:
            return "no heartbeat"
        return "{} (beat {}s ago)".format(
            "live" if beat.get("online") else "OFFLINE",
            int(time.time() - beat["at"]))

    def print_all(self):
        if not self.beats:
            print("(no heartbeat received yet)")
        for name, beat in self.beats.items():
            print("  {:<14} {}  #{} up={}s ip={} csq={} client={} meter_age={} {}".format(
                name, self.state(name), beat.get("seq"), beat.get("up"),
                beat.get("ip"), beat.get("csq"), beat.get("client"),
                beat.get("meter_age"), "signed" if beat.get("signed") else "UNSIGNED"))
