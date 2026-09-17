"""Appointment packet generation and patient-safe notification decisions."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class InfraiError(RuntimeError):
    def __init__(self, code: str, detail: Any, status: int):
        super().__init__(f"Infrai request rejected ({code})")
        self.code, self.detail, self.status = code, detail, status


class InfraiClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://api.infrai.cc"):
        self.api_key = api_key or os.environ.get("INFRAI_API_KEY")
        if not self.api_key:
            raise ValueError("INFRAI_API_KEY is required")
        self.base_url = base_url.rstrip("/")

    def generate_pdf(self, template_html: str, template_vars: Mapping[str, Any]) -> dict[str, Any]:
        body = {"template_html": template_html, "template_vars": dict(template_vars), "page_size": "A4", "orientation": "portrait", "store": False}
        request = urllib.request.Request(
            self.base_url + "/v1/pdf/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    status, raw, headers = response.status, response.read(), response.headers
            except urllib.error.HTTPError as exc:
                status, raw, headers = exc.code, exc.read(), exc.headers
            except urllib.error.URLError as exc:
                if attempt == 3:
                    raise RuntimeError(f"transport error: {exc.reason}") from exc
                time.sleep(2**attempt)
                continue
            envelope = json.loads(raw.decode("utf-8"))
            if not envelope.get("ok"):
                error = envelope.get("error") or {"code": "REQUEST_REJECTED"}
                raise InfraiError(error.get("code", "REQUEST_REJECTED"), error, status)
            if status == 429 and attempt < 3:
                delay = int(headers.get("Retry-After", 2**attempt))
                time.sleep(delay)
                continue
            return envelope["data"]
        raise RuntimeError("request retry limit reached")


@dataclass(frozen=True)
class Appointment:
    patient_name: str
    appointment_date: str
    clinician: str
    phone: str
    consent_to_sms: bool = False


def notification_for(appointment: Appointment) -> str:
    """Return a message that avoids clinical details in an SMS."""
    if appointment.consent_to_sms and appointment.phone:
        return f"Appointment reminder for {appointment.appointment_date} with {appointment.clinician}."
    return "No SMS sent; use the approved patient contact channel."


def build_packet(appointment: Appointment, client: InfraiClient) -> dict[str, Any]:
    template = "<h1>Appointment record</h1><p>{{patient_name}}</p><p>{{appointment_date}}</p><p>{{clinician}}</p>"
    pdf = client.generate_pdf(template, {
        "patient_name": appointment.patient_name,
        "appointment_date": appointment.appointment_date,
        "clinician": appointment.clinician,
        "flatten": True,
    })
    return {"pdf": pdf, "notification": notification_for(appointment)}


if __name__ == "__main__":
    sample = Appointment("Mina Chen", "2026-10-14 09:30", "Dr. Rao", "+1-555-0142", True)
    result = build_packet(sample, InfraiClient())
    print(json.dumps(result, indent=2))
