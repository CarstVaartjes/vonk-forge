"""Domains of identities the Controller owns in relational integer columns.

These limits come from the production PostgreSQL column types, not JSON,
JavaScript, or a generated client's preferred representation. Apply them only
to values with that relational owner; resource estimates and aggregate JSON
counters retain their own domains.
"""

MAX_DATABASE_INTEGER = 2**31 - 1
MAX_DATABASE_BIGINT = 2**63 - 1
