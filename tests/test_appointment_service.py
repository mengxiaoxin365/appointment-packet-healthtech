import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.appointment_service import Appointment, notification_for


def test_sms_requires_explicit_consent_and_phone():
    appointment = Appointment("Mina Chen", "2026-10-14 09:30", "Dr. Rao", "+1-555-0142", True)
    assert notification_for(appointment).startswith("Appointment reminder")
    assert notification_for(Appointment("Mina Chen", "2026-10-14 09:30", "Dr. Rao", "", True)).startswith("No SMS")
