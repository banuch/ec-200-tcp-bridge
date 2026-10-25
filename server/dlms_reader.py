"""
dlms_reader.py - read basic details from a DLMS/COSEM meter through the bridge.

Gurux only builds request bytes and parses reply bytes; MeterSession moves
those bytes over the module's TCP connection (server.py feeds received data
in via feed()).

Session flow:  SNRM -> UA  |  AARQ -> AARE (LLS)  |  GET ...  |  RLRQ, DISC

Settings below mirror Gurux Director: Indian Standard, HDLC, LN referencing,
Authentication MR (= LOW), client 0x20, logical server 0, physical server 1.
"""
import asyncio
import binascii
import json
from datetime import datetime, timedelta, timezone

from gurux_dlms import GXByteBuffer, GXDLMSClient, GXDLMSException, GXReplyData
from gurux_dlms.enums import Authentication, InterfaceType, Standard, Unit
from gurux_dlms.objects import (
    GXDLMSClock,
    GXDLMSData,
    GXDLMSDemandRegister,
    GXDLMSProfileGeneric,
    GXDLMSRegister,
)

# ---------------- meter settings ----------------
CLIENT_ADDRESS = 0x20         # 32 = Meter Reader (MR) association
LOGICAL_SERVER = 0
PHYSICAL_SERVER = 1
AUTHENTICATION = Authentication.LOW
PASSWORD = "lnt1"
WAIT_TIME = 5                 # seconds to wait for each reply
RESEND_COUNT = 3              # resends of a frame before giving up
TRACE = True                  # log every TX/RX frame in hex

ASSOCIATION_CACHE_SECONDS = 24 * 60 * 60
PROFILE_BATCH_ROWS = 50
PROFILE_MAX_ROWS_PER_CYCLE = 200
PROFILE_MAX_BYTES_PER_CYCLE = 1024 * 1024
PROFILE_READ_BUDGET_SECONDS = 120
PROFILE_OVERLAP_ROWS = 2

PROFILE_INITIAL_ROWS = {
    "load": 96,
    "daily": 7,
    "billing": 3,
    "event": 100,
}

PROFILE_CHECK_SECONDS = {
    "load": 0,
    "event": 0,
    "daily": 24 * 60 * 60,
    "billing": 24 * 60 * 60,
}

# (group, label, COSEM class, OBIS). Registers get scaler/unit applied.
READ_LIST = [
    ("Identity", "Serial number", GXDLMSData, "0.0.96.1.0.255"),
    ("Identity", "Manufacturer", GXDLMSData, "0.0.96.1.1.255"),
    ("Identity", "Meter type", GXDLMSData, "0.0.94.91.9.255"),
    ("Identity", "Firmware version", GXDLMSData, "1.0.0.2.0.255"),
    ("Clock", "Date/time", GXDLMSClock, "0.0.1.0.0.255"),
    ("Instantaneous", "Voltage (1-ph)", GXDLMSRegister, "1.0.12.7.0.255"),
    ("Instantaneous", "Voltage L1", GXDLMSRegister, "1.0.32.7.0.255"),
    ("Instantaneous", "Voltage L2", GXDLMSRegister, "1.0.52.7.0.255"),
    ("Instantaneous", "Voltage L3", GXDLMSRegister, "1.0.72.7.0.255"),
    ("Instantaneous", "Current (1-ph)", GXDLMSRegister, "1.0.11.7.0.255"),
    ("Instantaneous", "Current L1", GXDLMSRegister, "1.0.31.7.0.255"),
    ("Instantaneous", "Current L2", GXDLMSRegister, "1.0.51.7.0.255"),
    ("Instantaneous", "Current L3", GXDLMSRegister, "1.0.71.7.0.255"),
    ("Instantaneous", "Power factor", GXDLMSRegister, "1.0.13.7.0.255"),
    ("Instantaneous", "Frequency", GXDLMSRegister, "1.0.14.7.0.255"),
    ("Instantaneous", "Active power", GXDLMSRegister, "1.0.1.7.0.255"),
    ("Energy", "Import active energy", GXDLMSRegister, "1.0.1.8.0.255"),
    ("Energy", "Import apparent energy", GXDLMSRegister, "1.0.9.8.0.255"),
]

