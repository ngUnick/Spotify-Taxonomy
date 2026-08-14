"""Make the src-layout package importable without installing it."""

from pathlib import Path
import socket
import sys

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _reject_network(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise AssertionError("external network access is prohibited in offline tests")


# All test modules import this bootstrap.  Blocking both low-level entry points
# turns the offline requirement into an executable invariant for the full suite.
socket.socket.connect = _reject_network  # type: ignore[method-assign]
socket.create_connection = _reject_network
