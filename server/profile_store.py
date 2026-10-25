"""SQLite persistence for discovered DLMS Profile Generic data."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class ProfileStore:
    """Small synchronous store; operations are short and run in one event loop."""

    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def _create_schema(self):
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                device_id TEXT NOT NULL,
                obis TEXT NOT NULL,
                class_id INTEGER NOT NULL,
                class_name TEXT NOT NULL,
                profile_type TEXT NOT NULL,
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                discovered_at TEXT NOT NULL,
                PRIMARY KEY (device_id, obis)
            );

            CREATE TABLE IF NOT EXISTS profile_columns (
                device_id TEXT NOT NULL,
                profile_obis TEXT NOT NULL,
                position INTEGER NOT NULL,
                class_id INTEGER NOT NULL,
                obis TEXT NOT NULL,
                attribute_index INTEGER NOT NULL,
                data_index INTEGER NOT NULL,
                description TEXT,
                PRIMARY KEY (device_id, profile_obis, position),
                FOREIGN KEY (device_id, profile_obis)
                    REFERENCES profiles(device_id, obis) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS profile_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                profile_obis TEXT NOT NULL,
                capture_time TEXT,
                row_hash TEXT NOT NULL,
                row_json TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE (device_id, profile_obis, row_hash),
                FOREIGN KEY (device_id, profile_obis)
                    REFERENCES profiles(device_id, obis) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS profile_sync_state (
                device_id TEXT NOT NULL,
                profile_obis TEXT NOT NULL,
                entries_in_use INTEGER,
                last_capture_at TEXT,
                last_row_hash TEXT,
                last_success_at TEXT,
                last_attempt_at TEXT,
                status TEXT NOT NULL DEFAULT 'never',
                error TEXT,
                PRIMARY KEY (device_id, profile_obis),
                FOREIGN KEY (device_id, profile_obis)
                    REFERENCES profiles(device_id, obis) ON DELETE CASCADE
            );
            """
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def association_is_stale(self, device_id, max_age_seconds):
        row = self.db.execute(
            "SELECT MAX(discovered_at) AS discovered_at FROM profiles WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        if not row or not row["discovered_at"]:
            return True
        try:
            discovered = datetime.fromisoformat(row["discovered_at"])
            if discovered.tzinfo is None:
                discovered = discovered.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - discovered).total_seconds() >= max_age_seconds
        except (TypeError, ValueError):
            return True

    def upsert_profiles(self, device_id, profiles):
        stamp = utc_now()
        with self.db:
            for profile in profiles:
                existing = self.db.execute(
                    "SELECT enabled FROM profiles WHERE device_id = ? AND obis = ?",
                    (device_id, profile["obis"]),
                ).fetchone()
                enabled = (
                    int(existing["enabled"])
                    if existing is not None
                    else int(profile["profile_type"] != "unknown")
                )
                self.db.execute(
                    """
                    INSERT INTO profiles (
                        device_id, obis, class_id, class_name, profile_type,
                        description, enabled, metadata_json, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id, obis) DO UPDATE SET
                        class_id = excluded.class_id,
                        class_name = excluded.class_name,
                        profile_type = excluded.profile_type,
                        description = excluded.description,
                        metadata_json = excluded.metadata_json,
                        discovered_at = excluded.discovered_at
                    """,
                    (
                        device_id,
                        profile["obis"],
                        profile["class_id"],
                        profile["class_name"],
                        profile["profile_type"],
                        profile.get("description"),
                        enabled,
                        canonical_json(profile.get("metadata", {})),
                        stamp,
                    ),
                )
                self.db.execute(
                    """
                    INSERT OR IGNORE INTO profile_sync_state
                        (device_id, profile_obis, status)
                    VALUES (?, ?, 'never')
                    """,
                    (device_id, profile["obis"]),
                )

    def update_profile_metadata(self, device_id, obis, metadata, columns):
        with self.db:
            self.db.execute(
                "UPDATE profiles SET metadata_json = ? WHERE device_id = ? AND obis = ?",
                (canonical_json(metadata), device_id, obis),
            )
            self.db.execute(
                "DELETE FROM profile_columns WHERE device_id = ? AND profile_obis = ?",
                (device_id, obis),
            )
            for column in columns:
                self.db.execute(
                    """
                    INSERT INTO profile_columns (
                        device_id, profile_obis, position, class_id, obis,
                        attribute_index, data_index, description
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        obis,
                        column["position"],
                        column["class_id"],
                        column["obis"],
                        column["attribute_index"],
                        column["data_index"],
                        column.get("description"),
                    ),
                )

    def list_profiles(self, device_id, enabled_only=False):
        sql = "SELECT * FROM profiles WHERE device_id = ?"
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY profile_type, obis"
        return [dict(row) for row in self.db.execute(sql, (device_id,)).fetchall()]

    def get_profile(self, device_id, obis):
        row = self.db.execute(
            "SELECT * FROM profiles WHERE device_id = ? AND obis = ?",
            (device_id, obis),
        ).fetchone()
        return dict(row) if row else None

    def set_profile_enabled(self, device_id, obis, enabled):
        with self.db:
            cursor = self.db.execute(
                "UPDATE profiles SET enabled = ? WHERE device_id = ? AND obis = ?",
                (int(enabled), device_id, obis),
            )
        return cursor.rowcount != 0

    def get_sync_state(self, device_id, obis):
        row = self.db.execute(
            """
            SELECT * FROM profile_sync_state
            WHERE device_id = ? AND profile_obis = ?
            """,
            (device_id, obis),
        ).fetchone()
        return dict(row) if row else None

    def mark_attempt(self, device_id, obis):
        with self.db:
            self.db.execute(
                """
                INSERT INTO profile_sync_state (
                    device_id, profile_obis, last_attempt_at, status
                ) VALUES (?, ?, ?, 'running')
                ON CONFLICT(device_id, profile_obis) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    status = 'running', error = NULL
                """,
                (device_id, obis, utc_now()),
            )

    def mark_failure(self, device_id, obis, status, error):
        with self.db:
            self.db.execute(
                """
                UPDATE profile_sync_state
                SET status = ?, error = ?
                WHERE device_id = ? AND profile_obis = ?
                """,
                (status, str(error)[:1000], device_id, obis),
            )

    def mark_running_partial(self, device_id, error):
        with self.db:
            self.db.execute(
                """
                UPDATE profile_sync_state
                SET status = 'partial', error = ?
                WHERE device_id = ? AND status = 'running'
                """,
                (str(error)[:1000], device_id),
            )

    def save_rows(self, device_id, obis, rows, entries_in_use):
        inserted = 0
        last_capture = None
        last_hash = None
        stamp = utc_now()
        with self.db:
            for row in rows:
                row_json = canonical_json(row)
                row_hash = hashlib.sha256(row_json.encode("utf-8")).hexdigest()
                capture_time = row.get("capture_time")
                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO profile_rows (
                        device_id, profile_obis, capture_time, row_hash,
                        row_json, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (device_id, obis, capture_time, row_hash, row_json, stamp),
                )
                inserted += cursor.rowcount
                if capture_time:
                    if last_capture is None:
                        last_capture = capture_time
                    else:
                        try:
                            current = datetime.fromisoformat(capture_time)
                            previous = datetime.fromisoformat(last_capture)
                            if current > previous:
                                last_capture = capture_time
                        except (TypeError, ValueError):
                            if capture_time > last_capture:
                                last_capture = capture_time
                last_hash = row_hash
            self.db.execute(
                """
                INSERT INTO profile_sync_state (
                    device_id, profile_obis, entries_in_use, last_capture_at,
                    last_row_hash, last_success_at, last_attempt_at, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'complete', NULL)
                ON CONFLICT(device_id, profile_obis) DO UPDATE SET
                    entries_in_use = excluded.entries_in_use,
                    last_capture_at = COALESCE(excluded.last_capture_at, profile_sync_state.last_capture_at),
                    last_row_hash = COALESCE(excluded.last_row_hash, profile_sync_state.last_row_hash),
                    last_success_at = excluded.last_success_at,
                    last_attempt_at = excluded.last_attempt_at,
                    status = 'complete', error = NULL
                """,
                (
                    device_id,
                    obis,
                    entries_in_use,
                    last_capture,
                    last_hash,
                    stamp,
                    stamp,
                ),
            )
        return inserted

    def list_status(self, device_id):
        rows = self.db.execute(
            """
            SELECT p.obis, p.profile_type, p.enabled, s.entries_in_use,
                   s.last_capture_at, s.last_success_at, s.status, s.error
            FROM profiles p
            LEFT JOIN profile_sync_state s
              ON s.device_id = p.device_id AND s.profile_obis = p.obis
            WHERE p.device_id = ?
            ORDER BY p.profile_type, p.obis
            """,
            (device_id,),
        ).fetchall()
        return [dict(row) for row in rows]
