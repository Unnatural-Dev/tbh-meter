"""single_instance.py — guarantees that ONLY ONE reader runs at a time.

Why: if two readers attach to the same game, both write the raw/<id>.json
records — the same run comes out duplicated, and the contention over memory knocks out the LIVE gold
read, falling back to SAVE, which under contention yields 2× gold. Pinned against real data:
one instance (live) = correct gold, no duplicate; two = duplicate + 2×.

How (Windows): a NAMED MUTEX (kernel32.CreateMutexW). The OS releases the mutex
AUTOMATICALLY when the owning process dies — including on crash or kill — so
there is NO stale-lock to clean up (unlike a PID-file). A second instance
gets ERROR_ALREADY_EXISTS and gives up. The `Local\\` namespace (session scope) is always
allowed for a non-elevated user, and is where the app + a manual run collide. (`Global\\`
would require SeCreateGlobalPrivilege, which the non-elevated app doesn't have.)

Dev-only exception: `acquire(tag)` (CLI `--instance-tag NAME`) appends `-NAME` to the mutex name so
a second reader can run beside the first for a live A/B. The CLI forces a separate --output for it
(never share a raw/ folder) and warns about the gold contention above. No tag = the name above,
unchanged — the app relies on that.

Off Windows it's a no-op: the reader only attaches to the game on Windows, so there's nothing to
protect in dev/CI (--selftest doesn't even reach here).
"""
import re
import sys

# Keep the mutex handle alive for the lifetime of the process. If it gets GC'd,
# the handle closes and the mutex is released — we'd lose the lock. That's why we stash it here.
_held = []

MUTEX_NAME = "Local\\TBH_Meter_Reader"
_ERROR_ALREADY_EXISTS = 183


_TAG_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def validate_tag(tag):
    """Return `tag` if it is a safe mutex suffix (1-32 of A-Z a-z 0-9 _ -); else ValueError.
    No backslash/space/dot: the tag becomes part of a kernel object name."""
    if not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
        raise ValueError(f"invalid instance tag {tag!r}: use 1-32 of A-Z a-z 0-9 _ -")
    return tag


def mutex_name(tag=None):
    """Mutex name for this reader. No tag = the historical name (the app relies on it)."""
    return MUTEX_NAME if tag is None else f"{MUTEX_NAME}-{validate_tag(tag)}"


def acquire(tag=None):
    """Try to become the only reader running. True if it succeeded (or if there's nothing to
    protect off Windows); False if another instance is already running. `tag` (dev A/B only)
    scopes the lock so a second, tagged reader can coexist with the untagged one."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    handle = kernel32.CreateMutexW(None, False, mutex_name(tag))
    err = ctypes.get_last_error()
    if not handle:
        # Even creating the mutex failed — fail OPEN: never block the only reader.
        return True
    if err == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _held.append(handle)  # owner: hold the handle; the OS releases it at process end
    return True
