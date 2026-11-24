"""
server.py - DLMS reader for EC200U bridges running in TCP-server mode.

Run on your PC / VM:   python server.py
Needs Python 3.8+ and gurux-dlms (pip install -r requirements.txt).

What it does
 - Each EC200U listens on its own IPv6 address, TCP port 5000
   (device/main.py). This server CONNECTS OUT to every configured module.
 - Modules are listed in modules.json next to this file:
       {"EC200U_001": {"host": "2401:4900:9864:a48c::2", "port": 5000}}
   The module prints its IPv6 address at boot ("IPv6: ..."). When the
   operator gives it a new address, update it with the "setmodule" command
   (saved to modules.json) - no restart needed.
 - Every READ_INTERVAL seconds per module: connect, run the DLMS read
   (identity, values and bounded profile collection), disconnect.
   A failed connect retries after CONNECT_RETRY, a failed read after
   AUTO_READ_RETRY.
 - Logs every chunk received from a meter (hex + ASCII) to the console and
   bridge_log.txt.
 - PC relay on PC_PORT: a PC tool (Gurux Director: Media Net, TCP, host =
   this server, port = PC_PORT, interface HDLC) is piped byte-for-byte to one
   module. The target is the module chosen with "pipe <name>", else the
   module connected last, else the only configured module. The server opens
   the connection to the module if it is not open yet.
 - Console commands:

       modules                       configured modules and their state
       setmodule <name> <ipv6> [port]  add a module or change its address
       delmodule <name>              remove a module
       connect <name>                open a link (for send / read testing)
       poll <name>                   run one scheduled read now
       list                          open links (ids used below)
       send <id> <hex bytes>         e.g.  send 1 7EA00A00020023219349E27E
       sendtext <id> <text>          send plain text (\\r \\n escapes allowed)
       read <id>                     DLMS: read meter details on an open link
       pipe <name>|off               route the next PC connection
       kick <id>                     close a link
       quit                          stop the server

Meter addresses / password live in dlms_reader.py.
"""
import asyncio
import binascii
import json
import os
import socket
import sys
import time

import dlms_reader
from profile_store import ProfileStore

HERE = os.path.dirname(os.path.abspath(__file__))

MODULE_PORT = 5000          # default port the EC200U listens on (device PORT)
MODULES_FILE = os.environ.get("BRIDGE_MODULES", os.path.join(HERE, "modules.json"))
DEFAULT_MODULES = {
    # Used only when modules.json does not exist yet.
    "EC200U_001": {"host": "2401:4900:9864:a48c::2", "port": MODULE_PORT},
}

PC_HOST = "::"              # PC relay listens on all IPv6 interfaces
PC_PORT = 4059              # PC tools connect here to reach a module
PC_ALLOW = []               # PC IPs allowed on PC_PORT, e.g. ["1.2.3.4"]; [] = any

CONNECT_TIMEOUT = 20        # seconds to wait for the module to accept
IDLE_TIMEOUT = 600          # seconds without data before dropping a link / PC
READ_INTERVAL = 15 * 60     # seconds between scheduled reads of a module
CONNECT_RETRY = 60          # retry after the module could not be reached
AUTO_READ_RETRY = 60        # retry after an unsuccessful meter read
AUTO_READ_DELAY = 2         # seconds after connecting before DLMS
STARTUP_STAGGER = 5         # seconds between first reads of several modules
RELAY_TRACE = True          # log relayed bytes in hex
LOG_FILE = "bridge_log.txt"
PROFILE_COLLECTION_ENABLED = True
PROFILE_DB_FILE = os.environ.get(
    "BRIDGE_PROFILE_DB", os.path.join(HERE, "bridge_data.sqlite3"))

modules = {}                # name -> {"host": ..., "port": ...}
clients = {}                # link id -> (reader, writer, addr)
names = {}                  # link id -> module name
sessions = {}               # link id -> MeterSession while a DLMS read is running
pc_links = {}               # link id -> writer of the PC piped to it
pipe_target = None          # module name for the next PC, None = automatic
schedulers = {}             # module name -> scheduler task
link_locks = {}             # module name -> asyncio.Lock (one connect at a time)
closing = set()             # link ids this server is closing itself
tasks = set()               # running background tasks (keeps references alive)
next_id = 1
profile_store = ProfileStore(PROFILE_DB_FILE)


