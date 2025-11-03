import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class PersistenceStore:
    def __init__(
        self,
        *,
        db_path: Path,
        customer_event_retention_days: int,
        incident_retention_days: int,
        audit_retention_days: int,
    ) -> None:
        self._db_path = db_path
        self._customer_event_retention_days = customer_event_retention_days
        self._incident_retention_days = incident_retention_days
        self._audit_retention_days = audit_retention_days
        self._lock = threading.RLock()
        self._ensure_database()

    # -- Public API -----------------------------------------------------

    def load_customer_events(self) -> List[Dict[str, Any]]:
        query = """
            SELECT event_id, timestamp, event_type, zone, city, customer_id,
                   lat, lon, source, metadata, customer_name
            FROM customer_events
            ORDER BY datetime(timestamp) ASC
        """
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def save_customer_event(self, event: Dict[str, Any]) -> None:
        payload = dict(event)
        metadata = payload.get("metadata") or {}
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO customer_events (
                    event_id, timestamp, event_type, zone, city, customer_id,
                    lat, lon, source, metadata, customer_name
                )
                VALUES (:event_id, :timestamp, :event_type, :zone, :city, :customer_id,
                        :lat, :lon, :source, :metadata, :customer_name)
                """,
                {
                    "event_id": payload.get("event_id"),
                    "timestamp": payload.get("timestamp"),
                    "event_type": payload.get("event_type"),
                    "zone": payload.get("zone"),
                    "city": payload.get("city"),
                    "customer_id": payload.get("customer_id"),
                    "lat": payload.get("lat"),
                    "lon": payload.get("lon"),
                    "source": payload.get("source"),
                    "metadata": json.dumps(metadata),
                    "customer_name": payload.get("customer_name"),
                },
            )
            conn.commit()

    def purge_customer_events(self) -> None:
        query = """
            DELETE FROM customer_events
            WHERE timestamp < datetime('now', :retention)
        """
        retention_expr = f"-{max(self._customer_event_retention_days, 1)} days"
        with self._connection() as conn:
            conn.execute(query, {"retention": retention_expr})
            conn.commit()

    def load_open_incidents(self) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM incidents
            WHERE status = 'open'
            ORDER BY datetime(timestamp) ASC
        """
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def load_resolved_incidents(self) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM incidents
            WHERE status = 'resolved'
            ORDER BY datetime(resolved_at) ASC
        """
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def save_incident(self, entry: Dict[str, Any]) -> None:
        payload = dict(entry)
        payload.setdefault("status", "open")
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO incidents (
                    incident_id, timestamp, kind, customer_id, customer_name,
                    customer_label, action, detail, status, resolved_at,
                    resolved_by, resolution_reason
                )
                VALUES (
                    :incident_id, :timestamp, :kind, :customer_id, :customer_name,
                    :customer_label, :action, :detail, :status, :resolved_at,
                    :resolved_by, :resolution_reason
                )
                """,
                {
                    "incident_id": payload.get("incident_id"),
                    "timestamp": payload.get("timestamp"),
                    "kind": payload.get("kind"),
                    "customer_id": payload.get("customer_id"),
                    "customer_name": payload.get("customer_name"),
                    "customer_label": payload.get("customer_label"),
                    "action": payload.get("action"),
                    "detail": json.dumps(payload.get("detail") or {}),
                    "status": payload.get("status"),
                    "resolved_at": payload.get("resolved_at"),
                    "resolved_by": payload.get("resolved_by"),
                    "resolution_reason": payload.get("resolution_reason"),
                },
            )
            conn.commit()

    def mark_incidents_resolved(self, incidents: Iterable[Dict[str, Any]]) -> None:
        update_stmt = """
            UPDATE incidents
               SET status = 'resolved',
                   resolved_at = :resolved_at,
                   resolved_by = :resolved_by,
                   resolution_reason = :resolution_reason,
                   detail = :detail
             WHERE incident_id = :incident_id
        """
        with self._connection() as conn:
            for incident in incidents:
                conn.execute(
                    update_stmt,
                    {
                        "incident_id": incident.get("incident_id"),
                        "resolved_at": incident.get("resolved_at"),
                        "resolved_by": incident.get("resolved_by"),
                        "resolution_reason": incident.get("resolution_reason"),
                        "detail": json.dumps(incident.get("detail") or {}),
                    },
                )
            conn.commit()

    def purge_incidents(self) -> None:
        retention_expr = f"-{max(self._incident_retention_days, 1)} days"
        query = """
            DELETE FROM incidents
            WHERE status = 'resolved' AND resolved_at < datetime('now', :retention)
        """
        with self._connection() as conn:
            conn.execute(query, {"retention": retention_expr})
            conn.commit()

    def load_audits(self) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM audits
            ORDER BY datetime(timestamp) ASC
        """
        with self._connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def save_audit(self, entry: Dict[str, Any]) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO audits (
                    audit_id, action, customer_id, user, dry_run,
                    status, detail, timestamp
                )
                VALUES (
                    :audit_id, :action, :customer_id, :user, :dry_run,
                    :status, :detail, :timestamp
                )
                """,
                {
                    "audit_id": entry.get("audit_id"),
                    "action": entry.get("action"),
                    "customer_id": entry.get("customer_id"),
                    "user": entry.get("user"),
                    "dry_run": entry.get("dry_run"),
                    "status": entry.get("status"),
                    "detail": json.dumps(entry.get("detail") or {}),
                    "timestamp": entry.get("timestamp"),
                },
            )
            conn.commit()

    def purge_audits(self) -> None:
        retention_expr = f"-{max(self._audit_retention_days, 1)} days"
        query = """
            DELETE FROM audits
            WHERE timestamp < datetime('now', :retention)
        """
        with self._connection() as conn:
            conn.execute(query, {"retention": retention_expr})
            conn.commit()

    def reset(self) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM customer_events")
            conn.execute("DELETE FROM incidents")
            conn.execute("DELETE FROM audits")
            conn.commit()

    # -- Internal helpers -----------------------------------------------

    def _ensure_database(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_events (
                    event_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    zone TEXT,
                    city TEXT,
                    customer_id INTEGER,
                    lat REAL,
                    lon REAL,
                    source TEXT,
                    metadata TEXT,
                    customer_name TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    customer_id INTEGER,
                    customer_name TEXT,
                    customer_label TEXT,
                    action TEXT,
                    detail TEXT,
                    status TEXT NOT NULL,
                    resolved_at TEXT,
                    resolved_by TEXT,
                    resolution_reason TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audits (
                    audit_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    customer_id INTEGER,
                    user TEXT,
                    dry_run INTEGER,
                    status TEXT NOT NULL,
                    detail TEXT,
                    timestamp TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        return conn

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        for key in row.keys():
            value = row[key]
            if key in {"metadata", "detail"} and value:
                try:
                    data[key] = json.loads(value)
                except json.JSONDecodeError:
                    data[key] = {}
            else:
                data[key] = value
        return data

