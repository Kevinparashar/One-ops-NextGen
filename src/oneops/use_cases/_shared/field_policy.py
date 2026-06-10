"""Field-exposure policy — schema-driven redaction for UC entity handlers.

A handler that returns an entity record must not carry a hardcoded list of
fields to strip — that is a static catalogue (Component Spec C12). Instead each
field carries a *data classification*, declared as registry data in
`registries/v2/platform/field_policy.json`, and a handler exposes a field only when its
classification ranks below the withhold threshold.

The *principle* lives here in code ("expose what ranks below the threshold");
the *per-field data* lives in the registry. Marking a new field sensitive is a
one-line registry edit, never a code change (Component Spec C1).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oneops.errors import ConfigError

_DEFAULT_PATH = "registries/v2/platform/field_policy.json"

# Sensitivity ladder, low → high. A field is exposable when its rank is
# strictly below the configured withhold rank.
_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


class FieldPolicy:
    """Decides which fields of an entity record may be returned to the model."""

    def __init__(
        self,
        *,
        default_classification: str,
        withhold_at_or_above: str,
        classifications: dict[str, str],
        internal_content: dict[str, Any] | None = None,
        kb_audience: dict[str, Any] | None = None,
    ) -> None:
        for name, value in (("default_classification", default_classification),
                             ("withhold_at_or_above", withhold_at_or_above)):
            if value not in _RANK:
                raise ConfigError(
                    f"field_policy {name}={value!r} is not a known "
                    f"classification (expected one of: {', '.join(_RANK)})")
        bad = {f: c for f, c in classifications.items() if c not in _RANK}
        if bad:
            raise ConfigError(f"field_policy: unknown classification(s) {bad}")
        self._default = default_classification
        self._withhold = _RANK[withhold_at_or_above]
        self._classifications = dict(classifications)
        # Item-level visibility for nested record arrays (e.g. work_notes).
        ic = internal_content or {}
        self._content_arrays: tuple[str, ...] = tuple(ic.get("arrays", ()))
        self._content_flag: str = ic.get("visibility_flag", "is_public")
        self._privileged_roles: frozenset[str] = frozenset(
            ic.get("privileged_roles", ()))
        # KB article audiences a role may read.
        kb = kb_audience or {}
        self._kb_end_user_audiences: tuple[str, ...] = tuple(
            kb.get("end_user_visible", ("all", "end_user")))
        self._kb_privileged_audiences: tuple[str, ...] = tuple(
            kb.get("privileged_only", ("technician",)))

    @classmethod
    def from_registry_file(cls, path: str | None = None) -> FieldPolicy:
        if path is None:
            path = str(Path(__file__).resolve().parents[4] / _DEFAULT_PATH)
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"field_policy file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"field_policy unreadable: {p}", cause=exc) from exc
        return cls(
            default_classification=doc.get("default_classification", "internal"),
            withhold_at_or_above=doc.get("withhold_at_or_above", "confidential"),
            classifications=doc.get("classifications", {}),
            internal_content=doc.get("internal_content"),
            kb_audience=doc.get("kb_audience"))

    def classification_of(self, field: str) -> str:
        return self._classifications.get(field, self._default)

    def is_exposable(self, field: str) -> bool:
        return _RANK[self.classification_of(field)] < self._withhold

    def expose(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return only the fields of `record` whose classification ranks below
        the withhold threshold."""
        return {k: v for k, v in record.items() if self.is_exposable(k)}

    def kb_audiences_for(self, role: str) -> tuple[str, ...]:
        """KB article audiences a role may read. Every role sees the
        `end_user_visible` audiences; a privileged (staff) role additionally
        sees the `privileged_only` audiences. Default-deny: an end-user or
        unrecognised role never sees a privileged-only audience."""
        if role in self._privileged_roles:
            return self._kb_end_user_audiences + self._kb_privileged_audiences
        return self._kb_end_user_audiences

    def redact_internal_content(
        self, record: dict[str, Any], role: str
    ) -> dict[str, Any]:
        """Drop internal items from nested arrays (e.g. private `work_notes`)
        for a non-privileged caller. Default-deny: a role sees internal items
        only when it is in `privileged_roles`; an end-user or unrecognised role
        sees only items whose visibility flag is explicitly true."""
        if role in self._privileged_roles:
            return record
        out = dict(record)
        for arr in self._content_arrays:
            items = out.get(arr)
            if isinstance(items, list):
                out[arr] = [
                    it for it in items
                    if isinstance(it, dict) and it.get(self._content_flag) is True
                ]
        return out


_policy: FieldPolicy | None = None


def get_field_policy() -> FieldPolicy:
    """The process-wide field policy, loaded from the registry on first use."""
    global _policy
    if _policy is None:
        _policy = FieldPolicy.from_registry_file()
    return _policy


def set_field_policy(policy: FieldPolicy) -> None:
    """Replace the process-wide policy — used by tests."""
    global _policy
    _policy = policy


__all__ = ["FieldPolicy", "get_field_policy", "set_field_policy"]
