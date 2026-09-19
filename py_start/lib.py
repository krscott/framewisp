import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class Options:
    name: str


def greet(opts: Options) -> None:
    """Print a greeting message.

    Args:
        name: The name of the person to greet.
    """
    log.debug("Greeting user...")
    print(f"Hello, {opts.name}!")
