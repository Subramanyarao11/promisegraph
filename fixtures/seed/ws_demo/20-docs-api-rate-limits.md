---
title: Public API rate limits
capabilities: cap_api_rate_limits
observed_at: 2026-10-11T00:00:00Z
---
# Public API rate limits

Synthetic product documentation (fictional), product version 4.2.

| Plan | Requests per minute | Burst |
| --- | --- | --- |
| Growth | 300 | 50 |
| Enterprise | 1200 | 200 |

Throttled requests receive HTTP 429 with a `Retry-After` header (seconds) and `X-RateLimit-Remaining`.
Clients should wait at least `Retry-After` seconds and use exponential backoff with jitter.

For backfills, use the bulk metrics endpoint (`/v2/metrics:batch`), which counts as one request per 100 series.
Quota increases above the Enterprise limit require an account-team review and are not granted by support.