def log(line):
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + ".%03d" % (
        int(now * 1000) % 1000)
    text = "[{}] {}".format(stamp, line)
    print(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def _hex(data):
    return binascii.hexlify(data, " ").decode().upper()


def label(cid):
    name = names.get(cid)
    return "#{} ({})".format(cid, name) if name else "#{}".format(cid)


def device_key(cid):
    """Storage key for profile data: the module name."""
    return names.get(cid) or "connection-{}".format(cid)


def start_task(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


# ---------------------------------------------------------------------------
# Module list (modules.json)
# ---------------------------------------------------------------------------

def load_modules():
    if not os.path.exists(MODULES_FILE):
        modules.update(DEFAULT_MODULES)
        save_modules()
        log("Created {} with the default module list".format(MODULES_FILE))
        return
    with open(MODULES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    for name, entry in data.items():
        modules[name] = {
            "host": str(entry["host"]).strip("[]"),
            "port": int(entry.get("port", MODULE_PORT)),
        }


def save_modules():
    tmp = MODULES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(modules, f, indent=2)
    os.replace(tmp, MODULES_FILE)


def live_cid(name):
    """Open link id for a module name, or None."""
    for cid, n in names.items():
        if n == name and cid in clients:
            return cid
    return None


# ---------------------------------------------------------------------------
# Links to modules (the server is the TCP client)
# ---------------------------------------------------------------------------

def handle_data(cid, addr, data):
    """Called for every chunk a meter sends outside a DLMS read or PC pipe."""
    hexs = binascii.hexlify(data, " ").decode().upper()
    ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    log("RX #{} {} ({} bytes)\n    HEX  : {}\n    ASCII: {}".format(
        cid, addr, len(data), hexs, ascii_))


async def open_link(name):
    """Connect to a module (or reuse an open link); return the link id."""
    lock = link_locks.setdefault(name, asyncio.Lock())
    async with lock:
        cid = live_cid(name)
        if cid is not None:
            return cid
        entry = modules.get(name)
        if not entry:
            raise ValueError("unknown module {}".format(name))

        global next_id
        log("Connecting to module {} at [{}]:{}".format(
            name, entry["host"], entry["port"]))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(entry["host"], entry["port"],
                                    family=socket.AF_INET6),
            CONNECT_TIMEOUT)

        cid = next_id
        next_id += 1
        addr = writer.get_extra_info("peername")
        clients[cid] = (reader, writer, addr)
        names[cid] = name
        log("Module {} connected at {}".format(label(cid), addr))
        start_task(module_reader(cid, reader, writer, addr))
        return cid


def close_link(cid):
    entry = clients.get(cid)
    if entry:
        closing.add(cid)
        entry[1].close()


async def module_reader(cid, reader, writer, addr):
    """Route everything the module sends until the link closes."""
    reason = "unknown"
    try:
        while True:
            data = await asyncio.wait_for(reader.read(2048), IDLE_TIMEOUT)
            if not data:
                if cid in closing:
                    reason = "closed by this server"
                else:
                    reason = "module closed it (EOF/FIN)"
                break
            pc = pc_links.get(cid)
            session = sessions.get(cid)
            if pc:
                if RELAY_TRACE:
                    log("METER->PC {} ({} bytes): {}".format(label(cid), len(data), _hex(data)))
                pc.write(data)
                try:
                    await pc.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            elif session:
                session.feed(data)
            else:
                handle_data(cid, addr, data)
    except asyncio.TimeoutError:
        reason = "idle for {}s".format(IDLE_TIMEOUT)
    except (ConnectionResetError, BrokenPipeError, OSError) as error:
        reason = "socket error {!r}".format(error)
    except asyncio.CancelledError:
        reason = "closed by this server"
        raise
    finally:
        session = sessions.get(cid)
        if session:
            session.feed(None)          # abort a running read
        pc = pc_links.pop(cid, None)
        if pc:
            pc.close()                  # the PC sees the meter link drop
        clients.pop(cid, None)
        writer.close()
        log("Link {} closed: {}".format(label(cid), reason))
        names.pop(cid, None)
        closing.discard(cid)


# ---------------------------------------------------------------------------
# Scheduled reads
# ---------------------------------------------------------------------------

async def poll_once(name):
    """Connect, read the meter, disconnect. Returns seconds until next try."""
    if name not in modules:
        return None
    try:
        cid = await open_link(name)
    except (OSError, asyncio.TimeoutError, ValueError) as error:
        log("Module {} not reachable: {!r}; retrying in {}s".format(
            name, error, CONNECT_RETRY))
        return CONNECT_RETRY

    if cid in pc_links or cid in sessions:
        log("Module {} busy (PC or read running); retrying in {}s".format(
            label(cid), AUTO_READ_RETRY))
        return AUTO_READ_RETRY

    await asyncio.sleep(AUTO_READ_DELAY)
    success = await read_meter(cid)
    close_link(cid)

    if success:
        log("Scheduled read {} complete; next in {}s".format(name, READ_INTERVAL))
        return READ_INTERVAL
    log("Scheduled read {} unsuccessful; retrying in {}s".format(
        name, AUTO_READ_RETRY))
    return AUTO_READ_RETRY


async def scheduler(name, first_delay):
    try:
        await asyncio.sleep(first_delay)
        while name in modules:
            delay = await poll_once(name)
            if delay is None:
                return
            await asyncio.sleep(delay)
    except asyncio.CancelledError:
        pass
    finally:
        if schedulers.get(name) is asyncio.current_task():
            schedulers.pop(name, None)


def start_scheduler(name, first_delay=0):
    if name not in schedulers:
        schedulers[name] = start_task(scheduler(name, first_delay))


# ---------------------------------------------------------------------------
# PC relay
# ---------------------------------------------------------------------------

def pick_module_name():
    """Module for a new PC connection, or None."""
    if pipe_target is not None:
        return pipe_target
    if clients:
        return names.get(max(clients))
    if len(modules) == 1:
        return next(iter(modules))
    return None


async def handle_pc(reader, writer):
    addr = writer.get_extra_info("peername")
    if PC_ALLOW and addr[0] not in PC_ALLOW:
        log("PC {} refused: not in PC_ALLOW".format(addr))
        writer.close()
        return

    name = pick_module_name()
    if name is None:
        log("PC {} refused: several modules configured; use pipe <name>".format(addr))
        writer.close()
        return

    opened_here = live_cid(name) is None
    try:
        cid = await open_link(name)
    except (OSError, asyncio.TimeoutError, ValueError) as error:
        log("PC {} refused: module {} not reachable: {!r}".format(addr, name, error))
        writer.close()
        return

    reason = None
    if cid in pc_links:
        reason = "module {} already has a PC".format(label(cid))
    elif cid in sessions:
        reason = "a DLMS read is running on module {}".format(label(cid))
    if reason:
        log("PC {} refused: {}".format(addr, reason))
        writer.close()
        return

    pc_links[cid] = writer
    module_writer = clients[cid][1]
    log("PC {} piped to module {}".format(addr, label(cid)))
    try:
        while True:
            data = await asyncio.wait_for(reader.read(2048), IDLE_TIMEOUT)
            if not data or cid not in clients:
                break
            if RELAY_TRACE:
                log("PC->METER {} ({} bytes): {}".format(label(cid), len(data), _hex(data)))
            module_writer.write(data)
            await module_writer.drain()
    except asyncio.TimeoutError:
        log("PC {} idle for {}s, dropping".format(addr, IDLE_TIMEOUT))
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        if pc_links.get(cid) is writer:
            del pc_links[cid]
        writer.close()
        log("PC {} disconnected".format(addr))
        if opened_here:
            close_link(cid)


# ---------------------------------------------------------------------------
# DLMS operations on an open link
# ---------------------------------------------------------------------------

async def send_to(cid, payload):
    entry = clients.get(cid)
    if not entry:
        print("No such link:", cid)
        return
    _, writer, addr = entry
    writer.write(payload)
    await writer.drain()
    log("TX #{} {} ({} bytes): {}".format(
        cid, addr, len(payload), binascii.hexlify(payload, " ").decode().upper()))


async def read_meter(cid):
    entry = clients.get(cid)
    if not entry:
        print("No such link:", cid)
        return False
    if cid in sessions:
        print("A read is already running on link", cid)
        return False
    if cid in pc_links:
        print("A PC is piped to link", cid)
        return False
    session = dlms_reader.MeterSession(cid, entry[1], log)
    sessions[cid] = session
    log("DLMS {}: reading meter...".format(label(cid)))
    success = False
    try:
        results, profiles = await dlms_reader.read_cycle(
            session,
            profile_store,
            device_key(cid),
            include_profiles=PROFILE_COLLECTION_ENABLED,
        )
        log(dlms_reader.format_results(cid, results))
        for result in profiles:
            if result["status"] == "complete":
                log("PROFILE {} {}: read={}, new={}".format(
                    label(cid), result["obis"], result["rows_read"],
                    result["rows_inserted"]))
            else:
                log("PROFILE {} {}: {} ({})".format(
                    label(cid), result["obis"], result["status"],
                    result.get("error", "no details")))
        success = True
    except dlms_reader.LinkClosed:
        log("DLMS {}: module disconnected during read".format(label(cid)))
    except Exception as e:
        log("DLMS {}: read failed: {!r}".format(label(cid), e))
    finally:
        sessions.pop(cid, None)
    return success


async def scan_meter_profiles(cid):
    entry = clients.get(cid)
    if not entry:
        print("No such link:", cid)
        return
    if cid in sessions or cid in pc_links:
        print("Link {} is busy".format(cid))
        return
    session = dlms_reader.MeterSession(cid, entry[1], log)
    sessions[cid] = session
    try:
        profiles, _ = await dlms_reader.scan_profiles(
            session, profile_store, device_key(cid)
        )
        log("Association View {}: {} Profile Generic object(s)".format(
            label(cid), len(profiles)))
        for profile in profiles:
            log("  {} {:<8} enabled={}".format(
                profile["obis"], profile["profile_type"], bool(profile["enabled"])))
    except Exception as error:
        log("Association View {} failed: {!r}".format(label(cid), error))
    finally:
        sessions.pop(cid, None)


async def read_meter_profile(cid, obis, mode, values):
    entry = clients.get(cid)
    if not entry:
        print("No such link:", cid)
        return
    if cid in sessions or cid in pc_links:
        print("Link {} is busy".format(cid))
        return
    session = dlms_reader.MeterSession(cid, entry[1], log)
    sessions[cid] = session
    try:
        result = await dlms_reader.read_profile_manual(
            session, profile_store, device_key(cid), obis, mode, values
        )
        log("PROFILE {} {}: read={}, new={}".format(
            label(cid), obis, result["rows_read"], result["rows_inserted"]))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as error:
        log("PROFILE {} {} failed: {!r}".format(label(cid), obis, error))
    finally:
        sessions.pop(cid, None)


async def connect_command(name):
    try:
        cid = await open_link(name)
        print("Module {} open as link {}".format(name, cid))
    except (OSError, asyncio.TimeoutError, ValueError) as error:
        log("Connect {} failed: {!r}".format(name, error))


async def poll_command(name):
    await poll_once(name)


# ---------------------------------------------------------------------------
# Console
# ---------------------------------------------------------------------------

def print_modules():
    if not modules:
        print("(no modules configured; use setmodule <name> <ipv6>)")
    for name, entry in modules.items():
        cid = live_cid(name)
        state = "link {}".format(cid) if cid is not None else "not connected"
        print("  {:<14} [{}]:{}  {}".format(name, entry["host"], entry["port"], state))


async def console():
    global pipe_target
    loop = asyncio.get_running_loop()
    print("Commands: modules | setmodule <name> <ipv6> [port] | delmodule <name> | "
          "connect <name> | poll <name> | list | send <id> <hex> | "
          "sendtext <id> <text> | read <id> | objects <id> | profiles <id> | "
          "profile-status <id> | readprofile <id> <obis> last <count> | "
          "readprofile <id> <obis> entry <start> <count> | "
          "readprofile <id> <obis> range <from> <to> | "
          "enableprofile|disableprofile <id> <obis> | "
          "pipe <name>|off | kick <id> | autoread on|off | quit")
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(1)
            continue
        parts = line.strip().split(None, 2)
        args = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()
        try:
            if cmd == "modules":
                print_modules()
            elif cmd == "setmodule" and len(args) in (3, 4):
                name = args[1]
                port = int(args[3]) if len(args) == 4 else MODULE_PORT
                modules[name] = {"host": args[2].strip("[]"), "port": port}
                save_modules()
                print("Module {} -> [{}]:{} (saved)".format(name, modules[name]["host"], port))
                start_scheduler(name)
            elif cmd == "delmodule" and len(args) == 2:
                if modules.pop(args[1], None) is None:
                    print("Unknown module", args[1])
                else:
                    save_modules()
                    task = schedulers.pop(args[1], None)
                    if task:
                        task.cancel()
                    print("Removed", args[1])
            elif cmd == "connect" and len(args) == 2:
                start_task(connect_command(args[1]))
            elif cmd == "poll" and len(args) == 2:
                if args[1] not in modules:
                    print("Unknown module", args[1])
                else:
                    start_task(poll_command(args[1]))
            elif cmd == "list":
                if not clients:
                    print("(no open links)")
                for cid, (_, _, addr) in clients.items():
                    pc = pc_links.get(cid)
                    print("  {}  {}{}".format(label(cid), addr, "  <- PC {}".format(
                        pc.get_extra_info("peername")) if pc else ""))
                print("PC target: {}".format(pipe_target or "automatic"))
            elif cmd == "pipe" and len(args) == 2:
                pipe_target = None if args[1].lower() == "off" else args[1]
                if pipe_target and pipe_target not in modules:
                    print("Warning: {} is not a configured module".format(pipe_target))
                print("Next PC goes to", pipe_target or "the automatic choice")
            elif cmd == "send" and len(parts) == 3:
                payload = binascii.unhexlify(parts[2].replace(" ", ""))
                await send_to(int(parts[1]), payload)
            elif cmd == "sendtext" and len(parts) == 3:
                text = parts[2].encode().decode("unicode_escape").encode()
                await send_to(int(parts[1]), text)
            elif cmd == "read" and len(parts) >= 2:
                start_task(read_meter(int(parts[1])))
            elif cmd in ("objects", "profiles") and len(args) == 2:
                start_task(scan_meter_profiles(int(args[1])))
            elif cmd == "profile-status" and len(args) == 2:
                cid = int(args[1])
                status = profile_store.list_status(device_key(cid))
                if not status:
                    print("(no cached profiles; run profiles {})".format(cid))
                for item in status:
                    print("  {obis}  {profile_type:<8} enabled={enabled} "
                          "status={status} entries={entries_in_use} last={last_capture_at}".format(
                              **item))
            elif cmd == "readprofile" and len(args) >= 5:
                cid = int(args[1])
                mode = args[3].lower()
                expected = 5 if mode == "last" else 6
                if len(args) != expected or mode not in ("last", "entry", "range"):
                    raise ValueError("invalid readprofile arguments")
                start_task(read_meter_profile(cid, args[2], mode, args[4:]))
            elif cmd in ("enableprofile", "disableprofile") and len(args) == 3:
                cid = int(args[1])
                enabled = cmd == "enableprofile"
                if profile_store.set_profile_enabled(device_key(cid), args[2], enabled):
                    print("{} {} for {}".format(
                        "Enabled" if enabled else "Disabled", args[2], label(cid)))
                else:
                    print("Unknown cached profile; run profiles", cid)
            elif cmd == "autoread" and len(args) == 2 and args[1].lower() in ("on", "off"):
                # off: stop scheduled reads so manual send/read tests are not
                # interrupted; on: restart them for every configured module.
                if args[1].lower() == "off":
                    for task in list(schedulers.values()):
                        task.cancel()
                    schedulers.clear()
                    print("Scheduled reads OFF")
                else:
                    for name in modules:
                        start_scheduler(name)
                    print("Scheduled reads ON")
            elif cmd == "kick" and len(parts) >= 2:
                close_link(int(parts[1]))
            elif cmd == "quit":
                for _, w, _ in list(clients.values()):
                    w.close()
                asyncio.get_running_loop().stop()
                return
            else:
                print("Unknown or incomplete command")
        except (ValueError, binascii.Error) as e:
            print("Bad input:", e)


async def main():
    load_modules()
    pc_server = await asyncio.start_server(
        handle_pc, PC_HOST, PC_PORT, family=socket.AF_INET6
    )
    log("Modules: {}".format(", ".join(
        "{} [{}]:{}".format(n, e["host"], e["port"]) for n, e in modules.items())
        or "none"))
    log("PC relay listening on [{}]:{}".format(PC_HOST, PC_PORT))
    for index, name in enumerate(list(modules)):
        start_scheduler(name, index * STARTUP_STAGGER)
    async with pc_server:
        await asyncio.gather(pc_server.serve_forever(), console())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, RuntimeError):
        print("\nStopped.")
    finally:
        profile_store.close()
