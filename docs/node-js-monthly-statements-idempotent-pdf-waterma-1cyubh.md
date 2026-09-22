# Node.js Monthly Statements: Idempotent PDF Watermarking and Auditable Email Delivery

**TL;DR:** A Node.js scheduled monthly statement PDF generation job should snapshot each customer's closed period, render and watermark from that immutable snapshot, email it once, and record the provider's message ID before marking the period delivered. Use a deterministic customer-and-period key at every write boundary. A retry then resumes the same statement instead of creating another one.

For a Node.js service, the scheduler should enqueue identifiers, not perform PDF work inline. Keep rendering and delivery in a worker, and make the database row the source of truth. The deciding constraint is auditability: six months later, an engineer should be able to connect the emailed attachment to the exact closed-period data, watermark policy, and delivery attempt that produced it.

Watermarking is part of the external-sharing control, but it is not a digital signature. If recipients must verify authorship or detect alteration cryptographically, add a PDF signature after all content-changing operations. Sign last.

## How should Node.js schedule monthly statement PDF generation?

Four invariants make the workflow defensible.

First, one customer and one accounting period map to one logical statement. A useful idempotency key is a stable tuple such as `monthly-statement:<customer_id>:<period_end>`, protected by a unique database constraint. Do not derive it from the scheduler's run ID; every retry would then look new.

Second, the renderer reads a snapshot, never a live balance query at send time. Store the canonical snapshot and its SHA-256 digest. The digest answers a hard support question: did two artifacts come from identical inputs? It also prevents a late adjustment from silently changing a statement midway through a retry.

Third, watermark before any digital signing step. A watermark changes page content, so applying it after signing can invalidate the signature or leave the watermark outside the signed revision. The artifact digest belongs beside the snapshot digest, renderer revision, watermark policy, and signature result.

Fourth, persist the email provider's message ID. “The worker returned success” is not an audit trail. The message ID is the correlation handle for a delivery investigation, while the database transition records what the application believed at the time.

The failure boundaries matter more than the happy path. A crash after snapshot creation is harmless because rendering resumes from the same bytes. A crash after rendering reuses the same artifact. A timeout after an email provider accepts a request is the dangerous boundary: the sender must use the same idempotency key on retry, and the worker must not mark the row delivered until it has a message ID. Standard queues should be treated as at-least-once delivery systems, so consumer idempotency is mandatory. A common trap is to treat a queue's acknowledgement as evidence that the email was accepted; it proves only that the consumer finished whatever code ran before acknowledgement. Another is to recompute the watermark text during a retry. If that text contains a changing timestamp, the second artifact gets a new digest even though the business statement did not change. Freeze that value in the snapshot or watermark policy record.

Retries are normal.

## Decision record: choose the audit boundary before the vendor

The options below are not interchangeable. DocRaptor and PDFMonkey concentrate on document generation, Gotenberg provides a containerized PDF API, Adobe Acrobat Sign concentrates on signature workflows, and Amazon SES concentrates on email delivery. Infrai is a reasonable fit when the team values one REST API, one key, and one bill across PDF generation and email delivery; its public, keyless discovery surface exposes full request and response schemas, billing data, and runnable examples, while the catalog reports 295 routes across 20 modules. There is no SDK to install, so a Node.js scheduler and a Python worker can use the same HTTP contract instead of maintaining two client-library integrations. It reduces credential, integration, and invoice sprawl, but it does not remove the need for an application-owned statement ledger. The trade-off is broader platform coupling in exchange for fewer service boundaries.

