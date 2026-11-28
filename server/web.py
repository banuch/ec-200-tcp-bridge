"""
Web dashboard for server.py: devices online/offline, add/edit/delete
devices, "Read now", latest meter values, stored profile rows.

Plain asyncio + standard library (no web framework). One request per
connection. Every request needs HTTP basic auth; without a password the
dashboard does not start at all. HTTPS when a certificate and key exist.

It does not import server.py: server.py passes a Backend with the module
dict, the profile store and the functions to call.

API (JSON):
    GET    /api/devices
    POST   /api/devices                          {"name", "host", "port"}
    DELETE /api/devices/<name>
    POST   /api/devices/<name>/read
    POST   /api/devices/<name>/scan
    GET    /api/devices/<name>/reading
    GET    /api/devices/<name>/profiles
    POST   /api/devices/<name>/profiles/<obis>   {"enabled": true|false}
    GET    /api/devices/<name>/profiles/<obis>/rows?limit=50
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import os
import re
import ssl
import time
from urllib.parse import parse_qs, unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "web")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
HEADER_TIMEOUT = 10         # seconds to receive the request
BAD_PASSWORD_DELAY = 1.0    # seconds before answering a wrong password
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
OBIS_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){5}$")
REALM = "EC200U bridge"

STATUS_TEXT = {
    200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
    404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    500: "Internal Server Error",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"),
}


class Backend:
    """What the dashboard needs from server.py."""

    def __init__(self, modules, device_status, set_module, delete_module,
                 start_read, start_scan, store, module_port=4059):
        self.modules = modules
        self.device_status = device_status
        self.set_module = set_module
        self.delete_module = delete_module
        self.start_read = start_read
        self.start_scan = start_scan
        self.store = store
        self.module_port = module_port


class Response:
    def __init__(self, status, body=b"", content_type="application/json",
                 headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def json_response(status, payload):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return Response(status, body)


def error(status, message):
    return json_response(status, {"error": message})


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def check_auth(header, user, password):
    """True if the Authorization header carries user:password."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    given_user, sep, given_password = decoded.partition(":")
    if not sep:
        return False
    # Compare both parts every time (constant time, no early exit)
    ok_user = hmac.compare_digest(given_user.encode(), user.encode())
    ok_password = hmac.compare_digest(given_password.encode(), password.encode())
    return ok_user and ok_password


# ---------------------------------------------------------------------------
# Request handling (no sockets: tests call this directly)
# ---------------------------------------------------------------------------

class App:
    def __init__(self, backend, user, password, log=print):
        if not password:
            raise ValueError("the web dashboard needs a password")
        self.backend = backend
        self.user = user
        self.password = password
        self.log = log

    async def handle(self, method, target, headers, body, client="?"):
        """headers: dict with lower-case names. Returns a Response."""
        if not check_auth(headers.get("authorization"), self.user, self.password):
            if headers.get("authorization"):
                self.log("Web: wrong password from {}".format(client))
                await asyncio.sleep(BAD_PASSWORD_DELAY)
            return Response(
                401, b'{"error": "login required"}',
                headers={"WWW-Authenticate": 'Basic realm="{}", charset="UTF-8"'.format(REALM)})

        url = urlsplit(target)
        path = url.path
        query = parse_qs(url.query)

        if path in STATIC_FILES:
            if method != "GET":
                return error(405, "method not allowed")
            return self.static(path)

        if not path.startswith("/api/"):
            return error(404, "not found")

        data = None
        if method == "POST":
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return error(415, "send application/json")
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, UnicodeDecodeError):
                return error(400, "body is not valid JSON")
            if not isinstance(data, dict):
                return error(400, "body must be a JSON object")
        elif method not in ("GET", "DELETE"):
            return error(405, "method not allowed")

        parts = [unquote(p) for p in path[len("/api/"):].split("/") if p]
        try:
            return self.route(method, parts, query, data, client)
        except Exception as exc:            # never leak a traceback to the page
            self.log("Web: {} {} failed: {!r}".format(method, path, exc))
            return error(500, "internal error (see server log)")

    def static(self, path):
        filename, ctype = STATIC_FILES[path]
        try:
            with open(os.path.join(STATIC_DIR, filename), "rb") as f:
                return Response(200, f.read(), ctype)
        except OSError:
            return error(404, "{} is missing on the server".format(filename))

    # --- API routes ---

    def route(self, method, parts, query, data, client):
        b = self.backend
        if parts == ["devices"]:
            if method == "GET":
                return json_response(200, {
                    "now": time.time(),
                    "module_port": b.module_port,
                    "devices": [b.device_status(n) for n in sorted(b.modules)],
                })
            if method == "POST":
                return self.save_device(data, client)
            return error(405, "method not allowed")

        if len(parts) < 2 or parts[0] != "devices":
            return error(404, "not found")
        name = parts[1]
        if name not in b.modules:
            return error(404, "unknown device")
        rest = parts[2:]

        if rest == [] and method == "DELETE":
            b.delete_module(name)
            self.log("Web: {} deleted device {}".format(client, name))
            return json_response(200, {"deleted": name})

        if rest == ["read"] and method == "POST":
            why = b.start_read(name)
            if why:
                return error(409, why)
            self.log("Web: {} started a read of {}".format(client, name))
            return json_response(202, {"started": "read"})

        if rest == ["scan"] and method == "POST":
            why = b.start_scan(name)
            if why:
                return error(409, why)
            self.log("Web: {} started a profile scan of {}".format(client, name))
            return json_response(202, {"started": "profile scan"})

        if rest == ["reading"] and method == "GET":
            return json_response(200, {
                "latest": b.store.latest_reading(name),
                "history": b.store.recent_readings(name, 20),
            })

        if rest == ["profiles"] and method == "GET":
            return json_response(200, {"profiles": b.store.list_status(name)})

        if len(rest) >= 2 and rest[0] == "profiles":
            obis = rest[1]
            if not OBIS_RE.match(obis):
                return error(400, "bad OBIS code")
            if len(rest) == 2 and method == "POST":
                enabled = data.get("enabled")
                if not isinstance(enabled, bool):
                    return error(400, "enabled must be true or false")
                if not b.store.set_profile_enabled(name, obis, enabled):
                    return error(404, "unknown profile")
                self.log("Web: {} {} profile {} on {}".format(
                    client, "enabled" if enabled else "disabled", obis, name))
                return json_response(200, {"obis": obis, "enabled": enabled})
            if rest[2:] == ["rows"] and method == "GET":
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    return error(400, "limit must be a number")
                limit = max(1, min(limit, 500))
                return json_response(200, {
                    "columns": b.store.list_columns(name, obis),
                    "rows": b.store.list_rows(name, obis, limit),
                })

        return error(404, "not found")

    def save_device(self, data, client):
        name = str(data.get("name", "")).strip()
        host = str(data.get("host", "")).strip().strip("[]")
        port = data.get("port", self.backend.module_port)
        if not NAME_RE.match(name):
            return error(400, "name: 1-32 letters, digits, _ . -")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return error(400, "host must be an IPv6 address")
        if address.version != 6:
            return error(400, "host must be an IPv6 address (the modules are IPv6)")
        try:
            port = int(port)
        except (TypeError, ValueError):
            return error(400, "port must be a number")
        if not 1 <= port <= 65535:
            return error(400, "port must be 1-65535")
        existed = name in self.backend.modules
        self.backend.set_module(name, str(address), port)
        self.log("Web: {} {} device {} [{}]:{}".format(
            client, "changed" if existed else "added", name, address, port))
        return json_response(200, {"name": name, "host": str(address), "port": port})


