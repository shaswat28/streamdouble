"""Release-shaped checks: the things that are only wrong once it is published.

PyPI never lets a version number be reused. Everything here is cheap and
catches a class of mistake whose only other detection method is someone
installing the wrong thing.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import streamdouble

ROOT = Path(__file__).resolve().parent.parent


def project_metadata() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_the_two_version_declarations_agree():
    """`pyproject.toml` and `__init__.py` are two sources for one fact.

    A mismatch ships a wheel whose `streamdouble --version` reports a
    different release from the one PyPI is serving, which is confusing in
    exactly the situation where someone is trying to work out what they have.
    """
    assert streamdouble.__version__ == project_metadata()["version"]


#: Modules that legitimately require something outside the base dependencies.
#:
#: `pytest_plugin` imports pytest at module level, and that is correct: it is
#: only ever loaded through the `pytest11` entry point, which by definition
#: fires inside a pytest run. Making pytest a hard dependency to avoid this
#: would force it on everyone who wanted the command line.
#:
#: Listed explicitly rather than skipped silently, because the first version of
#: the test below asserted that *every* module imports -- which is false in any
#: environment without pytest, and passed here only because this one has it.
#: Verified separately: `streamdouble --version` works in a virtualenv holding
#: nothing but numpy, PyYAML and websockets.
NEEDS_PYTEST = {"pytest_plugin"}


def test_every_module_imports_with_only_the_declared_dependencies():
    """The wheel ships what the package claims to contain.

    A module that exists on disk but fails to import is invisible until a user
    reaches the feature that needs it -- and by then they have the release.
    """
    import importlib

    source = ROOT / "src" / "streamdouble"
    for path in sorted(source.glob("*.py")):
        if path.stem == "__init__" or path.stem in NEEDS_PYTEST:
            continue
        importlib.import_module(f"streamdouble.{path.stem}")


def test_the_command_line_does_not_need_pytest():
    """The CLI must work for someone who never installs a test runner.

    `cli` is what `streamdouble ...` runs, so nothing on its import path may
    reach the pytest plugin. Asserted by inspecting the import graph rather
    than by uninstalling pytest, which is not something a test can do to the
    environment it is running in.
    """
    import importlib
    import sys

    for name in list(sys.modules):
        if name.startswith("streamdouble"):
            del sys.modules[name]

    importlib.import_module("streamdouble.cli")

    assert "streamdouble.pytest_plugin" not in sys.modules, (
        "importing the CLI pulled in the pytest plugin, so the command line "
        "now requires pytest"
    )


def test_the_declared_dependencies_are_the_ones_actually_needed():
    """Importing the package must not require anything undeclared.

    CI caught PyYAML being used-but-undeclared exactly this way once. The
    check is shallow -- it proves the declared set is sufficient for import,
    not that it is minimal -- but the failure it catches is the expensive one.
    """
    declared = {
        name.split(">")[0].split("=")[0].split("[")[0].strip().lower()
        for name in project_metadata()["dependencies"]
    }
    assert {"numpy", "pyyaml", "websockets"} <= declared


def test_both_entry_points_are_declared():
    """The console script and the pytest plugin.

    The pytest11 entry point is how installing the package puts fixtures in
    someone's suite; losing it would make the plugin silently absent rather
    than broken, which is harder to notice.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert config["project"]["scripts"]["streamdouble"] == "streamdouble.cli:main"
    plugin = config["project"]["entry-points"]["pytest11"]["streamdouble"]
    assert plugin == "streamdouble.pytest_plugin"


def test_the_readme_has_no_relative_links():
    """The README is the PyPI landing page, and PyPI does not resolve them.

    Verified against the live 0.1.0 page: the hero image rendered as a broken
    image at the very top, and every docs link 404'd. That is the first thing
    anyone sees when they look this package up.

    GitHub renders absolute links perfectly well, so making them absolute costs
    nothing and fixes the other audience.
    """
    import re

    text = (ROOT / "README.md").read_text(encoding="utf-8")

    # Markdown links and images whose target is neither absolute, an anchor,
    # nor a mail link.
    relative = [
        target
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]

    assert not relative, (
        "these README links will not resolve on PyPI: " + ", ".join(relative)
    )
