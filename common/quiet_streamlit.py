"""Silence Streamlit's bare-mode warnings for scripts that import app/queries.py.

app/queries.py decorates its functions with @st.cache_data so they cache inside
Streamlit in Snowflake. Imported from a plain script there is no Streamlit
runtime, and Streamlit logs a warning per call:

    No runtime found, using MemoryCacheStorageManager
    Thread 'MainThread': missing ScriptRunContext!

Both are harmless - the functions still execute - but they bury the script's
own output. Setting the level on the parent "streamlit" logger is not enough:
Streamlit's logger factory sets a level on each module logger explicitly, and
an explicit level on a child beats propagation from its parent. So every
already-created streamlit logger has to be set.

Timing matters too: the cache warnings fire when @st.cache_data DECORATES a
function, which happens while app/queries.py is being imported - so silencing
after that import is already too late. `silence()` therefore imports streamlit
itself, which creates the loggers, and must be called BEFORE importing any
module that uses the decorator.
"""

from __future__ import annotations

import logging

NOISY_PREFIX = "streamlit"


def silence(level: int = logging.ERROR) -> int:
    """Raise the level on every streamlit logger. Returns how many were set.

    Call before importing anything that uses @st.cache_data.
    """
    try:
        import streamlit  # noqa: F401  - creates the logger tree
    except ImportError:
        return 0
    logging.getLogger(NOISY_PREFIX).setLevel(level)
    touched = 1
    # loggerDict is keyed by logger name; copy it because importing streamlit
    # lazily can add entries while iterating.
    for name in list(logging.Logger.manager.loggerDict):
        if name == NOISY_PREFIX or name.startswith(NOISY_PREFIX + "."):
            logging.getLogger(name).setLevel(level)
            touched += 1
    return touched
