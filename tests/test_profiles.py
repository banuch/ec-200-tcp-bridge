import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import dlms_reader
from profile_store import ProfileStore


class ProfilePolicyTests(unittest.TestCase):
    def test_classifies_common_profile_families(self):
        cases = {
            "1.0.99.1.0.255": "load",
            "1.0.99.2.0.255": "daily",
            "1.0.98.1.0.255": "billing",
            "0.0.99.98.0.255": "event",
            "1.0.99.9.0.255": "unknown",
        }
        for obis, expected in cases.items():
            with self.subTest(obis=obis):
                self.assertEqual(dlms_reader.classify_profile(obis), expected)

    def test_initial_load_sync_is_limited_to_latest_day(self):
        self.assertEqual(
            dlms_reader.choose_entry_window("load", 120, 1000, 900, None),
            (25, 96),
        )

    def test_incremental_sync_includes_overlap(self):
        state = {"entries_in_use": 100, "last_success_at": None}
        self.assertEqual(
            dlms_reader.choose_entry_window("load", 105, 1000, 900, state),
            (99, 7),
        )

    def test_large_backlog_continues_from_cursor_without_skipping(self):
        state = {"entries_in_use": 100, "last_success_at": None}
        self.assertEqual(
            dlms_reader.choose_entry_window("load", 600, 1000, 900, state),
            (99, 200),
        )

    def test_reset_uses_bounded_initial_window(self):
        state = {"entries_in_use": 800, "last_success_at": None}
        self.assertEqual(
            dlms_reader.choose_entry_window("event", 60, 1000, 0, state),
            (1, 60),
        )

    def test_full_circular_profile_estimates_recent_rows(self):
        state = {
            "entries_in_use": 1000,
            "last_success_at": (
                datetime.now(timezone.utc) - timedelta(minutes=31)
            ).isoformat(),
        }
        start, count = dlms_reader.choose_entry_window(
            "load", 1000, 1000, 900, state
        )
        self.assertEqual(count, 4)
        self.assertEqual(start, 997)

    def test_unknown_profiles_are_not_scheduled(self):
        self.assertFalse(dlms_reader.profile_is_due("unknown", None))

    def test_time_window_is_bounded_by_requested_rows(self):
        from gurux_dlms.objects import GXDLMSClock, GXDLMSProfileGeneric

        profile = GXDLMSProfileGeneric("1.0.99.1.0.255")
        profile.capturePeriod = 900
        profile.sortObject = GXDLMSClock("0.0.1.0.0.255")
        last = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        start, end = dlms_reader.choose_time_window(
            profile, {"last_capture_at": last.isoformat()}, 10
        )
        self.assertEqual(start, last - timedelta(minutes=30))
        self.assertEqual(end - start, timedelta(minutes=135))


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = ProfileStore(self.path)
        self.profile = {
            "obis": "1.0.99.1.0.255",
            "class_id": 7,
            "class_name": "GXDLMSProfileGeneric",
            "profile_type": "load",
            "description": "Load survey",
            "metadata": {"version": 1},
        }
        self.store.upsert_profiles("device-1", [self.profile])

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_known_profile_is_enabled_by_default(self):
        stored = self.store.get_profile("device-1", self.profile["obis"])
        self.assertEqual(stored["enabled"], 1)

    def test_unknown_profile_is_disabled_by_default(self):
        profile = dict(self.profile)
        profile.update(obis="1.0.99.9.0.255", profile_type="unknown")
        self.store.upsert_profiles("device-1", [profile])
        stored = self.store.get_profile("device-1", profile["obis"])
        self.assertEqual(stored["enabled"], 0)

    def test_rows_are_deduplicated(self):
        rows = [{"capture_time": "2026-10-25T12:00:00", "values": [1, 2]}]
        self.assertEqual(
            self.store.save_rows("device-1", self.profile["obis"], rows, 10),
            1,
        )
        self.assertEqual(
            self.store.save_rows("device-1", self.profile["obis"], rows, 10),
            0,
        )

    def test_newest_capture_time_wins_for_descending_rows(self):
        rows = [
            {"capture_time": "2026-10-25T12:15:00", "values": [2]},
            {"capture_time": "2026-10-25T12:00:00", "values": [1]},
        ]
        self.store.save_rows("device-1", self.profile["obis"], rows, 10)
        state = self.store.get_sync_state("device-1", self.profile["obis"])
        self.assertEqual(state["last_capture_at"], "2026-10-25T12:15:00")

    def test_enable_state_survives_rediscovery(self):
        self.store.set_profile_enabled("device-1", self.profile["obis"], False)
        changed = dict(self.profile)
        changed["description"] = "Updated"
        self.store.upsert_profiles("device-1", [changed])
        stored = self.store.get_profile("device-1", self.profile["obis"])
        self.assertEqual(stored["enabled"], 0)
        self.assertEqual(stored["description"], "Updated")


class ProfileCollectionTests(unittest.TestCase):
    def setUp(self):
        from gurux_dlms.objects import GXDLMSClock, GXDLMSProfileGeneric, GXDLMSRegister

        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.store = ProfileStore(self.path)
        self.profile = GXDLMSProfileGeneric("1.0.99.1.0.255")
        self.profile.addCaptureObject(GXDLMSClock("0.0.1.0.0.255"), 2, 0)
        self.profile.addCaptureObject(GXDLMSRegister("1.0.1.29.0.255"), 2, 0)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_initial_collection_reads_only_configured_tail(self):
        profile = self.profile

        class FakeSession:
            def __init__(self):
                self.entry_requests = []

            async def discover_objects(self):
                return [profile]

            async def read_profile_metadata(self, target):
                target.capturePeriod = 900
                target.entriesInUse = 120
                target.profileEntries = 1000
                return {}

            async def prepare_profile_scalers(self, target):
                return None

            async def read_profile_by_entry(self, target, start, count):
                self.entry_requests.append((start, count))
                base = datetime(2026, 10, 24, tzinfo=timezone.utc)
                return [
                    [base + timedelta(minutes=15 * (start + offset)), start + offset]
                    for offset in range(count)
                ]

            async def read_profile_entry_batches(self, target, start, count):
                rows = []
                while count:
                    batch = min(dlms_reader.PROFILE_BATCH_ROWS, count)
                    rows.extend(await self.read_profile_by_entry(
                        target, start, batch
                    ))
                    start += batch
                    count -= batch
                return rows

            async def read_profile_by_range(self, target, start, end):
                raise AssertionError(
                    "range is not used before a capture-time cursor exists"
                )

        session = FakeSession()
        operation = dlms_reader.collect_profiles(
            session, self.store, "device-1", force_discovery=True
        )
        fake_loop = mock.Mock()
        fake_loop.time.return_value = 0
        with mock.patch.object(
            dlms_reader.asyncio, "get_running_loop", return_value=fake_loop
        ):
            with self.assertRaises(StopIteration) as completed:
                operation.send(None)
        results = completed.exception.value
        self.assertEqual(session.entry_requests, [(25, 50), (75, 46)])
        self.assertEqual(results[0]["rows_read"], 96)
        self.assertEqual(results[0]["rows_inserted"], 96)


if __name__ == "__main__":
    unittest.main()
