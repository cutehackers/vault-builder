"""Test tier marker for builder tests (ci-qa-redesign spec 4.4).

A test marked full_tier_only runs init-vault.sh, runs install as a subprocess, runs a release gate
inside another vault, reads an old commit, or waits more than 5 seconds. The release gate skips it
when WIKI_GATE_TIER is unit. Without WIKI_GATE_TIER every test runs.
"""

from __future__ import annotations

import os
import unittest

full_tier_only = unittest.skipIf(
    os.environ.get("WIKI_GATE_TIER") == "unit",
    "full tier only: installs a vault, runs a nested release gate, reads an old commit, or waits",
)
