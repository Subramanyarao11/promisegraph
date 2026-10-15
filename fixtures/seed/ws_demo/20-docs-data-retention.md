---
title: Data retention policies
capabilities: cap_data_retention, cap_audit_log
observed_at: 2026-09-10T00:00:00Z
---
# Data retention policies

Synthetic product documentation (fictional), product version 4.2.

- Raw events are retained for 13 months by default, then purged by a scheduled purge job.
- Aggregated metrics are retained for 5 years.
- Audit log entries are retained for 365 days; the UI shows the most recent 90 days and the full year is
  available through the audit log CSV download.
- Deleted workspaces are purged within 30 days; support can confirm completion from the purge log.

Admins can shorten retention per dataset. Extending a scheduled purge date requires an approved exception;
support cannot change purge dates on its own.
