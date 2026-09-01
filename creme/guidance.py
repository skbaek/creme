from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_RELATIVE_GUIDANCE = Path(".creme/host-guidance.md")
MAX_GUIDANCE_BYTES = 64 * 1024


@dataclass(frozen=True)
class GuidanceValidation:
    status: str
    detail: str
    content: Optional[str] = None


def load(path: Path) -> GuidanceValidation:
    if not path.is_file():
        return GuidanceValidation("MISSING", f"host guidance not found: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return GuidanceValidation("INVALID", f"host guidance could not be read: {exc}")
    if len(raw) > MAX_GUIDANCE_BYTES:
        return GuidanceValidation(
            "INVALID",
            f"host guidance exceeds {MAX_GUIDANCE_BYTES} bytes: {path}",
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return GuidanceValidation("INVALID", f"host guidance is not UTF-8: {exc}")
    if "\x00" in content:
        return GuidanceValidation("INVALID", "host guidance contains a NUL byte")
    if not content.strip():
        return GuidanceValidation("INVALID", f"host guidance is empty: {path}")
    return GuidanceValidation("OK", f"local host guidance present: {path}", content)
