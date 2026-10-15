---
title: Warehouse sync
capabilities: cap_warehouse_sync
observed_at: 2026-10-01T00:00:00Z
---
# Warehouse sync (beta)

Synthetic product documentation (fictional), product version 4.3 beta.

Warehouse sync copies modelled tables to your data warehouse every 15 minutes. New columns appear after
the next full refresh of the model, which can be triggered under **Models > Sync > Refresh schema**.
Schema changes on large models currently trigger a full backfill, which can delay incremental syncs.