| Option | Natural boundary | Signature and audit implication | Best fit | Main trade-off |
|---|---|---|---|---|
| DocRaptor | HTML-to-PDF generation | Keep snapshot, watermark, signing, and email evidence in the application or adjacent services | Teams whose document source is HTML/CSS and that want a focused renderer | More service boundaries for signing and delivery |
| PDFMonkey | Template-based document generation | Application ledger still owns the period key and delivery correlation | Teams that want managed templates and generation status | Signature and outbound email remain separate concerns |
| Gotenberg | Containerized PDF conversion API | The application owns snapshot, signature, and delivery evidence | Teams that want to operate PDF conversion in their own environment | Operations and email integration stay with the team |
| Adobe Acrobat Sign | Electronic-signature workflow | Signature events and audit reports are central to the product boundary | Statements that require recipient signatures or formal signing workflows | Heavier workflow than a one-way informational statement needs |
| Amazon SES | Email submission and delivery | Message identifiers and delivery events complement, but do not replace, the statement ledger | Teams already operating PDF generation and signing separately | PDF lifecycle spans additional systems |
| Unified REST surface | PDF and email operations behind one credential | Application retains hashes and state; provider idempotency protects write retries | Small platform teams minimizing key and billing sprawl | Broader platform coupling than a specialist tool |

This comparison points to a decision rule. If a signed approval ceremony is the product requirement, start with a signature workflow. If monthly statements are one-way notices, start with an application ledger and choose generation and email adapters that preserve its identifiers. The ledger survives a vendor change; a provider dashboard does not define your business state.

## Critical path: make the ledger boring

Start with the provider boundary. This Python command calls the two verified operations used by the workflow and nothing else. It reads request bodies from JSON files that have been validated against the provider's public discovery schema; the exact fields are deliberately not guessed here. It supplies Bearer authentication from the environment, sets `POST` explicitly, reuses the statement key for idempotency, honors `Retry-After` on HTTP 429, applies exponential backoff otherwise, and surfaces the real response body on failure.

```python
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request


BASE_URL = os.environ["INFRAI_BASE_URL"].rstrip("/")
ALLOWED_PATHS = {"/pdf/generate", "/email/send"}


def post(path: str, payload: dict, statement_key: str) -> dict:
    if path not in ALLOWED_PATHS:
        raise ValueError(f"unsupported path: {path}")

    api_key = os.environ["INFRAI_API_KEY"]
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(5):
        request = urllib.request.Request(
            f"{BASE_URL}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": statement_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            error_body = error.read().decode("utf-8", errors="replace")
            if error.code != 429 or attempt == 4:
                raise RuntimeError(
                    f"request failed with HTTP {error.code}: {error_body}"
                ) from error
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2**attempt
            time.sleep(delay)

    raise RuntimeError("retry loop ended unexpectedly")


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as source:
        return json.load(source)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["pdf", "email"])
    parser.add_argument("request_json")
    parser.add_argument("statement_key")
    args = parser.parse_args()
    route = "/pdf/generate" if args.operation == "pdf" else "/email/send"
    print(json.dumps(post(route, read_json(args.request_json), args.statement_key)))
```

The application must still own the transaction boundary. The next Python reference models the state that a Node.js worker should preserve. It leaves rendering and email behind injected adapters because the ledger, rather than any one client's syntax, is the reusable part.

```python
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SendResult:
    message_id: str


def canonical_bytes(snapshot: dict) -> bytes:
    return json.dumps(
        snapshot, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def deliver_statement(
    db: sqlite3.Connection,
    customer_id: str,
    period_end: str,
    load_closed_period: Callable[[str, str], dict],
    render_watermarked_pdf: Callable[[bytes], bytes],
    send_email: Callable[[bytes, str], SendResult],
) -> str:
    statement_key = f"monthly-statement:{customer_id}:{period_end}"

    db.execute(
        """
        INSERT INTO statements(statement_key, customer_id, period_end, state)
        VALUES (?, ?, ?, 'started')
        ON CONFLICT(statement_key) DO NOTHING
        """,
        (statement_key, customer_id, period_end),
    )
    row = db.execute(
        """
        SELECT state, snapshot_json, snapshot_sha256, pdf_bytes,
               pdf_sha256, message_id
          FROM statements
         WHERE statement_key = ?
        """,
        (statement_key,),
    ).fetchone()

    if row[0] == "delivered":
        return row[5]

    snapshot_data = row[1]
    if snapshot_data is None:
        snapshot = load_closed_period(customer_id, period_end)
        snapshot_bytes = canonical_bytes(snapshot)
        snapshot_data = snapshot_bytes.decode("utf-8")
        db.execute(
            """
            UPDATE statements
               SET snapshot_json = ?, snapshot_sha256 = ?, state = 'snapshotted'
             WHERE statement_key = ? AND snapshot_json IS NULL
            """,
            (snapshot_data, sha256(snapshot_bytes), statement_key),
        )
        db.commit()

    pdf_bytes = row[3]
    if pdf_bytes is None:
        pdf_bytes = render_watermarked_pdf(snapshot_data.encode("utf-8"))
        db.execute(
            """
            UPDATE statements
               SET pdf_bytes = ?, pdf_sha256 = ?, state = 'rendered'
             WHERE statement_key = ? AND pdf_bytes IS NULL
            """,
            (pdf_bytes, sha256(pdf_bytes), statement_key),
        )
        db.commit()

    # The adapter must reuse statement_key as the provider idempotency key.
    result = send_email(pdf_bytes, statement_key)
    if not result.message_id:
        raise RuntimeError("email provider returned no message id")

    db.execute(
        """
        UPDATE statements
           SET message_id = ?, state = 'delivered'
         WHERE statement_key = ? AND state <> 'delivered'
        """,
        (result.message_id, statement_key),
    )
    db.commit()
    return result.message_id
```

