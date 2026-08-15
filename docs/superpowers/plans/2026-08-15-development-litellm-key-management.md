# Development LiteLLM key-management implementation plan

Date: 2026-08-15

1. Extend secret-generation contracts with
   `litellm-database-password`, an exact prior-generation upgrade operation,
   project publication, validation, and fresh-install documentation.
2. Extend development secret projections with distinct database-initializer
   and LiteLLM runtime views, generated database URLs, exact ownership, and
   consumer-boundary tests.
3. Add the fail-closed idempotent PostgreSQL role/database initializer and unit
   tests before wiring it into Compose.
4. Wire `dev-litellm-database-init` into both development Compose templates,
   renderers, dependency ordering, internal networking, volumes, and complete
   disposable-stack acceptance.
5. Materialize LiteLLM's database URL marker from its projection and retain the
   existing no-secret-environment and read-only filesystem contracts.
6. Update the NAS, fresh-install, workload, MIA/Pi, and architecture guides with
   the 18-file bundle, upgrade sequence, restricted-key issuance, validation,
   and revocation procedure.
7. Run focused tests, complete affected suites, disposable Compose smoke,
   secret scans, and diff checks; request review, merge, and wait for accepted
   development images.
8. Upgrade the local secret source, republish the NAS project, pull/redeploy
   without deleting volumes, issue a restricted Pi key, and prove allowed and
   denied routes through the deployed gateway.
