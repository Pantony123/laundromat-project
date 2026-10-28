#!/usr/bin/env python3
"""
Customer-Facing Laundromat Booking Portal
Clean, mobile-first FastAPI web application.
Run: uvicorn customer_portal:app --reload --port 8000
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr

from laundromat_booking import (
    LaundromatBookingSystem,
    MachineType,
    MachineStatus,
    BookingStatus,
    create_twilio_sender,
)

# ===== CONFIG =====
BASE_URL = "http://localhost:8000"  # Change for production

# Initialize system with optional Twilio
# sms_sender = create_twilio_sender("SID", "TOKEN", "+15551234567") if you have Twilio
system = LaundromatBookingSystem(
    qr_dir="qr_codes",
    sms_sender=None,  # Add your Twilio sender here
    auto_complete=True,
)

# ===== FASTAPI APP =====
app = FastAPI(title="Laundromat Booking", version="2.0.0")

# Static files & templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ===== PYDANTIC MODELS =====
class BookingRequest(BaseModel):
    machine_id: str
    customer_name: str
    customer_phone: str
    start_time: str  # ISO format from datetime-local
    duration_minutes: int

class WaitlistRequest(BaseModel):
    customer_name: str
    customer_phone: str
    machine_type: str  # "washer" or "dryer"

class CheckInRequest(BaseModel):
    booking_id: str
    code: str

# ===== HELPER FUNCTIONS =====
def get_available_machines(mtype: Optional[MachineType] = None):
    """Get machines available for booking right now (next 2 hours)."""
    now = datetime.now()
    soon = now + timedelta(hours=2)
    machines = system.list_machines(mtype)
    result = []
    for m in machines:
        if m.status == MachineStatus.MAINTENANCE:
            continue
        # Check if machine has a booking starting soon
        has_soon_booking = any(
            b.machine_id == m.id and b.status == BookingStatus.CONFIRMED.value
            and now <= b.start_dt <= soon
            for b in system.list_bookings()
        )
        result.append({
            "id": m.id,
            "type": m.type.value,
            "status": m.status.value,
            "location": m.location_notes,
            "available_now": m.status == MachineStatus.AVAILABLE and not has_soon_booking,
            "qr_base64": system.get_qr_base64(m.id),
        })
    return result

def format_datetime(dt: datetime) -> str:
    return dt.strftime("%b %d, %I:%M %p")

def format_datetime_input(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")

# ===== ROUTES =====

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Landing page - shows available machines."""
    washers = get_available_machines(MachineType.WASHER)
    dryers = get_available_machines(MachineType.DRYER)
    stats = system.get_stats()
    return templates.TemplateResponse(request, "home.html", {
        "washers": washers,
        "dryers": dryers,
        "stats": stats,
        "now": format_datetime_input(datetime.now()),
        "soon": format_datetime_input(datetime.now() + timedelta(hours=2)),
    })

@app.get("/book/{machine_id}", response_class=HTMLResponse)
async def book_page(request: Request, machine_id: str):
    """Booking form for a specific machine."""
    machine = system.get_machine(machine_id)
    if not machine:
        raise HTTPException(404, "Machine not found")

    # Check availability for next few hours
    now = datetime.now()
    bookings = system.list_bookings(machine_id=machine_id)
    upcoming = [b for b in bookings if b.status == BookingStatus.CONFIRMED.value and b.start_dt >= now]

    return templates.TemplateResponse(request, "book.html", {
        "machine": {
            "id": machine.id,
            "type": machine.type.value,
            "location": machine.location_notes,
            "qr_base64": system.get_qr_base64(machine_id),
        },
        "upcoming": [
            {"start": format_datetime(b.start_dt), "duration": b.duration_minutes}
            for b in upcoming[:5]
        ],
        "default_start": format_datetime_input(now),
        "default_end": format_datetime_input(now + timedelta(hours=1)),
    })

