"""
This package exists to speed up reading properties of raster or vector files by caching metadata in a local LMDB database. 
This is optional and will only be used if the LMDB package is installed and the cache can be created. 
The cache is stored in the user's home directory under .cache/nencarta.lmdb by default, 
but this can be overridden by setting the NENCARTA_CACHE_DIR environment variable to a different path.
"""

import os
import pickle
import threading
from pathlib import Path
from typing import Any

_HAS_LMDB = False
_LMDB_ENV = None
try:
    import lmdb

    # Cache raster metadata in LMDB when available. This is optional because
    # locked or read-only home directories should not block package imports.
    _CACHE_DIR = Path(os.environ.get("NENCARTA_CACHE_DIR", Path.home() / ".cache"))
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    _LMDB_ENV = lmdb.open(
        str(_CACHE_DIR / "nencarta.lmdb"),
        map_size=1024**3,
        subdir=False,
        lock=True,
        sync=False,
        metasync=False,
        readahead=False,
        writemap=True,
        max_readers=512,
    )
    _HAS_LMDB = True
except (ImportError, ModuleNotFoundError, OSError, RuntimeError):
    _HAS_LMDB = False
    _LMDB_ENV = None
except Exception:  # noqa: BLE001
    _HAS_LMDB = False
    _LMDB_ENV = None

class LMDBCache:
    """Optional LMDB-backed metadata cache for raster-like objects."""

    def __init__(self):
        self.can_cache = _HAS_LMDB
        if self.can_cache:
            self._setup_cache()
        

    def _setup_cache(self) -> None:
        if not globals().get("_LMDB_ENV"):
            raise RuntimeError("LMDB is not available, cannot setup cache. You should never call this method directly.")
        
        self._CACHE_LOCK = threading.RLock()
        self._LMDB_ENV = _LMDB_ENV

    def _save_cached_metadata(
        self,
        filepath: str,
        timestamp: float,
        metadata: dict[str, Any],
    ) -> None:
        """Store metadata for a file timestamp."""

        key = self._cache_key(filepath)

        value = pickle.dumps(
            {
                "timestamp": timestamp,
                "metadata": metadata,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )

        with self._LMDB_ENV.begin(write=True) as txn:
            txn.put(key, value)

    def _load_cached_metadata(
        self,
        filepath: str,
        timestamp: float,
    ) -> dict[str, Any] | None:
        """Return cached metadata only when the file timestamp matches."""

        key = self._cache_key(filepath)

        with self._LMDB_ENV.begin(write=False) as txn:
            value = txn.get(key)

        if value is None:
            return None

        try:
            entry = pickle.loads(value)
        except Exception:  # noqa: BLE001
            return None

        if entry["timestamp"] != timestamp:
            return None

        return entry["metadata"]
    
    def _get_file_timestamp(self, filepath: str) -> float:
        return os.path.getmtime(filepath)
    
    def _cache_key(self, filepath: str) -> bytes:
        return os.path.abspath(filepath).encode()
