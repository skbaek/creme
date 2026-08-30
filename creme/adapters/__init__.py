"""The only OS selection boundary used by shared Creme code."""

from __future__ import annotations

import platform
from typing import Optional

from .base import Adapter, CapabilityResult
from .darwin import DarwinAdapter
from .linux import LinuxAdapter


def get_adapter(system: Optional[str] = None) -> Adapter:
    selected = system or platform.system()
    if selected == "Darwin":
        return DarwinAdapter()
    if selected == "Linux":
        return LinuxAdapter()
    return Adapter.unsupported(selected)


__all__ = ["Adapter", "CapabilityResult", "get_adapter"]