@app.post("/book/{machine_id}")
async def create_booking(
    machine_id: str,
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    start_time: str = Form(...),
    duration_minutes: int = Form(...),
):
    """Process booking submission."""
    try:
        start_dt = datetime.fromisoformat(start_time)
    except ValueError:
        raise HTTPException(400, "Invalid date/time format")

    if start_dt < datetime.now():
        raise HTTPException(400, "Start time cannot be in the past")

    if duration_minutes < 15 or duration_minutes > 180:
        raise HTTPException(400, "Duration must be 15-180 minutes")

    booking = system.create_booking(
        machine_id, customer_name.strip(), customer_phone.strip(),
        start_dt, duration_minutes
    )

    if not booking:
        raise HTTPException(409, "Machine not available at that time")

    return RedirectResponse(url=f"/confirm/{booking.id}", status_code=303)

@app.get("/confirm/{booking_id}", response_class=HTMLResponse)
async def booking_confirmation(request: Request, booking_id: str):
    """Booking confirmation page with check-in QR."""
    booking = system.get_booking(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    machine = system.get_machine(booking.machine_id)

    return templates.TemplateResponse(request, "confirm.html", {
        "booking": {
            "id": booking.id,
            "machine_id": booking.machine_id,
            "machine_type": machine.type.value if machine else "Unknown",
            "customer_name": booking.customer_name,
            "start": format_datetime(booking.start_dt),
            "duration": booking.duration_minutes,
            "code": booking.check_in_code,
            "qr_base64": system.get_booking_qr_base64(booking_id),
            "status": booking.status,
        },
    })

@app.get("/status/{booking_id}", response_class=HTMLResponse)
async def booking_status(request: Request, booking_id: str):
    """Real-time booking status page (for customers to check)."""
    booking = system.get_booking(booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found")

    machine = system.get_machine(booking.machine_id)
    time_until = ""
    if booking.status == BookingStatus.CONFIRMED.value:
        delta = booking.start_dt - datetime.now()
        if delta.total_seconds() > 0:
            mins = int(delta.total_seconds() / 60)
            time_until = f"{mins} min" if mins < 60 else f"{mins//60}h {mins%60}m"

    return templates.TemplateResponse(request, "status.html", {
        "booking": {
            "id": booking.id,
            "machine_id": booking.machine_id,
            "machine_type": machine.type.value if machine else "Unknown",
            "customer_name": booking.customer_name,
            "start": format_datetime(booking.start_dt),
            "end": format_datetime(booking.end_dt),
            "duration": booking.duration_minutes,
            "code": booking.check_in_code,
            "status": booking.status,
            "time_until": time_until,
            "qr_base64": system.get_booking_qr_base64(booking_id),
        },
    })

@app.post("/checkin/{booking_id}")
async def checkin_booking(booking_id: str, code: str = Form(...)):
    """Customer checks in via QR scan + code entry."""
    success = system.check_in_booking(booking_id, code)
    if not success:
        raise HTTPException(400, "Invalid code or booking not confirmable")
    return RedirectResponse(url=f"/status/{booking_id}", status_code=303)

@app.get("/waitlist", response_class=HTMLResponse)
async def waitlist_page(request: Request):
    """Join waitlist page."""
    return templates.TemplateResponse(request, "waitlist.html", {
        "machine_types": ["washer", "dryer"],
    })

@app.post("/waitlist")
async def join_waitlist(
    customer_name: str = Form(...),
    customer_phone: str = Form(...),
    machine_type: str = Form(...),
):
    """Add to waitlist."""
    try:
        mtype = MachineType(machine_type)
    except ValueError:
        raise HTTPException(400, "Invalid machine type")

    entry = system.join_waitlist(
        customer_name.strip(), customer_phone.strip(), mtype
    )
    return RedirectResponse(url=f"/waitlist/confirm/{entry.id}", status_code=303)

@app.get("/waitlist/confirm/{entry_id}", response_class=HTMLResponse)
async def waitlist_confirm(request: Request, entry_id: str):
    """Waitlist confirmation."""
    entry = system.get_waitlist_entry(entry_id)
    if not entry:
        raise HTTPException(404, "Waitlist entry not found")
    return templates.TemplateResponse(request, "waitlist_confirm.html", {
        "entry": entry,
        "position": len([e for e in system.get_waitlist(entry.machine_type) if e.created_at <= entry.created_at]),
    })

@app.delete("/api/waitlist/{entry_id}")
async def api_remove_waitlist(entry_id: str):
    """Remove from waitlist."""
    success = system.leave_waitlist(entry_id)
    if not success:
        raise HTTPException(404, "Waitlist entry not found")
    return {"success": True}

# ===== API ENDPOINTS (for AJAX/mobile apps) =====
@app.get("/api/machines")
async def api_machines(type: Optional[str] = None):
    mtype = MachineType(type) if type else None
    return get_available_machines(mtype)

@app.get("/api/machines/{machine_id}/availability")
async def api_availability(machine_id: str, start: str, duration: int):
    try:
        dt = datetime.fromisoformat(start)
    except ValueError:
        raise HTTPException(400, "Invalid datetime")
    m = system.get_machine(machine_id)
    if not m:
        raise HTTPException(404, "Machine not found")
    available = system.check_availability(m.type, dt, duration)
    return {"available": any(a.id == machine_id for a in available)}

@app.get("/api/bookings/{booking_id}")
async def api_booking_status(booking_id: str):
    booking = system.get_booking(booking_id)
    if not booking:
        raise HTTPException(404, "Not found")
    return {
        "id": booking.id,
        "status": booking.status,
        "start": booking.start_time,
        "end": booking.end_dt.isoformat(),
    }

# ===== ADMIN ROUTES =====
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Staff admin dashboard."""
    stats = system.get_stats()
    machines = system.list_machines()
    bookings_today = system.list_bookings(date=datetime.now())
    waitlist = system.get_waitlist()

    return templates.TemplateResponse(request, "admin.html", {
        "request": request,
        "stats": stats,
        "machines": [
            {"id": m.id, "type": m.type.value, "status": m.status.value, "location": m.location_notes}
            for m in machines
        ],
        "bookings": [
            {
                "id": b.id,
                "machine": b.machine_id,
                "customer": b.customer_name,
                "phone": b.customer_phone,
                "start": format_datetime(b.start_dt),
                "duration": b.duration_minutes,
                "status": b.status,
            }
            for b in bookings_today
        ],
        "waitlist": [
            {"id": w.id, "name": w.customer_name, "phone": w.customer_phone, "type": w.machine_type.value}
            for w in waitlist
        ],
    })

@app.post("/admin/machine/{machine_id}/maintenance")
async def admin_toggle_maintenance(machine_id: str, maintenance: bool = Form(...)):
    success = system.set_maintenance(machine_id, maintenance)
    if not success:
        raise HTTPException(400, "Cannot set maintenance (machine in use)")
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/booking/{booking_id}/complete")
async def admin_complete_booking(booking_id: str):
    success = system.complete_booking(booking_id)
    if not success:
        raise HTTPException(400, "Cannot complete")
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/booking/{booking_id}/cancel")
async def admin_cancel_booking(booking_id: str):
    success = system.cancel_booking(booking_id)
    if not success:
        raise HTTPException(400, "Cannot cancel")
    return RedirectResponse(url="/admin", status_code=303)

# ===== HEALTH CHECK =====
@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ===== ERROR HANDLERS (friendly pages instead of black text) =====
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return templates.TemplateResponse(request, "error.html", {
        "status_code": 404,
        "title": "Page Not Found",
        "message": "The page you're looking for doesn't exist.",
    }, status_code=404)

@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    # Log the actual error for debugging
    print(f"❌ Internal error: {exc}")
    return templates.TemplateResponse(request, "error.html", {
        "status_code": 500,
        "title": "Something Went Wrong",
        "message": "We're having trouble processing your request. Please try again.",
    }, status_code=500)

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        return await not_found_handler(request, exc)
    return templates.TemplateResponse(request, "error.html", {
        "status_code": exc.status_code,
        "title": "Error",
        "message": exc.detail,
    }, status_code=exc.status_code)


# ===== CATCH-ALL 404 (must be last route) =====
@app.get("/{path:path}")
async def catch_all(request: Request, path: str):
    raise HTTPException(404, "Page not found")


# ===== STARTUP / SHUTDOWN =====
@app.on_event("startup")
async def startup():
    print(f"🚀 Laundromat Portal started at {BASE_URL}")
    print(f"   Admin: {BASE_URL}/admin")
    print(f"   QR codes: {Path('qr_codes').absolute()}")

@app.on_event("shutdown")
async def shutdown():
    system.stop_auto_complete()
    print("👋 Shutdown complete")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)