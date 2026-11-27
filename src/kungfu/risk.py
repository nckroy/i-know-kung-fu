# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
"""The experimental-software warning, and the acknowledgement every sensitive run needs.

`init`, `export` and `import` show the warning and run only after a positive acknowledgement for
that run: the typed phrase, or --accept-risk. Nothing is remembered between runs.

The acknowledgement is also an object that code writing into someone's vault or wiki must hold:
the importers' ``load()`` refuses to write without one, so no path that imports into a user's
data (a command, a script, another tool built on kungfu) can skip the warning. Only
``acknowledge()`` makes one. This guards against accidental writes; it isn't a security boundary
against code that sets out to bypass it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

ACCEPT_PHRASE = "I accept the risk"

WARNING = """\
  ⚠  I Know Kung-Fu is EXPERIMENTAL software. Using it can be risky. Use it at your own risk.

  {what}

  Its scrubbing and checks are automated, and they can miss things or get them wrong: names,
  secrets, ID numbers or sensitive context can end up in a bundle, and an import can add wrong
  or harmful advice to your notes. Nothing it produces has been reviewed by a person until you
  review it. Check everything before you share or keep it, and keep backups.

  It is provided "AS IS", without warranties or conditions of any kind, and its authors are not
  liable for any harm that comes from using it (Apache License 2.0, sections 7 and 8)."""

WHAT = {
    "init": "init reads your notes, AI memories and instruction files, and writes the scrubbing settings\n"
            "  that every later export relies on.",
    "export": "export reads your notes, AI memories and instruction files, sends scrubbed excerpts of them\n"
              "  to the Anthropic API, and writes a bundle that is meant to be given to someone else.",
    "import": "import sends excerpts of your notes to the Anthropic API and writes someone else's lessons\n"
              "  into your vault or wiki.",
}

_TOKEN = object()  # only acknowledge() can make an Acknowledgement


class NotAcknowledged(Exception):
    """Something tried to write into a user's data without an acknowledgement of the risk."""


@dataclass(frozen=True)
class Acknowledgement:
    command: str
    how: str  # "typed" or "--accept-risk"
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise TypeError("an Acknowledgement is made only by kungfu.risk.acknowledge()")


def warning(command: str) -> str:
    return WARNING.format(what=WHAT[command])


def accepted(answer: str) -> bool:
    return " ".join(answer.lower().replace(".", " ").split()) == ACCEPT_PHRASE.lower()


def acknowledge(command: str, flag: bool = False, interactive: bool = False,
                ask: Callable[[str], str] | None = None,
                say: Callable[[str], None] | None = None) -> Acknowledgement | None:
    """Show the warning and get a positive acknowledgement for this one run, or return None.

    ``flag`` is --accept-risk. Otherwise the user must type the phrase; -y doesn't count, and
    without a terminal there's no one to ask, so the answer is no."""
    ask = ask or input  # looked up now, not when this module loads
    say = say or (lambda m: print(m, file=sys.stderr))
    if flag:
        say(f"kungfu {command}: experimental software, used at your own risk (acknowledged with --accept-risk).")
        return Acknowledgement(command, "--accept-risk", _token=_TOKEN)
    say(warning(command) + "\n")
    if not interactive:
        say(f"Not run: kungfu {command} needs your acknowledgement. Read the warning above, then run it in a "
            "terminal and type the phrase, or pass --accept-risk to acknowledge it for this run.")
        return None
    try:
        answer = ask(f'  Type "{ACCEPT_PHRASE}" to continue, or anything else to stop: ')
    except (EOFError, KeyboardInterrupt):
        answer = ""
        print(file=sys.stderr)
    if accepted(answer):
        return Acknowledgement(command, "typed", _token=_TOKEN)
    say("Not acknowledged, so nothing was done.")
    return None


def require(ack: Acknowledgement | None, target) -> None:
    """Refuse to write into a user's data without an acknowledgement for an import."""
    if not isinstance(ack, Acknowledgement) or ack._token is not _TOKEN or ack.command != "import":
        raise NotAcknowledged(
            f"Refusing to write into {target}: importing into someone's notes needs their acknowledgement "
            f"that kungfu is experimental and used at their own risk.\n\n{warning('import')}\n\n"
            "Get one with kungfu.risk.acknowledge('import', ...) and pass it as ack=, or run `kungfu import`.")
