"""Windows-only subprocess policy for the isolated upstream registration worker."""

from __future__ import annotations

import os
import subprocess


if os.name == "nt":
    _original_popen = subprocess.Popen

    class _NoWindowPopen(_original_popen):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

