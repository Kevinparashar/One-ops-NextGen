"""Configuration loaded from env. Single source of truth for runtime settings.

Production-grade rules:
- All settings loaded ONCE at import time (immutable after).
- No mutable globals beyond this Settings instance.
- Secrets never logged; the __repr__ redacts API keys.
- Type-validated via pydantic; misconfiguration fails fast at startup.

Multi-turn focus migration flags (Phase 9):
  These flags live OUTSIDE the pydantic Settings instance because operators
  need to flip them per-test and per-deployment without re-importing the
  module. `Settings` is process-singleton via lru_cache; the migration
  flags are read from env on every call instead — same semantics as the
  per-call `_env_flag` reads inside graph nodes, just centralized for
  introspection / logging / tests.

  Default values preserve legacy behavior (Stage 1). See
  `docs/multi_turn_focus_migration.md` for the four-stage rollout matrix.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Identity ─────────────────────────────────────────────────────
    service_name: str = "oneops-graph"
    service_version: str = "0.1.0"
    environment: Literal["local", "dev", "staging", "prod"] = "local"

    # ── LLM Gateway ──────────────────────────────────────────────────
    llm_gateway_url: str = "http://localhost:4000"
    llm_gateway_api_key: SecretStr = SecretStr("sk-1234")
    llm_default_model: str = "gpt-4o-mini"
    llm_planner_model: str = "gpt-4o-mini"

    # Three-stage routing feature flag. Default "legacy" preserves pre-Phase-3
    # graph wiring; "three_stage" inserts shortlist + rerank nodes between
    # content_safety and planner. Gate A short-circuits off-domain queries.
    routing_mode: Literal["legacy", "three_stage"] = "legacy"
    routing_gate_a_threshold: float = 7.0      # rerank score below → CLARIFY
    routing_shortlist_top_k: int = 8
    routing_rerank_top_n: int = 5
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_replay_mode: Literal["off", "record", "replay"] = "off"

    # ── NATS ─────────────────────────────────────────────────────────
    nats_url: str = "nats://localhost:4222"
    nats_subject_request: str = "oneops.request.chat"
    nats_subject_response_prefix: str = "oneops.response"
    nats_request_timeout_seconds: float = 120.0
    nats_queue_group: str = "oneops-graph"

    # ── Dragonfly ────────────────────────────────────────────────────
    dragonfly_url: str = "redis://localhost:6379/0"
    dragonfly_pool_max: int = 50
    session_ttl_seconds: int = 3600
    cache_default_ttl_seconds: int = 300

    # ── Postgres ─────────────────────────────────────────────────────
    postgres_url: str = "postgresql://oneops:oneops@localhost:5432/oneops"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 20
    langgraph_checkpointer: Literal["postgres", "memory"] = "memory"
    langgraph_aes_key: SecretStr | None = None

    # ── OTEL ─────────────────────────────────────────────────────────
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "oneops-graph"
    otel_traces_sampler: str = "parentbased_traceidratio"
    otel_traces_sampler_arg: float = 1.0

    # ── LangSmith (optional dev tracing) ─────────────────────────────
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "oneops-dev"

    # ── Feature flags ────────────────────────────────────────────────
    bridge_invariant_strict: bool = Field(default=False)
    enable_llm_replay: bool = False
    uc_invoker_mode: Literal["local", "nats"] = "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Single source of truth. Use this everywhere instead of re-instantiating Settings."""
    return Settings()


# ── Multi-turn focus migration flags (Phase 9) ───────────────────────────


# Truthy / falsy aliases. Matches the per-call `_env_flag` helper in
# `oneops.graph.nodes` so flag semantics are byte-identical across the
# codebase. Keep these two in sync if either ever changes.
_TRUTHY = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSY = frozenset({"0", "false", "no", "off", "f", "n"})


