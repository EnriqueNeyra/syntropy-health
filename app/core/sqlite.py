"""
Which SQLite the app talks to.

SQLCipher is SQLite that encrypts every page of its file, and its Python binding has the same interface as the standard
module. Without a key it reads and writes ordinary SQLite files, which is how a database from before encryption keeps
working until it has been converted (app.core.encryption).

Its errors are its own classes, not the standard module's, so everything in the app imports ``sqlite3`` from here: an
``except sqlite3.Error`` naming the other module would catch nothing.

On a computer the binding has no build for, this falls back to the standard module. Such a server keeps working, without
encryption, and Settings says so. A database that's already encrypted refuses to open there rather than look damaged.
"""

from __future__ import annotations

try:
    from sqlcipher3 import dbapi2 as sqlite3
    HAS_ENCRYPTION = True
except ImportError:
    import sqlite3  # type: ignore[no-redef]
    HAS_ENCRYPTION = False

__all__ = ["sqlite3", "HAS_ENCRYPTION"]
