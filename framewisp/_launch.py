"""Capture the caller environment before executing the installed Nix wrapper.

Run by filename with isolated Python. Only the standard library is available.
"""

import os
import sys
from pathlib import Path


def main() -> None:
    os.environ.pop("FRAMEWISP_CALLER_ENV_FD", None)
    descriptor = os.memfd_create("framewisp-caller-environment")
    with os.fdopen(descriptor, "wb", closefd=False) as snapshot:
        # procfs retains the exec environment, before Python's locale coercion.
        snapshot.write(Path("/proc/self/environ").read_bytes())
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.set_inheritable(descriptor, True)
    os.environ["FRAMEWISP_CALLER_ENV_FD"] = str(descriptor)
    os.execv(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
