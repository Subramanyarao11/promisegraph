---
title: SSO and SCIM provisioning
capabilities: cap_sso_saml, cap_scim_user_provisioning, cap_scim_group_sync
observed_at: 2026-09-20T00:00:00Z
---
# SSO and SCIM provisioning

Synthetic product documentation (fictional), product version 4.2.

"Provisioning" can mean two different things. Confirm which one the customer needs before configuring anything.

## Account creation (SCIM user provisioning)

SCIM user provisioning creates, updates and deactivates individual user accounts from the identity provider.
Use it when new hires should get an account automatically and leavers should lose access.
Enable it under **Admin > Identity > SCIM**, generate a SCIM token, and configure the IdP's SCIM 2.0 app with
the `/scim/v2/Users` endpoint. Deactivating a user in the IdP revokes their sessions and API tokens within 5 minutes
(SCIM connector v2 and later).

Just-in-time (JIT) creation on SAML login is an alternative for small teams; it does not deactivate leavers.

## Group sync (SCIM group push and role mapping)

SCIM group sync pushes IdP groups and their membership, then maps each group to a workspace role
(Viewer, Editor, Admin). Use it when access should follow group membership. Configure group-to-role mappings
under **Admin > Identity > Group mapping**. Nested groups are not expanded in version 4.2; only direct members
of a pushed group receive its role.

## SAML single sign-on

SSO controls how users authenticate; it does not create accounts by itself unless JIT is enabled.
Upload the IdP metadata URL under **Admin > Identity > SAML**. Signing certificates can be rotated without downtime.