UNIT_TEXT = {
    Unit.VOLTAGE: "V", Unit.CURRENT: "A", Unit.FREQUENCY: "Hz",
    Unit.ACTIVE_POWER: "W", Unit.APPARENT_POWER: "VA", Unit.REACTIVE_POWER: "var",
    Unit.ACTIVE_ENERGY: "Wh", Unit.APPARENT_ENERGY: "VAh", Unit.REACTIVE_ENERGY: "varh",
}


class LinkClosed(Exception):
    """The module disconnected while a session was running."""


def _hex(data):
    return binascii.hexlify(bytes(data), " ").decode().upper()


def _format(value):
    if isinstance(value, (bytes, bytearray)):
        if value and all(32 <= b < 127 for b in value):
            return value.decode("ascii")
        return _hex(value)
    if isinstance(value, float):
        return "{:g}".format(round(value, 3))
    return str(value)


def _enum_text(value):
    name = getattr(value, "name", None)
    return name if name is not None else str(value)


def json_value(value):
    """Convert Gurux values into deterministic JSON-compatible values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        if raw and all(32 <= b < 127 for b in raw):
            return raw.decode("ascii")
        return {"hex": binascii.hexlify(raw).decode().upper()}
    if isinstance(value, datetime):
        return value.isoformat()
    inner = getattr(value, "value", None)
    if isinstance(inner, datetime):
        return inner.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if hasattr(value, "name"):
        return _enum_text(value)
    logical_name = getattr(value, "logicalName", None)
    if logical_name:
        return {"logical_name": logical_name, "class": type(value).__name__}
    return str(value)


def classify_profile(obis):
    """Classify common DLMS profile families without rejecting vendor OBISes."""
    try:
        a, _b, c, d, _e, _f = [int(part) for part in obis.split(".")]
    except (TypeError, ValueError):
        return "unknown"
    if a == 0 and c == 99 and d == 98:
        return "event"
    if c == 99 and d == 1:
        return "load"
    if c == 99 and d == 2:
        return "daily"
    if c == 98:
        return "billing"
    return "unknown"


def profile_is_due(profile_type, state, now=None):
    interval = PROFILE_CHECK_SECONDS.get(profile_type)
    if interval is None:
        return False
    if not state or not state.get("last_success_at") or interval == 0:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(state["last_success_at"])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= interval


def choose_entry_window(profile_type, entries, capacity, capture_period, state):
    """Return a bounded 1-based (start, count) incremental read window."""
    entries = max(0, int(entries or 0))
    capacity = max(0, int(capacity or 0))
    if entries == 0:
        return 0, 0

    initial = PROFILE_INITIAL_ROWS.get(profile_type, 0)
    previous = state.get("entries_in_use") if state else None
    if previous is None:
        count = min(entries, initial)
    elif entries < int(previous):
        # The profile was reset or wrapped to a smaller entry count.
        count = min(entries, initial)
    elif entries > int(previous):
        start = max(1, int(previous) + 1 - PROFILE_OVERLAP_ROWS)
        count = min(
            entries - start + 1,
            PROFILE_MAX_ROWS_PER_CYCLE,
        )
        return start, count
    elif capacity and entries >= capacity:
        # A full circular profile keeps the same entry count. Estimate how many
        # rows can have arrived since the previous successful synchronization.
        if profile_type == "event":
            count = min(entries, PROFILE_BATCH_ROWS)
        elif capture_period:
            elapsed = 0
            if state and state.get("last_success_at"):
                try:
                    last = datetime.fromisoformat(state["last_success_at"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    elapsed = max(0, (datetime.now(timezone.utc) - last).total_seconds())
                except (TypeError, ValueError):
                    elapsed = 0
            count = min(entries, int(elapsed / capture_period) + PROFILE_OVERLAP_ROWS)
        else:
            count = min(entries, PROFILE_OVERLAP_ROWS)
    else:
        # Re-read a tiny overlap so acknowledgements lost after a database
        # commit cannot create gaps. The row hash removes duplicates.
        count = min(entries, PROFILE_OVERLAP_ROWS)

    count = min(count, PROFILE_MAX_ROWS_PER_CYCLE)
    return max(1, entries - count + 1), count


def profile_supports_time_range(profile):
    sort_object = getattr(profile, "sortObject", None)
    if isinstance(sort_object, GXDLMSClock):
        return True
    logical_name = getattr(sort_object, "logicalName", None)
    if logical_name in ("0.0.1.0.0.255", "0.0.1.1.0.255"):
        return True
    if profile.captureObjects:
        first = profile.captureObjects[0][0]
        return isinstance(first, GXDLMSClock) or first.logicalName in (
            "0.0.1.0.0.255",
            "0.0.1.1.0.255",
        )
    return False


def choose_time_window(profile, state, max_rows):
    """Return a bounded incremental datetime range, or None."""
    if (
        not state
        or not state.get("last_capture_at")
        or not profile.capturePeriod
        or not profile_supports_time_range(profile)
        or max_rows <= PROFILE_OVERLAP_ROWS
    ):
        return None
    try:
        last = datetime.fromisoformat(state["last_capture_at"])
    except (TypeError, ValueError):
        return None
    step = timedelta(seconds=int(profile.capturePeriod))
    start = last - step * PROFILE_OVERLAP_ROWS
    end = start + step * (max_rows - 1)
    return start, end


class MeterSession:
    def __init__(self, cid, writer, log):
        self.cid = cid
        self.writer = writer
        self.log = log
        self.rx = asyncio.Queue()
        self.resends = RESEND_COUNT
        server_address = GXDLMSClient.getServerAddress(LOGICAL_SERVER, PHYSICAL_SERVER)
        self.client = GXDLMSClient(True, CLIENT_ADDRESS, server_address,
                                   AUTHENTICATION, PASSWORD, InterfaceType.HDLC)
        self.client.standard = Standard.INDIA

    # ---- byte transport ----
    def feed(self, data):
        """Bytes received from the module (None = connection closed)."""
        self.rx.put_nowait(data)

    async def _exchange(self, frame, reply):
        """Send one frame and wait until Gurux has a complete reply."""
        for attempt in range(1 + self.resends):
            while not self.rx.empty():          # drop stale bytes
                if self.rx.get_nowait() is None:
                    raise LinkClosed()
            if TRACE:
                self.log("DLMS TX #{}: {}".format(self.cid, _hex(frame)))
            self.writer.write(bytes(frame))
            await self.writer.drain()
            buf = GXByteBuffer()
            notify = GXReplyData()
            try:
                while True:
                    chunk = await asyncio.wait_for(self.rx.get(), WAIT_TIME)
                    if chunk is None:
                        raise LinkClosed()
                    if TRACE:
                        self.log("DLMS RX #{}: {}".format(self.cid, _hex(chunk)))
                    buf.set(chunk)
                    if self.client.getData(buf, reply, notify):
                        break
            except asyncio.TimeoutError:
                self.log("DLMS #{}: no reply in {}s (attempt {}/{})".format(
                    self.cid, WAIT_TIME, attempt + 1, 1 + self.resends))
                continue
            if reply.error:
                raise GXDLMSException(reply.error)
            return
        raise TimeoutError("meter did not answer")

    async def _request(self, frames, reply):
        """Send a request (one frame or a list) and collect multi-block replies."""
        if not frames:
            return
        if not isinstance(frames, list):
            frames = [frames]
        for frame in frames:
            reply.clear()
            await self._exchange(frame, reply)
            while reply.isMoreData():
                await self._exchange(self.client.receiverReady(reply), reply)

    # ---- DLMS steps ----
    async def connect(self):
        reply = GXReplyData()
        await self._request(self.client.snrmRequest(), reply)
        self.client.parseUAResponse(reply.data)
        reply = GXReplyData()
        await self._request(self.client.aarqRequest(), reply)
        self.client.parseAareResponse(reply.data)
        if self.client.getIsAuthenticationRequired():      # HLS only
            reply = GXReplyData()
            await self._request(self.client.getApplicationAssociationRequest(), reply)
            self.client.parseApplicationAssociationResponse(reply.data)

    async def read_attribute(self, obj, index):
        reply = GXReplyData()
        await self._request(self.client.read(obj, index), reply)
        return self.client.updateValue(obj, index, reply.value)

    async def read_item(self, cls, obis):
        obj = cls(obis)
        if isinstance(obj, GXDLMSRegister):
            await self.read_attribute(obj, 3)           # scaler + unit first
            value = await self.read_attribute(obj, 2)   # then scaled value
            if obj.unit in (None, Unit.NONE, Unit.NO_UNIT):
                unit = ""
            else:
                unit = UNIT_TEXT.get(obj.unit, getattr(obj.unit, "name", str(obj.unit)))
            return "{} {}".format(_format(value), unit).strip()
        return _format(await self.read_attribute(obj, 2))

    async def discover_objects(self):
        """Read and parse the Association View, including access rights."""
        reply = GXReplyData()
        await self._request(self.client.getObjectsRequest(), reply)
        return self.client.parseObjects(
            reply.data,
            onlyKnownObjects=False,
            ignoreInactiveObjects=False,
        )

    async def read_profile_metadata(self, profile):
        """Read Profile Generic metadata but never its buffer attribute 2."""
        errors = {}
        # Capture objects must be parsed first so profile rows can be decoded.
        for index in (3, 4, 5, 6, 7, 8):
            try:
                await self.read_attribute(profile, index)
            except GXDLMSException as error:
                errors[str(index)] = str(error)
            except TimeoutError as error:
                errors[str(index)] = str(error)
        return errors

    async def prepare_profile_scalers(self, profile):
        """Load scaler/unit metadata used by Gurux while decoding profile rows."""
        for obj, capture in profile.captureObjects:
            try:
                if isinstance(obj, GXDLMSDemandRegister):
                    await self.read_attribute(obj, 4)
                elif isinstance(obj, GXDLMSRegister):
                    await self.read_attribute(obj, 3)
            except (GXDLMSException, TimeoutError):
                # A missing scaler must not prevent the rest of the profile
                # from being collected. Gurux will retain its neutral default.
                continue

    async def read_profile_by_entry(self, profile, start, count):
        if count <= 0:
            return []
        reply = GXReplyData()
        await self._request(
            self.client.readRowsByEntry(profile, start, count),
            reply,
        )
        self.client.updateValue(profile, 2, reply.value)
        return list(profile.buffer)

    async def read_profile_entry_batches(self, profile, start, count):
        rows = []
        next_start = start
        remaining = count
        while remaining > 0:
            batch_count = min(PROFILE_BATCH_ROWS, remaining)
            rows.extend(await self.read_profile_by_entry(
                profile, next_start, batch_count
            ))
            next_start += batch_count
            remaining -= batch_count
        return rows

    async def read_profile_by_range(self, profile, start, end):
        reply = GXReplyData()
        await self._request(
            self.client.readRowsByRange(profile, start, end),
            reply,
        )
        self.client.updateValue(profile, 2, reply.value)
        return list(profile.buffer)

    async def close(self):
        """Release the association and drop the HDLC link; errors are ignored."""
        self.resends = 0
        # Build each request only when sending it: disconnectRequest() resets
        # the HDLC frame counters, which would break the RLRE check.
        for make in (self.client.releaseRequest, self.client.disconnectRequest):
            try:
                await self._request(make(), GXReplyData())
            except Exception:
                pass


async def read_basic(session):
    """Connect, read READ_LIST, disconnect. Returns [(group, label, obis, text)]."""
    try:
        await session.connect()
        session.log("DLMS #{}: associated (client {}, server {}/{})".format(
            session.cid, CLIENT_ADDRESS, LOGICAL_SERVER, PHYSICAL_SERVER))
        return await read_basic_connected(session)
    finally:
        await session.close()


async def read_basic_connected(session):
    """Read the configured scalar values on an already associated session."""
    results = []
    for group, label, cls, obis in READ_LIST:
        try:
            text = await session.read_item(cls, obis)
        except GXDLMSException as error:
            text = "n/a ({})".format(error)
        results.append((group, label, obis, text))
    return results


def association_profiles(objects):
    profiles = []
    for obj in objects:
        if not isinstance(obj, GXDLMSProfileGeneric):
            continue
        profiles.append({
            "obis": obj.logicalName,
            "class_id": int(obj.objectType),
            "class_name": type(obj).__name__,
            "profile_type": classify_profile(obj.logicalName),
            "description": getattr(obj, "description", None),
            "metadata": {
                "version": int(getattr(obj, "version", 0)),
                "attribute_access": {
                    str(index): int(obj.getAccess(index))
                    for index in range(1, obj.getAttributeCount() + 1)
                },
            },
            "object": obj,
        })
    return profiles


def profile_columns(profile):
    columns = []
    for position, (obj, capture) in enumerate(profile.captureObjects, 1):
        unit = getattr(obj, "unit", None)
        columns.append({
            "position": position,
            "class_id": int(obj.objectType),
            "class_name": type(obj).__name__,
            "obis": obj.logicalName,
            "attribute_index": int(capture.attributeIndex),
            "data_index": int(capture.dataIndex),
            "description": getattr(obj, "description", None),
            "unit": UNIT_TEXT.get(unit, _enum_text(unit)) if unit is not None else None,
            "scaler": getattr(obj, "scaler", None),
        })
    return columns


def profile_metadata(profile, errors=None):
    sort_object = getattr(profile, "sortObject", None)
    return {
        "capture_period": int(getattr(profile, "capturePeriod", 0) or 0),
        "sort_method": _enum_text(getattr(profile, "sortMethod", "")),
        "sort_object": getattr(sort_object, "logicalName", None),
        "entries_in_use": int(getattr(profile, "entriesInUse", 0) or 0),
        "profile_entries": int(getattr(profile, "profileEntries", 0) or 0),
        "errors": errors or {},
    }


def _capture_time(value):
    if isinstance(value, datetime):
        return value.isoformat()
    inner = getattr(value, "value", None)
    if isinstance(inner, datetime):
        return inner.isoformat()
    return None


def profile_row_records(profile, rows, start_entry=None):
    columns = profile_columns(profile)
    records = []
    for row_offset, row in enumerate(rows):
        capture_time = None
        values = []
        for position, value in enumerate(row):
            column = columns[position] if position < len(columns) else {
                "position": position + 1,
                "obis": None,
                "attribute_index": None,
                "data_index": None,
            }
            if capture_time is None:
                capture_time = _capture_time(value)
            values.append({
                "position": column["position"],
                "obis": column.get("obis"),
                "attribute_index": column.get("attribute_index"),
                "data_index": column.get("data_index"),
                "unit": column.get("unit"),
                "value": json_value(value),
            })
        records.append({
            "capture_time": capture_time,
            "source_entry": (
                start_entry + row_offset if start_entry is not None else None
            ),
            "values": values,
        })
    return records


def _failure_status(error):
    text = str(error).lower()
    if "denied" in text or "access" in text:
        return "access_denied"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    if "select" in text or "scope" in text:
        return "unsupported"
    return "decode_error"


async def discover_profiles(session, store, device_id):
    objects = await session.discover_objects()
    found = association_profiles(objects)
    store.upsert_profiles(
        device_id,
        [{key: value for key, value in item.items() if key != "object"} for item in found],
    )
    return {item["obis"]: item["object"] for item in found}


async def collect_profiles(session, store, device_id, force_discovery=False):
    """Discover and incrementally collect enabled profiles within hard limits."""
    discovered = {}
    if force_discovery or store.association_is_stale(
        device_id, ASSOCIATION_CACHE_SECONDS
    ):
        try:
            discovered = await discover_profiles(session, store, device_id)
        except Exception as error:
            # Profile discovery is supplementary to the basic read. A meter
            # that denies its Association View must not make the whole cycle
            # retry forever.
            return [{
                "obis": "0.0.40.0.0.255",
                "profile_type": "association",
                "status": _failure_status(error),
                "error": str(error),
            }]

    summaries = []
    total_rows = 0
    total_bytes = 0
    started = asyncio.get_running_loop().time()
    for record in store.list_profiles(device_id, enabled_only=True):
        if asyncio.get_running_loop().time() - started >= PROFILE_READ_BUDGET_SECONDS:
            break
        if total_rows >= PROFILE_MAX_ROWS_PER_CYCLE or total_bytes >= PROFILE_MAX_BYTES_PER_CYCLE:
            break

        state = store.get_sync_state(device_id, record["obis"])
        if not profile_is_due(record["profile_type"], state):
            continue

        profile = discovered.get(record["obis"])
        if profile is None:
            profile = GXDLMSProfileGeneric(record["obis"])
        store.mark_attempt(device_id, record["obis"])
        try:
            errors = await session.read_profile_metadata(profile)
            if not profile.captureObjects:
                raise ValueError("profile has no readable capture objects")
            await session.prepare_profile_scalers(profile)
            columns = profile_columns(profile)
            metadata = profile_metadata(profile, errors)
            store.update_profile_metadata(device_id, record["obis"], metadata, columns)

            start, count = choose_entry_window(
                record["profile_type"],
                profile.entriesInUse,
                profile.profileEntries,
                profile.capturePeriod,
                state,
            )
            remaining_rows = PROFILE_MAX_ROWS_PER_CYCLE - total_rows
            count = min(count, remaining_rows)
            range_window = choose_time_window(profile, state, count)
            if range_window is not None:
                rows = await session.read_profile_by_range(
                    profile, range_window[0], range_window[1]
                )
                source_entry = None
                if len(rows) > remaining_rows:
                    raise ValueError("profile range exceeded the row budget")
            else:
                rows = await session.read_profile_entry_batches(
                    profile, start, count
                )
                source_entry = start

            output = profile_row_records(profile, rows, source_entry)
            available_bytes = PROFILE_MAX_BYTES_PER_CYCLE - total_bytes
            stored_output = []
            encoded_size = 0
            for row in output:
                row_size = len(json.dumps(row, default=str).encode("utf-8"))
                if encoded_size + row_size > available_bytes:
                    break
                stored_output.append(row)
                encoded_size += row_size
            partial = len(stored_output) != len(output)

            if source_entry is not None and stored_output:
                synced_entry = source_entry + len(stored_output) - 1
            elif state and state.get("entries_in_use") is not None:
                synced_entry = min(
                    int(profile.entriesInUse),
                    int(state["entries_in_use"])
                    + max(0, len(stored_output) - PROFILE_OVERLAP_ROWS),
                )
            else:
                synced_entry = (
                    source_entry - 1 if source_entry is not None
                    else int(profile.entriesInUse)
                )
            inserted = store.save_rows(
                device_id, record["obis"], stored_output, synced_entry
            )
            if partial:
                store.mark_failure(
                    device_id,
                    record["obis"],
                    "partial",
                    "profile byte budget reached; remaining rows deferred",
                )
            total_rows += len(stored_output)
            total_bytes += encoded_size
            summaries.append({
                "obis": record["obis"],
                "profile_type": record["profile_type"],
                "status": "partial" if partial else "complete",
                "rows_read": len(output),
                "rows_inserted": inserted,
                "error": (
                    "profile byte budget reached; remaining rows deferred"
                    if partial else None
                ),
            })
        except Exception as error:
            status = _failure_status(error)
            store.mark_failure(device_id, record["obis"], status, error)
            summaries.append({
                "obis": record["obis"],
                "profile_type": record["profile_type"],
                "status": status,
                "error": str(error),
            })
    return summaries


async def read_cycle(session, store, device_id, include_profiles=True):
    """Run one associated scalar and bounded profile collection cycle."""
    try:
        await session.connect()
        session.log("DLMS #{}: associated (client {}, server {}/{})".format(
            session.cid, CLIENT_ADDRESS, LOGICAL_SERVER, PHYSICAL_SERVER))
        basic = await read_basic_connected(session)
        profiles = []
        if include_profiles:
            try:
                profiles = await asyncio.wait_for(
                    collect_profiles(session, store, device_id),
                    PROFILE_READ_BUDGET_SECONDS,
                )
            except asyncio.TimeoutError:
                store.mark_running_partial(
                    device_id, "profile collection exceeded the time budget"
                )
                profiles = [{
                    "obis": "*",
                    "profile_type": "collection",
                    "status": "partial",
                    "error": "profile collection exceeded the time budget",
                }]
        return basic, profiles
    finally:
        await session.close()


async def scan_profiles(session, store, device_id):
    """Refresh the Association View without reading any profile buffers."""
    try:
        await session.connect()
        discovered = await discover_profiles(session, store, device_id)
        return store.list_profiles(device_id), discovered
    finally:
        await session.close()


async def read_profile_manual(session, store, device_id, obis, mode, values):
    """Run one explicitly bounded profile read and persist its rows."""
    try:
        await session.connect()
        objects = await session.discover_objects()
        profiles = {obj.logicalName: obj for obj in objects
                    if isinstance(obj, GXDLMSProfileGeneric)}
        profile = profiles.get(obis)
        if profile is None:
            raise ValueError("Profile Generic {} was not found".format(obis))
        descriptor = association_profiles([profile])[0]
        store.upsert_profiles(
            device_id,
            [{key: value for key, value in descriptor.items() if key != "object"}],
        )
        errors = await session.read_profile_metadata(profile)
        if not profile.captureObjects:
            raise ValueError("profile has no readable capture objects")
        await session.prepare_profile_scalers(profile)
        store.update_profile_metadata(
            device_id, obis, profile_metadata(profile, errors), profile_columns(profile)
        )

        if mode == "last":
            requested = int(values[0])
            if requested < 1:
                raise ValueError("last count must be at least 1")
            count = min(requested, PROFILE_MAX_ROWS_PER_CYCLE,
                        int(profile.entriesInUse))
            start = max(1, int(profile.entriesInUse) - count + 1)
            rows = await session.read_profile_entry_batches(profile, start, count)
            source_entry = start
        elif mode == "entry":
            start, count = int(values[0]), int(values[1])
            if start < 1 or count < 1 or count > PROFILE_MAX_ROWS_PER_CYCLE:
                raise ValueError("entry start/count is outside the allowed range")
            rows = await session.read_profile_entry_batches(profile, start, count)
            source_entry = start
        elif mode == "range":
            start = datetime.fromisoformat(values[0])
            end = datetime.fromisoformat(values[1])
            if end <= start:
                raise ValueError("profile range end must be after start")
            if not profile.capturePeriod or not profile_supports_time_range(profile):
                raise ValueError(
                    "a bounded time range requires a clock-sorted profile with "
                    "a non-zero capture period; use entry mode"
                )
            maximum_span = timedelta(
                seconds=int(profile.capturePeriod) * (PROFILE_MAX_ROWS_PER_CYCLE - 1)
            )
            if end - start > maximum_span:
                raise ValueError(
                    "profile range can contain more than {} rows".format(
                        PROFILE_MAX_ROWS_PER_CYCLE)
                )
            rows = await session.read_profile_by_range(profile, start, end)
            source_entry = None
            if len(rows) > PROFILE_MAX_ROWS_PER_CYCLE:
                raise ValueError("profile range returned more than {} rows".format(
                    PROFILE_MAX_ROWS_PER_CYCLE))
        else:
            raise ValueError("unknown profile read mode")

        output = profile_row_records(profile, rows, source_entry)
        size = len(json.dumps(output, default=str).encode("utf-8"))
        if size > PROFILE_MAX_BYTES_PER_CYCLE:
            raise ValueError("profile response exceeds the byte limit")
        inserted = store.save_rows(device_id, obis, output, profile.entriesInUse)
        return {
            "obis": obis,
            "mode": mode,
            "rows_read": len(output),
            "rows_inserted": inserted,
            "rows": output,
        }
    finally:
        await session.close()


def format_results(cid, results):
    lines = ["Meter details via module #{}".format(cid)]
    group = None
    for g, label, obis, text in results:
        if g != group:
            lines.append("  [{}]".format(g))
            group = g
        lines.append("    {:<24} {:<16} {}".format(label, obis, text))
    return "\n".join(lines)
