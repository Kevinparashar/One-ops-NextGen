"""Handler resolution — a registry tool record's `handler_ref` → a callable.

A `ToolRecord` carries `handler_ref` as `"module.path:function"`. The resolver
turns that into the actual coroutine. Two resolution paths:

  * **explicit registry** — `register(handler_ref, fn)`. In-process / FaaS
    handlers, and the path tests use. Checked first.
  * **import resolution** — `import module; getattr(fn)`. The default for
    handlers that live as ordinary modules.

A handler that cannot be resolved raises `ToolHandlerError` — loud, never a
silent skip. Resolved callables are cached.
"""
from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.errors import ToolHandlerError

# A tool handler is an async callable: (arguments, context) -> result value.
ToolHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]


class HandlerResolver:
    """Resolves `handler_ref` strings to tool handler callables."""

    def __init__(self) -> None:
        self._explicit: dict[str, ToolHandler] = {}
        self._import_cache: dict[str, ToolHandler] = {}

    def register(self, handler_ref: str, handler: ToolHandler) -> None:
        """Register an in-process handler under its ref. Wins over import
        resolution — used by FaaS wiring and by tests."""
        if handler_ref in self._explicit:
            raise ValueError(f"handler '{handler_ref}' is already registered")
        self._explicit[handler_ref] = handler

    def resolve(self, handler_ref: str) -> ToolHandler:
        """Return the callable for `handler_ref`. Explicit registry first,
        then `module:function` import resolution. Raises `ToolHandlerError`
        if neither yields a callable."""
        if handler_ref in self._explicit:
            return self._explicit[handler_ref]
        if handler_ref in self._import_cache:
            return self._import_cache[handler_ref]

        if ":" not in handler_ref:
            raise ToolHandlerError(
                f"handler_ref '{handler_ref}' is not registered and is not in "
                "'module:function' form — cannot resolve")
        module_path, _, func_name = handler_ref.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise ToolHandlerError(
                f"handler_ref '{handler_ref}': module '{module_path}' "
                f"could not be imported", cause=exc) from exc
        handler = getattr(module, func_name, None)
        if handler is None:
            raise ToolHandlerError(
                f"handler_ref '{handler_ref}': module '{module_path}' has no "
                f"attribute '{func_name}'")
        if not callable(handler):
            raise ToolHandlerError(
                f"handler_ref '{handler_ref}': '{func_name}' is not callable")
        self._import_cache[handler_ref] = handler
        return handler

    @property
    def registered(self) -> frozenset[str]:
        return frozenset(self._explicit)


__all__ = ["ToolHandler", "HandlerResolver"]
