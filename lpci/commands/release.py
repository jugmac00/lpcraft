# Copyright 2023 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

import re
from argparse import ArgumentParser, Namespace
from collections import defaultdict
from operator import attrgetter
from typing import Dict, List
from urllib.parse import urlparse

from craft_cli import BaseCommand, emit
from launchpadlib.launchpad import Launchpad
from lazr.restfulclient.resource import Entry

from lpci.errors import CommandError
from lpci.git import get_current_branch, get_current_remote_url


class ReleaseCommand(BaseCommand):
    """Release a Launchpad build of a commit to a target archive."""

    name = "release"
    help_msg = __doc__.splitlines()[0]
    overview = __doc__
    common = True

    def fill_parser(self, parser: ArgumentParser) -> None:
        """Add arguments specific to this command."""
        parser.add_argument(
            "-l",
            "--launchpad",
            dest="launchpad_instance",
            default="production",
            help="Use this Launchpad instance.",
        )
        parser.add_argument(
            "-n",
            "--dry-run",
            default=False,
            action="store_true",
            help="Just report what would be done.",
        )
        parser.add_argument(
            "--repository",
            help=(
                "Git repository URL (defaults to the upstream repository for "
                "the current branch, if on git.launchpad.net)"
            ),
        )
        parser.add_argument(
            "--commit",
            help=(
                "Git branch name, tag name, or commit ID (defaults to the "
                "current branch)"
            ),
        )
        parser.add_argument(
            "-a",
            "--architecture",
            help=(
                "Only release the latest build for this architecture "
                "(defaults to the latest build for each built architecture)"
            ),
        )
        parser.add_argument(
            "archive", help="Target archive, e.g. ppa:OWNER/DISTRIBUTION/NAME"
        )
        parser.add_argument("suite", help="Target suite, e.g. focal")
        parser.add_argument("channel", help="Target channel, e.g. edge")

    def _check_args(self, args: Namespace) -> None:
        """Check and process arguments."""
        if args.repository is None:
            current_remote_url = get_current_remote_url()
            if current_remote_url is None:
                raise CommandError(
                    "No --repository option was given, and the current branch "
                    "does not track a remote branch."
                )
            parsed_url = urlparse(current_remote_url)
            # XXX cjwatson 2023-01-04: Ideally this would check for the git
            # service corresponding to the --launchpad argument rather than
            # hardcoding git.launchpad.net.
            if parsed_url.hostname == "git.launchpad.net":
                args.repository = parsed_url.path
            else:
                raise CommandError(
                    "No --repository option was given, and the current branch "
                    "does not track a remote branch on git.launchpad.net."
                )
        args.repository = args.repository.lstrip("/")
        if args.commit is None:
            args.commit = get_current_branch()
            if args.commit is None:
                raise CommandError(
                    "No --commit option was given, and there is no current "
                    "branch."
                )

    def _find_builds(
        self, launchpad: Launchpad, args: Namespace
    ) -> Dict[str, List[Entry]]:
        repository = launchpad.git_repositories.getByPath(path=args.repository)
        if repository is None:
            raise CommandError(
                f"Repository {args.repository} does not exist on Launchpad."
            )
        if re.match(r"^[0-9a-f]{40}$", args.commit) is None:
            ref = repository.getRefByPath(path=args.commit)
            if ref is None:
                raise CommandError(
                    f"{args.repository} has no branch or tag named "
                    f"{args.commit}."
                )
            args.commit = ref.commit_sha1
        reports = repository.getStatusReports(commit_sha1=args.commit)
        builds = [
            report.ci_build
            for report in reports
            if report.ci_build is not None
            and (
                args.architecture is None
                or report.ci_build.arch_tag == args.architecture
            )
            and report.ci_build.buildstate == "Successfully built"
            and report.getArtifactURLs(artifact_type="Binary")
        ]
        if not builds:
            raise CommandError(
                f"{args.repository}:{args.commit} has no completed CI "
                f"builds with attached files."
            )
        builds_by_arch = defaultdict(list)
        for build in builds:
            builds_by_arch[build.arch_tag].append(build)
        return builds_by_arch

    def _release_build(
        self, launchpad: Launchpad, build: Entry, args: Namespace
    ) -> None:
        archive = launchpad.archives.getByReference(reference=args.archive)
        description = (
            f"{build.arch_tag} build of {args.repository}:{args.commit} to "
            f"{args.archive} {args.suite} {args.channel}"
        )
        if args.dry_run:
            emit.message(f"Would release {description}.")
        else:
            archive.uploadCIBuild(
                ci_build=build,
                to_series=args.suite,
                to_pocket="Release",
                to_channel=args.channel,
            )
            emit.message(f"Released {description}.")

    def run(self, args: Namespace) -> int:
        """Run the command."""
        self._check_args(args)
        launchpad = Launchpad.login_with(
            "lpci", args.launchpad_instance, version="devel"
        )
        builds_by_arch = self._find_builds(launchpad, args)
        if args.architecture is not None:
            arch_tags = [args.architecture]
        else:
            arch_tags = sorted(builds_by_arch)
        for arch_tag in arch_tags:
            latest_build = sorted(
                builds_by_arch[arch_tag], key=attrgetter("datebuilt")
            )[-1]
            self._release_build(launchpad, latest_build, args)
        return 0
