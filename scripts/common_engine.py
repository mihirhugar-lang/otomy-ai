"""Single financial calculation engine used by Otomy and localhost.

The ERP fetcher and the two web applications are adapters. They must pass the
same normalized rows to this module; they must not reimplement balance,
cashbook, or daily-ledger calculations.

The implementation is loaded from ``gha_sync.py`` for now because that is the
cloud-side authoritative engine. Keeping this small facade gives both
consumers one import contract while the fetch/publish code remains separate.
"""

from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path


ENGINE_NAME = "loctell-common-engine"
ENGINE_VERSION = "2026-08-02.1"
_ENGINE_PATH = Path(__file__).with_name("gha_sync.py")


@lru_cache(maxsize=1)
def _authoritative_module():
    """Load the cloud implementation without running its ``main`` function."""
    module_name = "otomy_common_engine_authoritative"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(module_name, _ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load common engine: {_ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def build_ledger_view(*args, **kwargs):
    return _authoritative_module().build_ledger_view(*args, **kwargs)


def build_cashbook_view(*args, **kwargs):
    return _authoritative_module().build_cashbook_view(*args, **kwargs)


def balance_overlay():
    return _authoritative_module()._balance_overlay()


def engine_metadata() -> dict:
    return {"name": ENGINE_NAME, "version": ENGINE_VERSION}
