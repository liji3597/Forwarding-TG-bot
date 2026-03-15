from __future__ import annotations

import os
import sys


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def main() -> int:
    try:
        import tg_forwarder  # noqa: F401
    except Exception:
        return 1
    return 0 if _pid_exists(1) else 1


if __name__ == "__main__":
    sys.exit(main())
