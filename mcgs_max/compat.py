"""Portable dependency loading for mcgs-max.

Third-party packages live in ``vendor/`` next to this package so the project can
be copied to another machine without installing anything globally. If a package
is missing, the error message says exactly what to run.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
VENDOR = PROJECT_ROOT / "vendor"

_REQUIRED = {
    "access_parser": "access-parser",
}


def _ensure_vendor_on_path() -> None:
    if VENDOR.is_dir():
        entry = str(VENDOR)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def require(module: str):
    """Import `module`, bootstrapping it from vendor/ when present."""
    _ensure_vendor_on_path()
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError:
        pip_name = _REQUIRED.get(module, module)
        raise ModuleNotFoundError(
            f"{module!r} is required by mcgs-max but was not found.\n"
            f"Install it into the bundled folder with:\n"
            f'  python -m pip install --target "{VENDOR}" {pip_name}'
        ) from None


def check_environment() -> dict:
    """Report runtime facts without importing anything heavy."""
    import platform

    info = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "vendor_dir": str(VENDOR),
        "vendor_present": VENDOR.is_dir(),
        "missing": [],
    }
    for module in _REQUIRED:
        _ensure_vendor_on_path()
        try:
            importlib.import_module(module)
            info.setdefault("available", []).append(module)
        except ModuleNotFoundError:
            info["missing"].append(module)
    return info
