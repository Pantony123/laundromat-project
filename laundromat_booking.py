#!/usr/bin/env python3
"""
Laundromat Booking System - Core Module
Uses database layer (SQLite locally, PostgreSQL in production).
Enhanced with auto-complete, waitlist, QR codes, and SMS notifications.
"""

import os
import threading
import time
import qrcode
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
import base64

from database import get_db


class MachineType(Enum):
    WASHER = "washer"
    DRYER = "dryer"


class MachineStatus(Enum):
    AVAILABLE = "available"
    BOOKED = "booked"
    IN_USE = "in_use"
    MAINTENANCE = "maintenance"


class BookingStatus(Enum):
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


@dataclass
class Machine:
    id: str
    type: MachineType
    status: MachineStatus = MachineStatus.AVAILABLE
    current_booking: Optional[str] = None
    location_notes: str = ""


@dataclass
class Booking:
    id: str
    machine_id: str
    customer_name: str
    customer_phone: str
    start_time: str  # ISO format
    duration_minutes: int
    created_at: str  # ISO format
    status: str = BookingStatus.CONFIRMED.value
    completed_at: Optional[str] = None
    check_in_code: str = ""

    def __post_init__(self):
        if not self.check_in_code:
            self.check_in_code = f"{random.randint(1000, 9999)}"

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start_time)

    @property
    def end_dt(self) -> datetime:
        return self.start_dt + timedelta(minutes=self.duration_minutes)

    @property
    def is_expired(self) -> bool:
        return datetime.now() > self.end_dt + timedelta(minutes=10)  # 10min grace


@dataclass
class WaitlistEntry:
    id: str
    customer_name: str
    customer_phone: str
    machine_type: MachineType
    created_at: str
    notified: bool = False
    notified_at: Optional[str] = None