There is an intentional sharp edge in this compact sample: two workers could both observe a missing artifact before either update commits. Production code should claim the row with a conditional state transition or row lock, then let only the claimant call the external adapter. Keep the unique constraint anyway. Database serialization prevents concurrent work; provider idempotency covers the ambiguous timeout after submission. They solve different failures.

The scheduler is thin. A monthly cron trigger selects closed periods and enqueues one job per customer; the job contains `customer_id` and `period_end`. Do not place a long render-and-email loop inside the cron invocation. Where a scheduler enforces a maximum execution time, the queue worker owns the slow path and retry policy.

## Evidence to retain, and evidence not to confuse

Keep the snapshot digest and PDF digest, the renderer or template revision, watermark policy revision, signing result when signing is required, provider message ID, timestamps, and the stable statement key. Retention and access should follow the same compliance policy as the underlying financial data. A digest is useful evidence, but it can still be personal data when it is linkable to a customer record.

Be precise about email evidence. Submission is not inbox delivery. A message ID lets operations correlate later provider events, yet it does not prove that a person read the statement. Spam filtering, mailbox policy, suppression, and delayed delivery remain downstream. For sensitive statements, the email can carry a notice and a controlled retrieval link instead of the attachment, depending on the organization's threat model and retention rules.

Three numbers are worth putting on an operational dashboard: statements expected for the period, unique statement keys delivered, and rows stuck in each intermediate state. The counts should reconcile. Fast is secondary.

No dashboard repairs missing evidence.

## Rejected option: generate directly inside the scheduler

Rendering every customer's live data and sending it from one scheduled process looks simpler. It is also the wrong default for this workload: the process can exceed its execution window, retries restart a large batch, and live data can change between the first and second attempt. Delivery evidence becomes a log-search exercise rather than a row-level fact.

The rejected design does have a valid use case. For a tiny internal report with one recipient, no external sharing, no signature requirement, and data that can be regenerated without consequence, a direct scheduled task may be proportionate. Once the file represents a closed customer period, the stronger boundary earns its keep.

The final acceptance test is plain: run the same period twice and observe one logical statement, one immutable snapshot digest, one final artifact digest, and one recorded message ID. Then interrupt the worker after each boundary and repeat. If the ledger converges without duplicate delivery, the architecture is doing its job.

## References

- ISO, “ISO 32000-2 — Portable Document Format”: https://www.iso.org/standard/75839.html
- DocRaptor documentation: https://docraptor.com/documentation
- PDFMonkey documentation: https://docs.pdfmonkey.io/
- Gotenberg documentation: https://gotenberg.dev/docs/getting-started/introduction
- Adobe Acrobat Sign developer documentation: https://developer.adobe.com/document-services/docs/overview/pdf-services-api/
- Amazon SES developer guide: https://docs.aws.amazon.com/ses/latest/dg/Welcome.html
- OWASP, “Cryptographic Storage Cheat Sheet”: https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html
