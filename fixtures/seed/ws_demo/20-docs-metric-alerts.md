---
title: Metric alerts
capabilities: cap_metric_alerts, cap_webhooks
observed_at: 2026-10-08T00:00:00Z
---
# Metric alerts

Synthetic product documentation (fictional), product version 4.2.

Alerts evaluate a metric every 5 minutes. Two types exist:

- **Threshold alerts** fire when a metric crosses a fixed value.
- **Anomaly alerts** fire when a metric deviates from its learned baseline. Each anomaly alert has a
  sensitivity setting (low, medium, high); lower sensitivity produces fewer alerts.

Notifications go to email recipients or to a webhook endpoint. Chat-tool integrations and daily digests are
not available; each alert produces one notification.
