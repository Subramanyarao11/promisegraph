---
title: Troubleshooting scheduled exports
capabilities: cap_scheduled_exports, cap_dashboard_sharing
observed_at: 2026-10-01T00:00:00Z
---
# Troubleshooting scheduled exports

Synthetic product documentation (fictional), product version 4.2.

## A run shows `Skipped` on a daylight-saving transition day

Schedules defined in a local timezone can skip one run on the day clocks change.
Workaround: recreate the schedule with the timezone set to UTC and adjust the delivery hour.
This workaround does not apply to schedules that use a custom fiscal calendar. A permanent fix is tracked by engineering.

## A run shows `Failed` with `delivery_timeout`

Retry the run once with **Run now**. If it fails again, contact support with the schedule ID; do not
repeatedly recreate the schedule, which resets its history.

## Temporary access while a schedule is failing

As a temporary workaround, a workspace admin can create a share link to the dashboard so recipients can
download the data manually. Public (no-login) share links are only available when the organisation
has not enabled **Require authentication for shared links**.

## Exports missing from Export history

If no runs appear after a schedule's next run time, the export worker may not have picked up the job.
Support can check worker queue health. Customers cannot fix this from the UI.