# ---------------------------------------------------------------------------
# HTTP over asyncio streams
# ---------------------------------------------------------------------------

async def read_request(reader):
    """Returns (method, target, headers, body) or raises ValueError."""
    head = await reader.readuntil(b"\r\n\r\n")
    if len(head) > MAX_HEADER_BYTES:
        raise ValueError("headers too large")
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        method, target, _ = lines[0].split(" ", 2)
    except ValueError:
        raise ValueError("bad request line")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    if length < 0 or length > MAX_BODY_BYTES:
        raise OverflowError("body too large")
    body = await reader.readexactly(length) if length else b""
    return method.upper(), target, headers, body


def encode_response(response):
    status_line = "HTTP/1.1 {} {}\r\n".format(
        response.status, STATUS_TEXT.get(response.status, ""))
    headers = dict(SECURITY_HEADERS)
    headers.update({
        "Content-Type": response.content_type,
        "Content-Length": str(len(response.body)),
        "Connection": "close",
    })
    headers.update(response.headers)
    head = status_line + "".join("{}: {}\r\n".format(k, v) for k, v in headers.items())
    return head.encode("iso-8859-1") + b"\r\n" + response.body


async def serve_connection(app, reader, writer):
    peer = writer.get_extra_info("peername")
    client = peer[0] if peer else "?"
    try:
        try:
            method, target, headers, body = await asyncio.wait_for(
                read_request(reader), HEADER_TIMEOUT + 5)
            response = await app.handle(method, target, headers, body, client)
        except OverflowError:
            response = error(413, "request too large")
        except (ValueError, asyncio.LimitOverrunError):
            response = error(400, "bad request")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return
        writer.write(encode_response(response))
        await writer.drain()
    except (ConnectionError, ssl.SSLError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start(backend, host, port, user, password, cert_file, key_file, log):
    """Start the dashboard. Returns the asyncio server, or None if not started."""
    if not password:
        log("Web dashboard NOT started: no password. Put one in "
            "server/web_password.txt (or set BRIDGE_WEB_PASSWORD).")
        return None
    app = App(backend, user, password, log)

    context = None
    if os.path.exists(cert_file) and os.path.exists(key_file):
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert_file, key_file)

    server = await asyncio.start_server(
        lambda r, w: serve_connection(app, r, w), host, port,
        ssl=context, limit=MAX_HEADER_BYTES)
    scheme = "https" if context else "http"
    log("Web dashboard on {}://{}:{} (user {})".format(
        scheme, "[{}]".format(host) if host and ":" in host else (host or "*"),
        port, user))
    if context is None:
        log("Web dashboard WARNING: plain HTTP, the password is sent "
            "unencrypted. Add web_cert.pem + web_key.pem for HTTPS.")
    return server
