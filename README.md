# Laundromat Booking System

A complete automated booking system for laundromats with customer self-service portal, admin dashboard, QR code check-in, waitlist, and auto-complete.

## Features

- **Customer Portal** - Mobile-first web app for booking washers/dryers
- **Admin Dashboard** - Real-time machine status, booking management, waitlist
- **QR Code Check-in** - Scan machine QR → enter 4-digit code → cycle starts
- **Waitlist** - Join queue → SMS notification when machine frees
- **Auto-complete** - Cycles auto-complete 10min after end time
- **No-show detection** - Releases machines 15min past end time
- **SMS Ready** - Twilio integration for confirmations/reminders

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server
python -m uvicorn customer_portal:app --host 0.0.0.0 --port 8000

# 3. Open in browser
# http://localhost:8000          - Customer portal
# http://localhost:8000/admin    - Admin dashboard
# http://localhost:8000/waitlist - Waitlist page
```

## CLI Usage (same data file)

```bash
# List available machines
python laundromat_booking.py list washer
python laundromat_booking.py list dryer

# Check availability
python laundromat_booking.py available washer "2026-10-28 14:30" 45

# Create booking
python laundromat_booking.py book W1 "John Doe" "555-1234" "2026-10-28 14:30" 45

# View bookings
python laundromat_booking.py bookings

# Cancel booking
python laundromat_booking.py cancel B123456789

# Toggle maintenance
python laundromat_booking.py maintenance W1 on
```

## Project Structure

```
laundromat-project/
├── laundromat_booking.py    # Core system (thread-safe, auto-complete, QR, waitlist)
├── customer_portal.py       # FastAPI web app
├── requirements.txt         # Python dependencies
├── laundromat_data.json     # Auto-generated data file
├── qr_codes/               # Machine QR codes (auto-generated)
│   ├── W1.png ... W4.png
│   └── D1.png ... D4.png
├── templates/              # Jinja2 templates
│   ├── base.html
│   ├── home.html
│   ├── book.html
│   ├── confirm.html
│   ├── status.html
│   ├── waitlist.html
│   ├── waitlist_confirm.html
│   └── admin.html
└── static/                 # Static assets (CSS, JS, images)
```

## SMS Notifications (Optional)

```python
# In customer_portal.py, add Twilio credentials:
from laundromat_booking import create_twilio_sender

sms_sender = create_twilio_sender(
    "YOUR_ACCOUNT_SID",
    "YOUR_AUTH_TOKEN",
    "+15551234567"  # Your Twilio phone number
)

system = LaundromatBookingSystem(
    data_file="laundromat_data.json",
    qr_dir="qr_codes",
    sms_sender=sms_sender,
    auto_complete=True,
)
```

## Machine QR Codes

Print and stick on each machine:
- `qr_codes/W1.png` through `W4.png` - Washers
- `qr_codes/D1.png` through `D4.png` - Dryers

Customers scan the QR → opens booking page for that machine.

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/machines` | List all machines with availability |
| `GET /api/machines/{id}/availability?start=...&duration=...` | Check specific availability |
| `GET /api/bookings/{id}` | Get booking status |
| `DELETE /api/waitlist/{id}` | Remove from waitlist |

## Production Deployment

1. Set `BASE_URL` in `customer_portal.py`
2. Add Twilio credentials for SMS
3. Use a process manager (systemd, PM2, Docker)
4. Set up reverse proxy (nginx) with SSL
5. Configure data backup for `laundromat_data.json`

## License

MIT - Feel free to use and modify.