class LaundromatBookingSystem:
    def __init__(
        self,
        qr_dir: str = "qr_codes",
        sms_sender: Optional[Callable[[str, str], bool]] = None,
        auto_complete: bool = True,
    ):
        self.db = get_db()
        self.qr_dir = Path(qr_dir)
        self.qr_dir.mkdir(exist_ok=True)
        self.sms_sender = sms_sender
        self._lock = threading.RLock()
        self._auto_complete_running = False
        self._auto_complete_thread: Optional[threading.Thread] = None

        self._generate_all_qr_codes()

        if auto_complete:
            self.start_auto_complete()

    # ===== HELPERS: Convert DB rows to dataclasses =====
    def _row_to_machine(self, row) -> Machine:
        return Machine(
            id=row["id"],
            type=MachineType(row["type"]),
            status=MachineStatus(row["status"]),
            current_booking=row["current_booking"],
            location_notes=row["location_notes"] or "",
        )

    def _row_to_booking(self, row) -> Booking:
        return Booking(
            id=row["id"],
            machine_id=row["machine_id"],
            customer_name=row["customer_name"],
            customer_phone=row["customer_phone"],
            start_time=row["start_time"],
            duration_minutes=row["duration_minutes"],
            created_at=row["created_at"],
            status=row["status"],
            completed_at=row["completed_at"],
            check_in_code=row["check_in_code"],
        )

    def _row_to_waitlist(self, row) -> WaitlistEntry:
        return WaitlistEntry(
            id=row["id"],
            customer_name=row["customer_name"],
            customer_phone=row["customer_phone"],
            machine_type=MachineType(row["machine_type"]),
            created_at=row["created_at"],
            notified=bool(row["notified"]),
            notified_at=row["notified_at"],
        )

    # ===== QR CODES =====
    def _generate_all_qr_codes(self):
        for machine in self.list_machines():
            self._generate_machine_qr(machine.id)

    def _generate_machine_qr(self, machine_id: str):
        machine = self.get_machine(machine_id)
        if not machine:
            return
        qr_data = f"LAUNDRY:{machine_id}:{machine.type.value}"
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(self.qr_dir / f"{machine_id}.png")

    def get_qr_base64(self, machine_id: str) -> str:
        path = self.qr_dir / f"{machine_id}.png"
        if path.exists():
            with open(path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:image/png;base64,{b64}"
        return ""

    def get_booking_qr_base64(self, booking_id: str) -> str:
        booking = self.get_booking(booking_id)
        if not booking:
            return ""
        qr_data = f"LAUNDRY_CHECKIN:{booking_id}"
        qr = qrcode.QRCode(version=1, box_size=8, border=2)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format='PNG')
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"

    # ===== SMS NOTIFICATIONS =====
    def _send_sms(self, phone: str, message: str) -> bool:
        if self.sms_sender:
            try:
                return self.sms_sender(phone, message)
            except Exception as e:
                print(f"SMS failed: {e}")
        return False

    def _notify_booking_confirmed(self, booking: Booking):
        msg = (f"🧺 Booking confirmed!\n"
               f"Machine: {booking.machine_id}\n"
               f"Time: {booking.start_dt.strftime('%b %d, %I:%M %p')}\n"
               f"Duration: {booking.duration_minutes} min\n"
               f"Code: {booking.check_in_code}\n"
               f"Scan QR on machine to start.")
        self._send_sms(booking.customer_phone, msg)

    def _notify_booking_reminder(self, booking: Booking):
        msg = (f"⏰ Reminder: Your {booking.machine_id} booking starts in 10 min "
               f"({booking.start_dt.strftime('%I:%M %p')}). Code: {booking.check_in_code}")
        self._send_sms(booking.customer_phone, msg)

    def _notify_booking_started(self, booking: Booking):
        msg = (f"🔄 Cycle started on {booking.machine_id}. "
               f"Estimated done: {booking.end_dt.strftime('%I:%M %p')}.")
        self._send_sms(booking.customer_phone, msg)

    def _notify_booking_completed(self, booking: Booking):
        msg = f"✅ Your cycle on {booking.machine_id} is complete. Please collect your laundry."
        self._send_sms(booking.customer_phone, msg)

    def _notify_waitlist(self, entry: WaitlistEntry, machine_id: str):
        msg = (f"🎉 A {entry.machine_type.value} ({machine_id}) is now available! "
               f"Book now: [your-booking-url]")
        if self._send_sms(entry.customer_phone, msg):
            entry.notified = True
            entry.notified_at = datetime.now().isoformat()
            self._save_waitlist_entry(entry)

    # ===== MACHINE QUERIES =====
    def get_machine(self, machine_id: str) -> Optional[Machine]:
        row = self.db.fetchone("SELECT * FROM machines WHERE id = ?", (machine_id,))
        return self._row_to_machine(row) if row else None

    def list_machines(self, machine_type: Optional[MachineType] = None) -> list[Machine]:
        with self._lock:
            if machine_type:
                rows = self.db.fetchall("SELECT * FROM machines WHERE type = ? ORDER BY id", (machine_type.value,))
            else:
                rows = self.db.fetchall("SELECT * FROM machines ORDER BY id")
            return [self._row_to_machine(r) for r in rows]

    def check_availability(
        self, machine_type: MachineType, start_time: datetime, duration_minutes: int
    ) -> list[Machine]:
        with self._lock:
            end_time = start_time + timedelta(minutes=duration_minutes)
            available = []
            for machine in self.list_machines(machine_type):
                if machine.status == MachineStatus.MAINTENANCE:
                    continue
                # Check for conflicting bookings
                conflict = False
                rows = self.db.fetchall(
                    """SELECT * FROM bookings WHERE machine_id = ?
                       AND status NOT IN (?, ?, ?)""",
                    (machine.id, BookingStatus.CANCELLED.value, BookingStatus.COMPLETED.value, BookingStatus.NO_SHOW.value)
                )
                for row in rows:
                    b = self._row_to_booking(row)
                    b_start = b.start_dt
                    b_end = b.end_dt
                    if start_time < b_end and end_time > b_start:
                        conflict = True
                        break
                if not conflict:
                    available.append(machine)
            return available

    # ===== BOOKINGS =====
    def create_booking(
        self,
        machine_id: str,
        customer_name: str,
        customer_phone: str,
        start_time: datetime,
        duration_minutes: int,
    ) -> Optional[Booking]:
        with self._lock:
            machine = self.get_machine(machine_id)
            if not machine:
                return None

            available = self.check_availability(machine.type, start_time, duration_minutes)
            if machine not in available:
                return None

            booking_id = f"B{datetime.now().strftime('%Y%m%d%H%M%S')}"
            now_iso = datetime.now().isoformat()
            booking = Booking(
                id=booking_id,
                machine_id=machine_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                start_time=start_time.isoformat(),
                duration_minutes=duration_minutes,
                created_at=now_iso,
            )

            # Save booking
            self.db.execute(
                """INSERT INTO bookings (id, machine_id, customer_name, customer_phone,
                   start_time, duration_minutes, created_at, status, check_in_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (booking.id, booking.machine_id, booking.customer_name, booking.customer_phone,
                 booking.start_time, booking.duration_minutes, booking.created_at,
                 booking.status, booking.check_in_code)
            )

            # Update machine
            self.db.execute(
                "UPDATE machines SET status = ?, current_booking = ? WHERE id = ?",
                (MachineStatus.BOOKED.value, booking_id, machine_id)
            )

            # Generate check-in QR
            self.get_booking_qr_base64(booking_id)

            # Send confirmation
            self._notify_booking_confirmed(booking)
            return booking

    def cancel_booking(self, booking_id: str) -> bool:
        with self._lock:
            booking = self.get_booking(booking_id)
            if not booking or booking.status == BookingStatus.CANCELLED.value:
                return False

            self.db.execute(
                "UPDATE bookings SET status = ? WHERE id = ?",
                (BookingStatus.CANCELLED.value, booking_id)
            )

            machine = self.get_machine(booking.machine_id)
            if machine and machine.current_booking == booking_id:
                self.db.execute(
                    "UPDATE machines SET status = ?, current_booking = NULL WHERE id = ?",
                    (MachineStatus.AVAILABLE.value, booking.machine_id)
                )

            # Notify waitlist
            self._notify_waitlist_for_machine(booking.machine_id)
            return True

    def start_booking(self, booking_id: str) -> bool:
        with self._lock:
            booking = self.get_booking(booking_id)
            if not booking or booking.status != BookingStatus.CONFIRMED.value:
                return False

            self.db.execute(
                "UPDATE bookings SET status = ? WHERE id = ?",
                (BookingStatus.IN_PROGRESS.value, booking_id)
            )

            machine = self.get_machine(booking.machine_id)
            if machine:
                self.db.execute(
                    "UPDATE machines SET status = ? WHERE id = ?",
                    (MachineStatus.IN_USE.value, booking.machine_id)
                )

            self._notify_booking_started(booking)
            return True

    def complete_booking(self, booking_id: str) -> bool:
        with self._lock:
            booking = self.get_booking(booking_id)
            if not booking or booking.status == BookingStatus.COMPLETED.value:
                return False

            now_iso = datetime.now().isoformat()
            self.db.execute(
                "UPDATE bookings SET status = ?, completed_at = ? WHERE id = ?",
                (BookingStatus.COMPLETED.value, now_iso, booking_id)
            )

            machine = self.get_machine(booking.machine_id)
            if machine and machine.current_booking == booking_id:
                self.db.execute(
                    "UPDATE machines SET status = ?, current_booking = NULL WHERE id = ?",
                    (MachineStatus.AVAILABLE.value, booking.machine_id)
                )

            self._notify_booking_completed(booking)
            self._notify_waitlist_for_machine(booking.machine_id)
            return True

    def check_in_booking(self, booking_id: str, code: str) -> bool:
        with self._lock:
            booking = self.get_booking(booking_id)
            if not booking or booking.check_in_code != code:
                return False
            if booking.status != BookingStatus.CONFIRMED.value:
                return False
            return self.start_booking(booking_id)

    def get_booking(self, booking_id: str) -> Optional[Booking]:
        row = self.db.fetchone("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        return self._row_to_booking(row) if row else None

    def list_bookings(
        self,
        date: Optional[datetime] = None,
        status: Optional[str] = None,
        machine_id: Optional[str] = None,
    ) -> list[Booking]:
        with self._lock:
            query = "SELECT * FROM bookings WHERE 1=1"
            params = []
            if date:
                query += " AND date(start_time) = ?"
                params.append(date.date().isoformat())
            if status:
                query += " AND status = ?"
                params.append(status)
            if machine_id:
                query += " AND machine_id = ?"
                params.append(machine_id)
            query += " ORDER BY start_time DESC"
            rows = self.db.fetchall(query, tuple(params))
            return [self._row_to_booking(r) for r in rows]

    def get_upcoming_bookings(self, minutes_ahead: int = 60) -> list[Booking]:
        now = datetime.now()
        cutoff = now + timedelta(minutes=minutes_ahead)
        return [
            b for b in self.list_bookings(status=BookingStatus.CONFIRMED.value)
            if now <= b.start_dt <= cutoff
        ]

    # ===== AUTO-COMPLETE =====
    def start_auto_complete(self, interval_seconds: int = 30):
        if self._auto_complete_running:
            return
        self._auto_complete_running = True
        self._auto_complete_thread = threading.Thread(
            target=self._auto_complete_loop,
            args=(interval_seconds,),
            daemon=True
        )
        self._auto_complete_thread.start()

    def stop_auto_complete(self):
        self._auto_complete_running = False
        if self._auto_complete_thread:
            self._auto_complete_thread.join(timeout=5)

    def _auto_complete_loop(self, interval_seconds: int):
        while self._auto_complete_running:
            try:
                self._process_auto_complete()
            except Exception as e:
                print(f"Auto-complete error: {e}")
            time.sleep(interval_seconds)

    def _process_auto_complete(self):
        with self._lock:
            now = datetime.now()
            # Auto-complete expired bookings
            rows = self.db.fetchall(
                "SELECT * FROM bookings WHERE status = ?",
                (BookingStatus.IN_PROGRESS.value,)
            )
            for row in rows:
                b = self._row_to_booking(row)
                if b.is_expired:
                    self.complete_booking(b.id)

            # Mark no-shows
            rows = self.db.fetchall(
                "SELECT * FROM bookings WHERE status = ?",
                (BookingStatus.CONFIRMED.value,)
            )
            for row in rows:
                b = self._row_to_booking(row)
                if now > b.end_dt + timedelta(minutes=15):
                    self.db.execute(
                        "UPDATE bookings SET status = ? WHERE id = ?",
                        (BookingStatus.NO_SHOW.value, b.id)
                    )
                    machine = self.get_machine(b.machine_id)
                    if machine and machine.current_booking == b.id:
                        self.db.execute(
                            "UPDATE machines SET status = ?, current_booking = NULL WHERE id = ?",
                            (MachineStatus.AVAILABLE.value, b.machine_id)
                        )
                    self._notify_waitlist_for_machine(b.machine_id)

    def _notify_waitlist_for_machine(self, machine_id: str):
        machine = self.get_machine(machine_id)
        if not machine:
            return
        for entry in self.get_waitlist(machine.type):
            if not entry.notified:
                self._notify_waitlist(entry, machine_id)

    # ===== WAITLIST =====
    def join_waitlist(
        self, customer_name: str, customer_phone: str, machine_type: MachineType
    ) -> WaitlistEntry:
        with self._lock:
            entry_id = f"WL{datetime.now().strftime('%Y%m%d%H%M%S')}"
            now_iso = datetime.now().isoformat()
            entry = WaitlistEntry(
                id=entry_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                machine_type=machine_type,
                created_at=now_iso,
            )
            self.db.execute(
                """INSERT INTO waitlist (id, customer_name, customer_phone, machine_type, created_at, notified)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (entry.id, entry.customer_name, entry.customer_phone,
                 entry.machine_type.value, entry.created_at, entry.notified)
            )
            return entry

    def leave_waitlist(self, entry_id: str) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM waitlist WHERE id = ?", (entry_id,))
            return True

    def _save_waitlist_entry(self, entry: WaitlistEntry):
        self.db.execute(
            "UPDATE waitlist SET notified = ?, notified_at = ? WHERE id = ?",
            (entry.notified, entry.notified_at, entry.id)
        )

    def get_waitlist(self, machine_type: Optional[MachineType] = None) -> list[WaitlistEntry]:
        with self._lock:
            if machine_type:
                rows = self.db.fetchall(
                    "SELECT * FROM waitlist WHERE machine_type = ? ORDER BY created_at",
                    (machine_type.value,)
                )
            else:
                rows = self.db.fetchall("SELECT * FROM waitlist ORDER BY created_at")
            return [self._row_to_waitlist(r) for r in rows]

    def get_waitlist_entry(self, entry_id: str) -> Optional[WaitlistEntry]:
        row = self.db.fetchone("SELECT * FROM waitlist WHERE id = ?", (entry_id,))
        return self._row_to_waitlist(row) if row else None

    # ===== MAINTENANCE =====
    def set_maintenance(self, machine_id: str, in_maintenance: bool) -> bool:
        with self._lock:
            machine = self.get_machine(machine_id)
            if not machine:
                return False
            if in_maintenance:
                if machine.status == MachineStatus.IN_USE:
                    return False
                status = MachineStatus.MAINTENANCE.value
            else:
                status = MachineStatus.AVAILABLE.value
            self.db.execute(
                "UPDATE machines SET status = ? WHERE id = ?",
                (status, machine_id)
            )
            return True

    # ===== STATS =====
    def get_stats(self) -> dict:
        with self._lock:
            now = datetime.now()
            today = now.date().isoformat()
            stats = {}

            # Machine counts
            for status in ["available", "in_use", "booked", "maintenance"]:
                row = self.db.fetchone(
                    "SELECT COUNT(*) as c FROM machines WHERE status = ?", (status,)
                )
                stats[status] = row["c"]

            stats["total_machines"] = sum(stats.values())

            # Bookings today
            row = self.db.fetchone(
                "SELECT COUNT(*) as c FROM bookings WHERE date(start_time) = ?", (today,)
            )
            stats["bookings_today"] = row["c"]

            # Active bookings
            row = self.db.fetchone(
                "SELECT COUNT(*) as c FROM bookings WHERE status = ?", (BookingStatus.IN_PROGRESS.value,)
            )
            stats["active_bookings"] = row["c"]

            # Waitlist
            row = self.db.fetchone("SELECT COUNT(*) as c FROM waitlist")
            stats["waitlist_count"] = row["c"]

            return stats


# ===== TWILIO SMS SENDER (optional) =====
def create_twilio_sender(account_sid: str, auth_token: str, from_number: str) -> Callable[[str, str], bool]:
    """Returns an SMS sender function using Twilio."""
    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)

        def send(phone: str, message: str) -> bool:
            if not phone.startswith('+'):
                phone = '+1' + phone.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
            client.messages.create(body=message, from_=from_number, to=phone)
            return True
        return send
    except ImportError:
        print("Twilio not installed. Run: pip install twilio")
        return lambda phone, msg: False


# ===== CONVENIENCE =====
def create_system(**kwargs) -> LaundromatBookingSystem:
    """Factory function for easy instantiation."""
    return LaundromatBookingSystem(**kwargs)