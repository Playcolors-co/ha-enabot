"""
ebo_log.py — clean logging for the add-on.

The Agora SDK is very noisy: it prints "on publish result: ..." for every RTM publish
from its native (C) layer straight to stdout, and logs setup lines via Python logging.
This module, imported FIRST (before agora), silences all of that while keeping our own
log lines readable in the Home Assistant add-on log.

How: duplicate the real stdout, redirect fd 1 to /dev/null (drops native + print noise),
mute library logging. Our log() writes to the saved real stdout via os.write.

Verbosity: set EBO_LOG_LEVEL to debug | info (default) | warning. log(...) is INFO by
default; pass level="debug" for chatty lines (e.g. per-N-frames) or level="warning"/"error"
for problems.
"""
import logging
import os
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

# mute third-party logging (agora uses logger.error for setup chatter)
logging.disable(logging.ERROR)

# keep a handle to the real stdout, then send fd 1 to /dev/null
try:
    _real = os.dup(1)
    _null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null, 1)
except Exception:
    _real = 1

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_threshold = _LEVELS.get(os.environ.get("EBO_LOG_LEVEL", "info").lower(), 20)


def log(*a, **kw):
    level = kw.get("level", "info")
    if _LEVELS.get(level, 20) < _threshold:
        return
    try:
        line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n"
        os.write(_real, line.encode("utf-8", "replace"))
    except Exception:
        pass
