# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

from argparse import Namespace

from craft_cli import BaseCommand, emit

from lpci._version import version_description as lpci_version


class VersionCommand(BaseCommand):
    """Show lpci's version number."""

    name = "version"
    help_msg = __doc__.splitlines()[0]
    overview = __doc__
    common = True

    def run(self, args: Namespace) -> int:
        """Run the command."""
        emit.message(lpci_version)
        return 0
