"""Hypothesis configuration for the ``gatk_sv_aws`` test suite.

This conftest registers two Hypothesis profiles and wires the workspace-root
``.hypothesis/`` directory as the shared example database:

* ``hypothesis`` — the default developer profile with ``max_examples=100``
  and ``deadline=None``. Loaded when ``HYPOTHESIS_PROFILE`` is unset.
* ``ci`` — the continuous-integration profile with ``max_examples=500`` and
  ``deadline=None``. Opt in with either environment variable or CLI flag::

      HYPOTHESIS_PROFILE=ci pytest tests/gatk_sv_aws
      pytest --hypothesis-profile=ci tests/gatk_sv_aws

The example database points at ``<workspace-root>/.hypothesis/`` (resolved
relative to this file) so counter-examples are shared with the existing
``tests/property/`` suite.

Precedence: this conftest calls ``settings.load_profile`` at import time,
which sets the active profile before tests run. Passing
``--hypothesis-profile=<name>`` on the pytest CLI overrides that choice
because Hypothesis' pytest plugin loads the CLI-named profile after
conftest import.
"""

from __future__ import annotations

import os
from pathlib import Path

from hypothesis import settings
from hypothesis.database import DirectoryBasedExampleDatabase

# Workspace root is three directories above this file:
#   <workspace>/kiro-life-sciences/tests/gatk_sv_aws/conftest.py
#   parents[0] = gatk_sv_aws/
#   parents[1] = tests/
#   parents[2] = kiro-life-sciences/
#   parents[3] = <workspace>
_EXAMPLE_DB_PATH = Path(__file__).resolve().parents[3] / ".hypothesis"
_EXAMPLE_DB = DirectoryBasedExampleDatabase(str(_EXAMPLE_DB_PATH))

settings.register_profile(
    "hypothesis",
    max_examples=100,
    deadline=None,
    database=_EXAMPLE_DB,
)

settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    database=_EXAMPLE_DB,
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "hypothesis"))
