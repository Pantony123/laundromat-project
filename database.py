#!/usr/bin/env python3
"""
Database layer - supports both SQLite (local) and PostgreSQL (production).
Set DATABASE_URL env var for PostgreSQL, otherwise uses local SQLite.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Try to import psycopg2 for PostgreSQL
try:
    import psycopg2
    from psycopg2.pool import SimpleConnectionPool
    HAS_PG = True
except ImportError:
    HAS_PG = False


class Database:
    """Thread-safe database wrapper for SQLite or PostgreSQL."""

    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or os.getenv("DATABASE_URL")
        self.is_postgres = self.database_url and self.database_url.startswith(("postgres://", "postgresql://"))
        self._local = threading.local()
        self._pg_pool = None

        if self.is_postgres:
            if not HAS_PG:
                raise RuntimeError("PostgreSQL URL set but psycopg2 not installed. Run: pip install psycopg2-binary")
            self._pg_pool = SimpleConnectionPool(1, 5, self.database_url)
        else:
            # Local SQLite
            self.sqlite_path = Path("laundromat.db")

        self.init_db()

    def _get_conn(self):
        """Get a connection for the current thread."""
        if self.is_postgres:
            return self._pg_pool.getconn()
        else:
            if not hasattr(self._local, 'conn'):
                self._local.conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
                self._local.conn.row_factory = sqlite3.Row
            return self._local.conn

    def _put_conn(self, conn):
        """Return connection to pool (PostgreSQL) or keep (SQLite)."""
        if self.is_postgres:
            self._pg_pool.putconn(conn)

    @contextmanager
    def conn(self):
        """Context manager for database connection."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()  # Commit for both PostgreSQL and SQLite
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put_conn(conn)

    def execute(self, query: str, params: tuple = ()):
        """Execute a write query."""
        with self.conn() as conn:
            cur = conn.execute(query, params)
            return cur

    def fetchone(self, query: str, params: tuple = ()):
        """Fetch single row."""
        with self.conn() as conn:
            cur = conn.execute(query, params)
            return cur.fetchone()

    def fetchall(self, query: str, params: tuple = ()):
        """Fetch all rows."""
        with self.conn() as conn:
            cur = conn.execute(query, params)
            return cur.fetchall()

    def init_db(self):
        """Create tables if they don't exist."""
        if self.is_postgres:
            # PostgreSQL schema
            schema = """
            CREATE TABLE IF NOT EXISTS machines (
                id VARCHAR(20) PRIMARY KEY,
                type VARCHAR(10) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'available',
                current_booking VARCHAR(50),
                location_notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id VARCHAR(50) PRIMARY KEY,
                machine_id VARCHAR(20) NOT NULL REFERENCES machines(id),
                customer_name VARCHAR(100) NOT NULL,
                customer_phone VARCHAR(30) NOT NULL,
                start_time TIMESTAMP NOT NULL,
                duration_minutes INT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'confirmed',
                completed_at TIMESTAMP,
                check_in_code VARCHAR(4) NOT NULL
            );
            CREATE TABLE IF NOT EXISTS waitlist (
                id VARCHAR(50) PRIMARY KEY,
                customer_name VARCHAR(100) NOT NULL,
                customer_phone VARCHAR(30) NOT NULL,
                machine_type VARCHAR(10) NOT NULL,
                created_at TIMESTAMP NOT NULL,
                notified BOOL NOT NULL DEFAULT FALSE,
                notified_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_machine ON bookings(machine_id);
            CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
            CREATE INDEX IF NOT EXISTS idx_waitlist_type ON waitlist(machine_type);
            """
        else:
            # SQLite schema
            schema = """
            CREATE TABLE IF NOT EXISTS machines (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                current_booking TEXT,
                location_notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                machine_id TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                start_time TEXT NOT NULL,
                duration_minutes INT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                completed_at TEXT,
                check_in_code TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS waitlist (
                id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                machine_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                notified INT NOT NULL DEFAULT 0,
                notified_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bookings_machine ON bookings(machine_id);
            CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
            CREATE INDEX IF NOT EXISTS idx_waitlist_type ON waitlist(machine_type);
            """

        with self.conn() as conn:
            conn.executescript(schema)
            self._seed_machines(conn)

    def _seed_machines(self, conn):
        """Insert default machines if table is empty."""
        count = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
        if count == 0:
            for i in range(1, 5):
                conn.execute(
                    "INSERT INTO machines (id, type, status, location_notes) VALUES (?, ?, ?, ?)",
                    (f"W{i}", "washer", "available", f"Washer row, position {i}")
                )
                conn.execute(
                    "INSERT INTO machines (id, type, status, location_notes) VALUES (?, ?, ?, ?)",
                    (f"D{i}", "dryer", "available", f"Dryer row, position {i}")
                )
            if self.is_postgres:
                conn.commit()

    def close(self):
        """Close all connections."""
        if self.is_postgres and self._pg_pool:
            self._pg_pool.closeall()


# Global instance (initialized on first import)
_db: Optional[Database] = None

def get_db() -> Database:
    """Get the global database instance."""
    global _db
    if _db is None:
        _db = Database()
    return _db