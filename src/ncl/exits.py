"""Exit codes, and the response each one asks of the caller.

The caller's correct reaction differs per refusal, so refusals do not share a
code. The response table is part of the command-line contract and is emitted
by ``ncl doctor --json``.
"""

from __future__ import annotations

OK = 0
ERROR = 1
USAGE = 2

PRECONDITION_FAILED = 10
NOT_CONFIGURED = 11
NO_CREDENTIAL = 12
CREDENTIAL_REJECTED = 13
CONSENT_REQUIRED = 14

UNREACHABLE = 20
SERVER_ERROR = 21
MALFORMED_RESPONSE = 22

AMBIGUOUS_TARGET = 30
TARGET_NOT_FOUND = 31
SCOPE_DENIED = 32
CONFLICT = 33
UNSUPPORTED_STRUCTURE = 34

CONFIRMATION_REQUIRED = 40
PLAN_STALE = 41
OUTCOME_UNCERTAIN = 42

LOCKED = 50
THROTTLED = 51

# What a caller should do about each code. Keeping this as the one table makes
# the guide and machine-readable doctor output describe the same contract.
RESPONSE = {
    OK: "Proceed.",
    ERROR: "Unexpected failure. Read stderr; do not blindly retry.",
    USAGE: "The command was malformed. Fix the arguments.",
    PRECONDITION_FAILED: (
        "The environment is not usable. Run `ncl doctor` and resolve it; "
        "retrying will not help."
    ),
    NOT_CONFIGURED: (
        "No matching ncl profile is configured. Create or select a profile "
        "before retrying."
    ),
    NO_CREDENTIAL: (
        "The selected profile has no stored credential. Run `ncl login`."
    ),
    CREDENTIAL_REJECTED: (
        "The server rejected the stored credential. Re-authenticate with `ncl login`."
    ),
    CONSENT_REQUIRED: "Browser consent is required. Run `ncl login`.",
    UNREACHABLE: (
        "The configured origin did not answer. Check the route and origin before retrying."
    ),
    SERVER_ERROR: (
        "The server answered, but not usefully. Read the response and do not blindly retry."
    ),
    MALFORMED_RESPONSE: (
        "The server response could not be parsed. Read the actual response before retrying."
    ),
    AMBIGUOUS_TARGET: (
        "More than one resource matched. Narrow the target before retrying."
    ),
    TARGET_NOT_FOUND: "The requested resource was not found. Re-observe or check its name.",
    SCOPE_DENIED: (
        "The requested resource is outside the configured allowlist. Do not widen it implicitly."
    ),
    CONFLICT: (
        "The resource changed under us and its ETag no longer matches. "
        "Read it again before retrying."
    ),
    UNSUPPORTED_STRUCTURE: (
        "The resource contains structure this tool does not model. Do not rewrite it."
    ),
    CONFIRMATION_REQUIRED: (
        "A plan exists but was not applied. Confirm it explicitly before retrying."
    ),
    PLAN_STALE: "The plan no longer describes reality. Re-read and make a new plan.",
    OUTCOME_UNCERTAIN: (
        "Reconcile by reading the resource back. A blind retry can duplicate or overwrite."
    ),
    LOCKED: "Another process holds the mutation or refresh lock. Wait, then retry.",
    THROTTLED: "The server is rate-limiting. Back off before retrying.",
}
