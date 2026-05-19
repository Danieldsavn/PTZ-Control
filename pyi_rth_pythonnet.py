# PyInstaller runtime hook — load .NET/cffi before pywebview probes alternate GUI backends.
import os
import sys

if sys.platform == "win32":
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
    os.environ.setdefault("PYWEBVIEW_GUI", "edgechromium")
    try:
        import _cffi_backend  # noqa: F401
    except ImportError:
        pass
