"""Integration adapters for UC-8 Fulfillment.

Production-grade two-layer design:

  protocol.py     — `IntegrationAdapter` Protocol + typed result envelopes.
                    The CONTRACT. Every integration (mock + real) must honor
                    this exact signature.

  mock.py         — Deterministic mock implementing the Protocol. Used by
                    tests and the demo. Behaviour is reproducible and tests
                    can assert against it.

  (future)
  ad_real.py      — Real Active Directory binding (Microsoft Graph or LDAP).
                    Same Protocol — handler code does not change.
  okta_real.py    — Real Okta SSO + MFA binding.
  procurement_real.py — Real procurement (ServiceNow Procurement / Coupa).
  ...

Swapping mock for real is a binding change in `runner.py`, NOT a handler
edit. This is the production-grade seam that the manager decision package
calls out: mocks for the demo, the same Protocol gets real implementations
in week 2 without rewriting UC-8.
"""

from oneops.use_cases.uc08_fulfillment.adapters.inprocess import (
    FailurePolicy,
    InProcessIntegrationAdapter,
)
from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    AccountResult,
    AdapterResponse,
    GenericTaskResult,
    GroupMembershipResult,
    IntegrationAdapter,
    LicenseResult,
    MailboxResult,
    ProcurementOrderResult,
    VpnGrantResult,
)

__all__ = [
    "IntegrationAdapter",
    "AdapterResponse",
    "AccountResult",
    "MailboxResult",
    "VpnGrantResult",
    "GroupMembershipResult",
    "LicenseResult",
    "ProcurementOrderResult",
    "GenericTaskResult",
    "InProcessIntegrationAdapter",
    "FailurePolicy",
]
