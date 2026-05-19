# PyInstaller runtime hook — select .NET Framework before pythonnet/clr loads (pywebview).
import os
import sys

if sys.platform == "win32":
    os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
