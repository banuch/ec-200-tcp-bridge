"""Break into the QuecPython REPL of a module stuck in a restart loop.

Run on the PC:   python tools/rescue_repl.py [COM32] [seconds]

The USB REPL port only exists for a few seconds per boot when /usr/main.py
restarts the module. This script waits for the port, opens it the moment it
appears, sends Ctrl+C until main.py is interrupted, then renames
/usr/main.py to /usr/main_disabled.py so the next boot stops at the REPL.
Nothing else on the module is changed.
"""
import sys
import time

import serial
from serial.tools import list_ports

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM32"
TOTAL_SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 180
BAUD = 115200
PROMPT = b">>> "


def repl_port():
    """Prefer a port that Windows labels as the QuecPython REPL."""
    for info in list_ports.comports():
        if "REPL" in (info.description or "").upper():
            return info.device
    return PORT


def read_for(link, seconds):
    data = b""
    end = time.time() + seconds
    while time.time() < end:
        data += link.read(4096)
    return data


def command(link, line, wait=1.0):
    link.write(line.encode() + b"\r\n")
    reply = read_for(link, wait)
    print(reply.decode("utf-8", "replace").strip())
    return reply


def try_rescue(link):
    """Interrupt main.py on an open port; True once it has been renamed."""
    seen = b""
    end = time.time() + 20
    while time.time() < end:
        link.write(b"\x03")
        seen += link.read(4096)
        if not seen.rstrip(b" ").endswith(PROMPT.rstrip(b" ")):
            continue

        # Confirm a live prompt, not one about to disappear in a restart.
        link.reset_input_buffer()
        if b"4242" not in command(link, "print(42 * 101)", 0.5):
            seen = b""
            continue

        print("--- REPL reached; disabling /usr/main.py ---")
        command(link, "import uos")
        command(link, "uos.rename('/usr/main.py', '/usr/main_disabled.py')")
        listing = command(link, "print('USR', uos.listdir('/usr'))")
        if b"USR" in listing and b"'main.py'" not in listing:
            return True
        print("main.py is still listed; trying to remove it instead")
        command(link, "uos.remove('/usr/main.py')")
        listing = command(link, "print('USR', uos.listdir('/usr'))")
        return b"USR" in listing and b"'main.py'" not in listing

    text = seen.decode("utf-8", "replace").strip()
    if text:
        print("No prompt yet. Module said:\n" + text[-600:])
    return False


def main():
    deadline = time.time() + TOTAL_SECONDS
    print("Waiting up to %d s for the REPL port (default %s)..." % (
        TOTAL_SECONDS, PORT))
    seen_ports = set()

    while time.time() < deadline:
        for info in list_ports.comports():
            if info.device not in seen_ports:
                seen_ports.add(info.device)
                print("Port seen: %s  %s" % (info.device, info.description))

        try:
            link = serial.Serial(repl_port(), BAUD, timeout=0.05)
        except (serial.SerialException, OSError):
            time.sleep(0.05)
            continue

        print("Opened", link.port)
        try:
            if try_rescue(link):
                print("DONE: main.py is disabled. The module will now stay "
                      "at the REPL after a reset.")
                return 0
        except (serial.SerialException, OSError) as error:
            print("Port dropped (%s); waiting for the next boot" % error)
        finally:
            try:
                link.close()
            except Exception:
                pass

    print("FAILED: no REPL prompt within %d s." % TOTAL_SECONDS)
    return 1


sys.exit(main())
