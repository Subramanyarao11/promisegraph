---
title: Scheduling a recurring export
capabilities: cap_scheduled_exports, cap_csv_export
observed_at: 2026-10-01T00:00:00Z
---
# Scheduling a recurring export

Synthetic product documentation for the Lumetric analytics platform (fictional), product version 4.2.

Scheduled exports deliver a dashboard or saved query as a file on a recurring schedule.
Supported formats are CSV, XLSX and PDF. Supported destinations are email (up to 20 recipients)
and, in beta for selected plans, an S3 bucket. SFTP delivery is not supported.

## Create a schedule

1. Open the dashboard or saved query and choose **Export > Schedule**.
2. Pick a cadence: hourly, daily, weekly (choose the weekday) or monthly (choose the day of month).
3. Set the delivery time and the timezone. Schedules run in the timezone you select, not the viewer's timezone.
4. Choose the file format (CSV, XLSX or PDF).
5. Add email recipients. Recipients outside your organisation require an admin to allow external delivery.
6. Save. The first run appears under **Export history** with status `Scheduled`.

Workspace admins and editors can create schedules. Viewers can subscribe to an existing schedule but cannot create one.

## Limits

- Up to 50 active schedules per workspace on the Growth plan and 500 on Enterprise.
- Files larger than 25 MB are delivered as a secure, authenticated download link instead of an attachment.
- Hourly schedules are limited to saved queries returning fewer than 1M rows.

## Checking delivery

**Export history** lists each run with status `Delivered`, `Failed` or `Skipped`. Selecting **Run now** queues
an immediate run with the same settings; it does not change the schedule.
