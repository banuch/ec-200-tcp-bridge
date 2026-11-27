"""
link_test.py - find out why the EC200U TCP link resets during a meter read.

Every full DLMS read so far died the same way: SNRM, AARQ and the first
GET (serial number) work, then the next request (GET 0.0.96.1.1.255) makes
the link reset on BOTH sides (server: ConnectionResetError, module:
ECONNRESET in recv) before the module ever sees the request.

This script replays exact frames captured from those reads, each test on a
fresh connection, and reports at which step the link died. Comparing the
tests separates the possible causes:

    count       SNRM 8 times                 -> number of replies the module sends?
    replay      SNRM AARQ GET1 GET2 DISC     -> reproduces the failure (baseline)
    get1-twice  SNRM AARQ GET1 GET1 DISC     -> 4th request, but known-good bytes
    get2-third  SNRM AARQ GET2 DISC          -> the GET2 bytes, one step earlier
    get2-second SNRM GET2                    -> the GET2 bytes right after SNRM

The meter may refuse an out-of-sequence frame (no reply). That is fine: the
question is only whether the TCP link survives.

Run on the VM, with server.py STOPPED (the module takes one client at a time
and server.py would also connect to it):

    python3 link_test.py                 # all tests
    python3 link_test.py count replay    # just these
    python3 link_test.py --host 2401:4900:9864:a48c::2 --port 4059

The module address is read from modules.json (first module) unless --host
is given.
"""
import argparse
import binascii
import json
import os
import socket
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES_FILE = os.environ.get("BRIDGE_MODULES", os.path.join(HERE, "modules.json"))
DEFAULT_HOST = "2401:4900:9864:a48c::2"
DEFAULT_PORT = 4059

REPLY_TIMEOUT = 6.0     # module waits 4 s for the meter, plus network time
REQUEST_GAP = 0.5       # pause after a reply before the next request
FINAL_WAIT = 3.0        # watch for a late reset after the last frame
BETWEEN_TESTS = 5.0     # let the module go back to accept()

# Frames copied from the failing reads (client 0x20 -> server 0x03, 0x41)
FRAMES = {
    "SNRM": "7EA0070341935A647E",
    "AARQ": "7EA0400341105F93E6E6006032A1090607608574050801018A0207808B07608574"
            "05080201AC0680046C6E7431BE10040E01000000065F1F0400401E5DFFFF6DE37E",
    "GET1": "7EA0190341323ABDE6E600C001C100010000600100FF020089A07E",  # 0.0.96.1.0.255
    "GET2": "7EA0190341540ABBE6E600C001C100010000600101FF020032BC7E",  # 0.0.96.1.1.255
    "DISC": "7EA00703415356A27E",
}
FRAMES = {name: bytes.fromhex(text.replace(" ", "")) for name, text in FRAMES.items()}

TESTS = {
    "count": ["SNRM"] * 8,
    "replay": ["SNRM", "AARQ", "GET1", "GET2", "DISC"],
    "get1-twice": ["SNRM", "AARQ", "GET1", "GET1", "DISC"],
    "get2-third": ["SNRM", "AARQ", "GET2", "DISC"],
    "get2-second": ["SNRM", "GET2"],
}


class LinkDead(Exception):
    pass


started = time.monotonic()


def say(text):
    print("[{:8.3f}] {}".format(time.monotonic() - started, text), flush=True)


def hexs(data):
    return binascii.hexlify(data, " ").decode().upper()


def frame_complete(buf):
    """True once buf holds a whole HDLC frame (length from the format field)."""
    start = buf.find(b"\x7e")
    if start < 0 or len(buf) - start < 3:
        return False
    length = ((buf[start + 1] & 0x07) << 8) | buf[start + 2]
    return len(buf) - start >= length + 2


def recv_reply(sock, timeout):
    """Collect one reply. Returns bytes (b'' if nothing came); raises LinkDead."""
    sock.settimeout(timeout)
    buf = b""
    deadline = time.monotonic() + timeout
    while not frame_complete(buf):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            chunk = sock.recv(2048)
        except socket.timeout:
            break
        except OSError as error:
            raise LinkDead("recv: {!r}".format(error))
        if not chunk:
            raise LinkDead("module closed the connection (EOF)")
        buf += chunk
    return buf


def run_test(name, host, port):
    steps = TESTS[name]
    say("=== TEST {}: {} ===".format(name, " ".join(steps)))
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect((host, port))
    except OSError as error:
        say("connect failed: {!r}".format(error))
        sock.close()
        return "connect failed"
    say("connected to [{}]:{}".format(host, port))
    time.sleep(1.0)

    replies = 0
    result = None
    try:
        for index, step in enumerate(steps, 1):
            frame = FRAMES[step]
            time.sleep(REQUEST_GAP)
            say("step {} TX {} ({} bytes, {} replies so far): {}".format(
                index, step, len(frame), replies, hexs(frame)))
            sent_at = time.monotonic()
            try:
                sock.sendall(frame)
            except OSError as error:
                raise LinkDead("send: {!r}".format(error))
            try:
                reply = recv_reply(sock, REPLY_TIMEOUT)
            except LinkDead as dead:
                raise LinkDead("{} ({:.0f} ms after step {} {})".format(
                    dead, (time.monotonic() - sent_at) * 1000, index, step))
            if reply:
                replies += 1
                say("step {} RX {} bytes: {}".format(index, len(reply), hexs(reply)))
            else:
                say("step {} no reply in {:.0f}s (link still up)".format(index, REPLY_TIMEOUT))

        # A reset can arrive after the last reply; wait a moment for it
        try:
            late = recv_reply(sock, FINAL_WAIT)
            if late:
                say("late RX {} bytes: {}".format(len(late), hexs(late)))
        except LinkDead as dead:
            raise LinkDead("{} (after the last step)".format(dead))
        result = "OK: all {} steps, {} replies, link stayed up".format(len(steps), replies)
    except LinkDead as dead:
        result = "RESET at {}".format(dead)
    finally:
        sock.close()
    say("RESULT {}: {}".format(name, result))
    return result


def module_address(args):
    if args.host:
        return args.host.strip("[]"), args.port or DEFAULT_PORT
    if os.path.exists(MODULES_FILE):
        with open(MODULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data:
            entry = next(iter(data.values()))
            return str(entry["host"]).strip("[]"), args.port or int(entry.get("port", DEFAULT_PORT))
    return DEFAULT_HOST, args.port or DEFAULT_PORT


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("tests", nargs="*",
                        help="tests to run: {} (default: all)".format(", ".join(TESTS)))
    parser.add_argument("--host", help="module IPv6 address")
    parser.add_argument("--port", type=int, help="module TCP port")
    args = parser.parse_args()
    unknown = [name for name in args.tests if name not in TESTS]
    if unknown:
        parser.error("unknown test(s): {}".format(", ".join(unknown)))

    host, port = module_address(args)
    names = args.tests or list(TESTS)
    say("module [{}]:{}  tests: {}".format(host, port, ", ".join(names)))
    say("server.py must be stopped while this runs")

    results = []
    for number, name in enumerate(names):
        if number:
            time.sleep(BETWEEN_TESTS)
        results.append((name, run_test(name, host, port)))

    print("\n=== SUMMARY ===")
    for name, result in results:
        print("  {:<12} {:<34} {}".format(name, " ".join(TESTS[name]), result))


if __name__ == "__main__":
    main()
