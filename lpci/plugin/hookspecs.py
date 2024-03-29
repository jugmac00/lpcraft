# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

from __future__ import annotations

__all__ = [
    "lpci_install_packages",
    "lpci_install_snaps",
    "lpci_execute_run",
    "lpci_execute_before_run",
    "lpci_execute_after_run",
    "lpci_set_environment",
]

import pluggy

from lpci.plugin import NAME

hookspec = pluggy.HookspecMarker(NAME)


@hookspec  # type: ignore
def lpci_install_packages() -> list[str]:
    """System packages to be installed."""


@hookspec  # type: ignore
def lpci_install_snaps() -> list[str]:
    """Snaps to be installed."""


@hookspec  # type: ignore
def lpci_execute_run() -> str:
    """Command to be executed."""
    # Please note: when both a plugin and the configuration file are
    # providing a `run` command, the one from the configuration file will be
    # used


@hookspec  # type: ignore
def lpci_set_environment() -> dict[str, str | None]:
    """Environment variables to be set."""
    # Please note: when there is the same environment variable provided by
    # the plugin and the configuration file, the one in the configuration
    # file will be taken into account


@hookspec  # type: ignore
def lpci_execute_before_run() -> str:
    """Command to execute prior to the main execution body."""


@hookspec  # type: ignore
def lpci_execute_after_run() -> str:
    """Command to execute after the main execution body."""
