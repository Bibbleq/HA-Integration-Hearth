"""Let the core tests import `custom_components.hearth.core` without Home Assistant.

When HA is not installed (local runs), register lightweight package shells so
importing the core subpackage never executes `custom_components/hearth/__init__.py`.
In CI, HA is present and the normal package import path is used.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[2]

if importlib.util.find_spec("homeassistant") is None:
    if "custom_components" not in sys.modules:
        pkg = types.ModuleType("custom_components")
        pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = pkg
    if "custom_components.hearth" not in sys.modules:
        hearth = types.ModuleType("custom_components.hearth")
        hearth.__path__ = [str(ROOT / "custom_components" / "hearth")]
        sys.modules["custom_components.hearth"] = hearth
