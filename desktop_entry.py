"""Standalone entry point used by the PyInstaller desktop build.

PyInstaller executes this file as the top-level ``__main__`` script, so it
must not use relative imports. The bootstrap dispatches Velopack hooks before
importing the GUI; ``python -m qq_chat_analyzer.gui`` uses the same bootstrap.
"""

from __future__ import annotations

import sys
import multiprocessing

# PyInstaller must dispatch our private SQLite children before desktop startup.
if __name__ == "__main__":
    multiprocessing.freeze_support()

from qq_chat_analyzer.desktop import main


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]

