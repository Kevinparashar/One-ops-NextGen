"""PolicyEngine — the embedded data-driven policy evaluator (P10, ADR-0003).

Policy is structured data (`registries/v2/policy_rules.json`). The engine
loads it, and `evaluate` answers a `PolicyQuery` deterministically: the
highest-priority matching rule wins; no match means ALLOW (open by default —
a rule must exist to restrict).

**Hot-reload, no redeploy.** `reload()` re-reads the policy file and bumps the
version. A policy change is a data deploy + a reload signal — no code change,
no process restart. The brief's requirement, and the ADR-0003 promise.

Every decision emits an OTel event naming the matched rule and effect — the
audit trail for "why did the assistant refuse / use a canned response?".
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from oneops.errors import ConfigError
from oneops.observability import get_logger, get_tracer
from oneops.policy_engine.models import (
    PolicyDecision,
    PolicyEffect,
    PolicyMatch,
    PolicyQuery,
    PolicyRule,
)

_log = get_logger("oneops.policy_engine")
_tracer = get_tracer("oneops.policy_engine")

_DEFAULT_POLICY_FILE = "registries/v2/policy_rules.json"


def _parse_rules(doc: dict) -> list[PolicyRule]:
    rules: list[PolicyRule] = []
    for raw in doc.get("rules", []):
        m = raw.get("match", {})
        rules.append(PolicyRule(
            id=raw["id"],
            description=raw.get("description", ""),
            match=PolicyMatch(
                roles=tuple(m.get("roles", [])),
                actions=tuple(m.get("actions", [])),
                data_classifications=tuple(m.get("data_classifications", [])),
                surfaces=tuple(m.get("surfaces", [])),
                intents=tuple(m.get("intents", [])),
            ),
            effect=PolicyEffect(raw["effect"]),
            reason=raw.get("reason", ""),
            canned_response=raw.get("canned_response", ""),
            priority=int(raw.get("priority", 0)),
        ))
    return rules


class PolicyEngine:
    """Evaluates policy queries against a hot-reloadable ruleset."""

    def __init__(self, rules: list[PolicyRule], *, version: str = "0") -> None:
        self._lock = threading.RLock()
        # Highest priority first — `evaluate` takes the first match.
        self._rules = sorted(rules, key=lambda r: -r.priority)
        self._version = version

    @classmethod
    def from_file(cls, path: str | None = None) -> PolicyEngine:
        engine = cls([])
        engine._load(path)
        return engine

    def _resolve_path(self, path: str | None) -> Path:
        if path is None:
            path = _DEFAULT_POLICY_FILE
        p = Path(path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[3] / path
        return p

    def _load(self, path: str | None) -> None:
        p = self._resolve_path(path)
        if not p.is_file():
            raise ConfigError(f"policy file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"policy file unreadable: {p}", cause=exc) from exc
        with self._lock:
            self._rules = sorted(_parse_rules(doc), key=lambda r: -r.priority)
            self._version = str(doc.get("version", "0"))
        self._path = p
        _log.info("policy.loaded", source=str(p), version=self._version,
                  rule_count=len(self._rules))

    def reload(self) -> str:
        """Re-read the policy file — hot-reload, no redeploy. Returns the new
        policy version. This is the whole point of the data-driven design: a
        policy change deploys as data + a reload, never as code."""
        self._load(getattr(self, "_path", None))
        return self._version

    @property
    def version(self) -> str:
        return self._version

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def evaluate(self, query: PolicyQuery) -> PolicyDecision:
        """Return the verdict for `query`. Highest-priority matching rule wins;
        no match → ALLOW (open by default). Every decision is traced."""
        with _tracer.start_as_current_span(
            "policy.evaluate",
            attributes={"oneops.tenant_id": query.tenant_id,
                        "oneops.user_id": getattr(query, "user_id", "") or "",
                        "policy.version": self._version},
        ) as span:
            with self._lock:
                rules = list(self._rules)
            for rule in rules:                       # already priority-ordered
                if rule.match.matches(query):
                    span.set_attribute("policy.matched_rule", rule.id)
                    span.set_attribute("policy.effect", rule.effect.value)
                    if rule.effect is not PolicyEffect.ALLOW:
                        _log.info("policy.decision", rule_id=rule.id,
                                  effect=rule.effect.value, tenant_id=query.tenant_id,
                                  reason=rule.reason)
                    return PolicyDecision(
                        effect=rule.effect, reason=rule.reason,
                        canned_response=rule.canned_response,
                        matched_rule_id=rule.id, policy_version=self._version)
            span.set_attribute("policy.matched_rule", "")
            span.set_attribute("policy.effect", PolicyEffect.ALLOW.value)
            return PolicyDecision(effect=PolicyEffect.ALLOW,
                                  reason="no rule restricts this",
                                  policy_version=self._version)


__all__ = ["PolicyEngine"]
