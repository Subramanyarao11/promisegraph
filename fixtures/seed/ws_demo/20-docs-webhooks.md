---
title: Webhooks
capabilities: cap_webhooks
observed_at: 2026-09-15T00:00:00Z
---
# Webhooks

Synthetic product documentation (fictional), product version 4.2.

Webhooks send an HTTP POST to your endpoint when events occur (alert fired, export delivered, user deactivated).

## Signatures

Each request carries an `X-Lumetric-Signature` header: an HMAC-SHA256 of the raw body using the endpoint's
signing secret. Compare it with a constant-time comparison and reject requests older than 5 minutes using
the `X-Lumetric-Timestamp` header.

## Retries

Failed deliveries (non-2xx or timeout after 10 seconds) are retried with exponential backoff for up to
30 minutes. Events that cannot be delivered within that window are dropped and appear in the webhook log as
`expired`. There is currently no self-serve replay.