def _parse_flag(name: str, default: bool) -> bool:
    """Parse a boolean env flag with explicit truthy/falsy aliases.

    Unknown values fall back to `default` and emit a structlog warning
    rather than raising — operators see the misparse in the audit log
    without losing a startup. Strict validation lives in tests.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    norm = raw.strip().lower()
    if norm in _TRUTHY:
        return True
    if norm in _FALSY:
        return False
    # Unknown value — log + fall back. We import the logger lazily to
    # avoid pulling structlog into module import for tests that bypass
    # observability setup.
    try:
        from oneops.observability import get_logger
        get_logger("oneops.config").warning(
            "focus_migration.flag_invalid",
            flag=name, value=raw, falling_back_to=default,
        )
    except Exception:
        pass
    return default


@dataclass(frozen=True)
class FocusMigrationConfig:
    """Snapshot of the five multi-turn-focus migration flags.

    NOT cached. `get_focus_migration_config()` re-reads env on every call
    so monkeypatched tests and live envvar updates take effect immediately.
    The runtime code (load_session_node, aggregator_node, uc_executor_node)
    continues to call `_env_flag` directly for the same semantics; this
    dataclass exists for introspection, observability, and tests.
    """

    use_graph_focus: bool
    legacy_dragonfly_focus_read: bool
    legacy_dragonfly_focus_write: bool
    compare_graph_and_dragonfly_focus: bool
    require_thread_id: bool

    @property
    def stage(self) -> str:
        """Best-effort label for the active migration stage.

        Returns:
          "stage_1_legacy"        — Dragonfly is boss; graph focus is dormant.
          "stage_2_dual_write"    — Graph primary; legacy read+write still on.
          "stage_3_graph_primary" — Graph primary; legacy read only.
          "stage_4_graph_only"    — Graph only; legacy fully disabled.
          "custom"                — Flag combination doesn't match a known stage.
        """
        s1 = (
            not self.use_graph_focus
            and self.legacy_dragonfly_focus_read
            and self.legacy_dragonfly_focus_write
            and not self.compare_graph_and_dragonfly_focus
        )
        if s1:
            return "stage_1_legacy"
        s2 = (
            self.use_graph_focus
            and self.legacy_dragonfly_focus_read
            and self.legacy_dragonfly_focus_write
            and self.compare_graph_and_dragonfly_focus
        )
        if s2:
            return "stage_2_dual_write"
        s3 = (
            self.use_graph_focus
            and self.legacy_dragonfly_focus_read
            and not self.legacy_dragonfly_focus_write
            and self.compare_graph_and_dragonfly_focus
        )
        if s3:
            return "stage_3_graph_primary"
        s4 = (
            self.use_graph_focus
            and not self.legacy_dragonfly_focus_read
            and not self.legacy_dragonfly_focus_write
            and not self.compare_graph_and_dragonfly_focus
        )
        if s4:
            return "stage_4_graph_only"
        return "custom"


def get_focus_migration_config() -> FocusMigrationConfig:
    """Read the five focus-migration flags from the current environment.

    Defaults (Stage 1):
      USE_GRAPH_FOCUS                  = false
      LEGACY_DRAGONFLY_FOCUS_READ      = true
      LEGACY_DRAGONFLY_FOCUS_WRITE     = true
      COMPARE_GRAPH_AND_DRAGONFLY_FOCUS = false
      REQUIRE_THREAD_ID                = false
    """
    return FocusMigrationConfig(
        use_graph_focus=_parse_flag("USE_GRAPH_FOCUS", default=False),
        legacy_dragonfly_focus_read=_parse_flag("LEGACY_DRAGONFLY_FOCUS_READ", default=True),
        legacy_dragonfly_focus_write=_parse_flag("LEGACY_DRAGONFLY_FOCUS_WRITE", default=True),
        compare_graph_and_dragonfly_focus=_parse_flag("COMPARE_GRAPH_AND_DRAGONFLY_FOCUS", default=False),
        require_thread_id=_parse_flag("REQUIRE_THREAD_ID", default=False),
    )


__all__ = [
    "Settings",
    "get_settings",
    "FocusMigrationConfig",
    "get_focus_migration_config",
]
