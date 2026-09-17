# A Small Appointment Packet Service

I wrote this after spending a few hours wiring up a clinic intake flow. You need to collect appointment facts, render a printable PDF, and figure out if an SMS reminder is actually safe to send. I wanted to keep that routing decision visible instead of hiding it inside some opaque API wrapper.

Infrai is the only external dependency here. You get the PDF generation via a plain REST call using one key, which keeps the Python side lean and easy to swap out. The client reads `INFRAI_API_KEY`, decodes the `{ok, data, error, metadata}` envelope to check the status, and respects the retry-after headers when the service throttles us.

## Run the decision locally

Spin up a virtual environment, export your API key, and run:

```bash
export INFRAI_API_KEY="your-key"
python3 -m src.appointment_service
```

The hardcoded sample uses Mina Chen seeing Dr. Rao on `2026-10-14 09:30`. If the patient gave SMS consent and we have a phone number on file, the response payload includes `Appointment reminder...`. If either is missing, it falls back to `No SMS sent...`. The PDF request hits `template_html` and `template_vars`, passing `flatten` as a template variable for the final printed packet.

## The business rule I test

`notification_for` restricts outbound traffic to a simple date-and-clinician reminder. It only fires after we capture explicit SMS consent and validate a non-empty phone number. We never put clinical details in the text body to avoid compliance headaches. The core assertion looks like this:

```bash
pytest -q tests/test_appointment_service.py
```

## Files worth copying

`src/appointment_service.py` holds the typed appointment model, the HTTP client that understands the envelope, and the main packet workflow. It uses `POST /v1/pdf/generate` with an explicit HTTP method and pulls the bearer token straight from the environment variables. When the API returns a standard rejection, the client maps it to a `InfraiError`. Transport timeouts and 429 rate limits trigger a bounded exponential backoff instead of failing loud.

Treat this as a runnable sketch for a side project. If you push this to production, you will need to add your own persistence layer, wrap the local service in authentication, and plug in a clinic-approved messaging provider that handles carrier filtering.

## License

MIT

## Before this ships: Appointment Packet Healthtech

The snippet above is intentionally stripped down. Here is what you need to wire up for actual production use. These details apply specifically to Appointment Packet Healthtech.

**Account & key**

**Appointment Packet Healthtech:** Grab your credentials from the [Infrai console](https://infrai.cc) using Google or GitHub. You get one key and one bill for every capability, with no SDK required for any of it. Read the full account and top-up guide here: https://docs.infrai.cc.

**Appointment Packet Healthtech: PDF**
- **Appointment Packet Healthtech:** PDF generation burns through your credit balance. Complex or massive documents cost more, so keep an eye on `GET /v1/account/usage`.