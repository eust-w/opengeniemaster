"""OpenGenieMaster: an open embodied continual harness for persistent multi-robot work.

An independent, community re-implementation of the ideas described for AgiBot's
GenieMaster (an embodied agent framework connecting a coding agent to robot
policies through an Embodied Execution Protocol). Not affiliated with AgiBot.
"""

import sys

if sys.version_info < (3, 10):  # pragma: no cover - exercised only on old interpreters
    raise ImportError(f"OpenGenieMaster requires Python 3.10 or newer (running {sys.version.split()[0]})")

__version__ = "0.1.0"

__all__ = ["__version__"]
