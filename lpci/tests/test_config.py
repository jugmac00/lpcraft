# Copyright 2021-2022 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

import os
from datetime import timedelta
from pathlib import Path
from textwrap import dedent

from fixtures import TempDir
from pydantic import AnyHttpUrl, ValidationError
from testtools import TestCase
from testtools.matchers import (
    Equals,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)

from lpci.config import (
    LAUNCHPAD_PPA_BASE_URL,
    Config,
    OutputDistributeEnum,
    PackageComponent,
    PackageFormat,
    PackageRepository,
    PackageSuite,
    PackageType,
    PPAShortFormURL,
    Snap,
    get_ppa_url_parts,
)
from lpci.errors import CommandError


class TestConfig(TestCase):
    def setUp(self):
        super().setUp()
        self.tempdir = Path(self.useFixture(TempDir()).path)
        # `Path.cwd()` is assumed as the project directory.
        # So switch to the created project directory.
        os.chdir(self.tempdir)

    def create_config(self, text):
        path = self.tempdir / ".launchpad.yaml"
        path.write_text(text)
        return path

    def test_load_config_not_under_project_dir(self):
        paths_outside_project_dir = [
            "/",
            "/etc/init.d",
            "../../foo",
            "a/b/c/../../../../d",
        ]
        for path in paths_outside_project_dir:
            config_file = Path(path) / "config.yaml"
            self.assertRaises(
                CommandError,
                Config.load,
                config_file,
            )

    def test_load(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - [test]

                jobs:
                    test:
                        series: focal
                        architectures: [amd64, arm64]
                        run-before: pip install --upgrade setuptools build
                        run: |
                            tox
                        run-after: coverage report
                """
            )
        )
        config = Config.load(path)
        self.assertThat(
            config,
            MatchesStructure(
                pipeline=Equals([["test"]]),
                jobs=MatchesDict(
                    {
                        "test": MatchesListwise(
                            [
                                MatchesStructure.byEquality(
                                    series="focal",
                                    architectures=["amd64", "arm64"],
                                    run_before="pip install --upgrade setuptools build",  # noqa:E501
                                    run="tox\n",
                                    run_after="coverage report",
                                )
                            ]
                        )
                    }
                ),
            ),
        )

    def test_load_single_pipeline(self):
        # A single pipeline element can be written as a string, and is
        # automatically wrapped in a list.
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: [amd64]
                """
            )
        )
        config = Config.load(path)
        self.assertEqual([["test"]], config.pipeline)

    def test_load_single_architecture(self):
        # A single architecture can be written as a string, and is
        # automatically wrapped in a list.
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                """
            )
        )
        config = Config.load(path)
        self.assertEqual(["amd64"], config.jobs["test"][0].architectures)

    def test_bad_job_name(self):
        # Job names must be identifiers.
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - foo:bar

                jobs:
                    'foo:bar':
                        series: focal
                        architectures: amd64
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError, r"string does not match regex", Config.load, path
        )

    def test_bad_series_name(self):
        # Series names must be identifiers.
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: something/bad
                        architectures: amd64
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError, r"string does not match regex", Config.load, path
        )

    def test_bad_architecture_name(self):
        # Architecture names must be identifiers.
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: 'not this'
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError, r"string does not match regex", Config.load, path
        )

    def test_expands_matrix(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        matrix:
                            - series: bionic
                              architectures: [amd64, arm64]

                            - series: focal
                              architectures: amd64
                              run: tox -e py38
                        run: tox
                """
            )
        )
        config = Config.load(path)
        self.assertThat(
            config,
            MatchesStructure(
                pipeline=Equals([["test"]]),
                jobs=MatchesDict(
                    {
                        "test": MatchesListwise(
                            [
                                MatchesStructure.byEquality(
                                    series="bionic",
                                    architectures=["amd64", "arm64"],
                                    run="tox",
                                ),
                                MatchesStructure.byEquality(
                                    series="focal",
                                    architectures=["amd64"],
                                    run="tox -e py38",
                                ),
                            ]
                        )
                    }
                ),
            ),
        )

    def test_load_environment(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        environment:
                            ACTIVE: 1
                            SKIP: 0

                """
            )
        )
        config = Config.load(path)
        self.assertEqual(
            {"ACTIVE": "1", "SKIP": "0"}, config.jobs["test"][0].environment
        )

    def test_output(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - build

                jobs:
                    build:
                        series: focal
                        architectures: [amd64]
                        run: pyproject-build
                        output:
                            paths: ["*.whl"]
                            distribute: artifactory
                            channels: [edge]
                            properties:
                                foo: bar
                            dynamic-properties: properties
                            expires: 1:00:00
                """
            )
        )
        config = Config.load(path)
        self.assertThat(
            config.jobs["build"][0].output,
            MatchesStructure.byEquality(
                paths=["*.whl"],
                distribute=OutputDistributeEnum.artifactory,
                channels=["edge"],
                properties={"foo": "bar"},
                dynamic_properties=Path("properties"),
                expires=timedelta(hours=1),
            ),
        )

    def test_output_negative_expires(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - build

                jobs:
                    build:
                        series: focal
                        architectures: [amd64]
                        run: pyproject-build
                        output:
                            expires: -1:00:00
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            r"non-negative duration expected",
            Config.load,
            path,
        )

    def test_input(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - build
                    - test

                jobs:
                    build:
                        series: focal
                        architectures: [amd64]
                        packages: [make]
                        run: make
                        output:
                            paths: [binary]

                    test:
                        series: focal
                        architectures: [amd64]
                        run: artifacts/binary
                        input:
                            job-name: build
                            target-directory: artifacts
                """
            )
        )
        config = Config.load(path)
        self.assertThat(
            config.jobs["test"][0].input,
            MatchesStructure.byEquality(
                job_name="build", target_directory="artifacts"
            ),
        )

    def test_load_snaps(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        snaps: [name: chromium, name: firefox]
                """
            )
        )
        config = Config.load(path)
        self.assertEqual(
            [
                Snap(name="chromium", channel="latest/stable", classic=False),
                Snap(name="firefox", channel="latest/stable", classic=False),
            ],
            config.jobs["test"][0].snaps,
        )

    def test_load_config_without_snaps(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                """
            )
        )
        config = Config.load(path)
        self.assertEqual(None, config.jobs["test"][0].snaps)

    def test_load_package(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [nginx, apache2]
                """
            )
        )
        config = Config.load(path)
        self.assertEqual(["nginx", "apache2"], config.jobs["test"][0].packages)

    def test_load_config_without_packages(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                """
            )
        )
        config = Config.load(path)
        self.assertEqual(None, config.jobs["test"][0].packages)

    def test_load_plugin(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [nginx, apache2]
                        plugin: tox
                """
            )
        )

        config = Config.load(path)

        self.assertEqual("tox", config.jobs["test"][0].plugin)

    def test_package_repositories(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [nginx, apache2]
                        package-repositories:
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [focal]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [focal]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                              trusted: false
                """  # noqa: E501
            )
        )

        config = Config.load(path)

        self.assertEqual(
            [
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.focal],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/jammy-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/jammy-golang-backport",
                    ),
                ),
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.focal],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/jammy-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/jammy-golang-backport",
                    ),
                    trusted=False,
                ),
            ],
            config.jobs["test"][0].package_repositories,
        )

    def test_package_repositories_support_all_supported_LTS_releases(self):
        """Supported releases as of now:

        - bionic
        - focal
        - jammy
        - noble
        """
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: bionic
                        architectures: amd64
                        packages: [nginx, apache2]
                        package-repositories:
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [bionic]
                              url: https://canonical.example.org/artifactory/bionic-golang-backport
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [focal]
                              url: https://canonical.example.org/artifactory/focal-golang-backport
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [jammy]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [noble]
                              url: https://canonical.example.org/artifactory/noble-golang-backport
                """  # noqa: E501
            )
        )

        config = Config.load(path)

        self.assertEqual(
            [
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.bionic],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/bionic-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/bionic-golang-backport",
                    ),
                ),
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.focal],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/focal-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/focal-golang-backport",
                    ),
                ),
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.jammy],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/jammy-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/jammy-golang-backport",
                    ),
                ),
                PackageRepository(
                    type=PackageType.apt,
                    formats=[PackageFormat.deb],
                    components=[PackageComponent.main],
                    suites=[PackageSuite.noble],
                    url=AnyHttpUrl(
                        "https://canonical.example.org/artifactory/noble-golang-backport",  # noqa: E501
                        scheme="https",
                        host="canonical.example.org",
                        tld="org",
                        host_type="domain",
                        path="/artifactory/noble-golang-backport",
                    ),
                ),
            ],
            config.jobs["test"][0].package_repositories,
        )

    def test_package_repositories_as_string(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [nginx, apache2]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              components: [main]
                              suites: [focal, bionic]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [focal]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                              trusted: true
                            - type: apt
                              formats: [deb]
                              components: [main]
                              suites: [focal]
                              url: https://canonical.example.org/artifactory/jammy-golang-backport
                              trusted: false
                """  # noqa: E501
            )
        )
        config = Config.load(path)
        expected = [
            "deb https://canonical.example.org/artifactory/jammy-golang-backport focal main",  # noqa: E501
            "deb https://canonical.example.org/artifactory/jammy-golang-backport bionic main",  # noqa: E501
            "deb-src https://canonical.example.org/artifactory/jammy-golang-backport focal main",  # noqa: E501
            "deb-src https://canonical.example.org/artifactory/jammy-golang-backport bionic main",  # noqa: E501
        ]
        repositories = config.jobs["test"][0].package_repositories
        assert repositories is not None  # workaround necessary to please mypy
        self.assertEqual(
            expected, (list(repositories[0].sources_list_lines()))
        )
        self.assertEqual(
            [
                "deb [trusted=yes] https://canonical.example.org/artifactory/jammy-golang-backport focal main"  # noqa: E501
            ],  # noqa: E501
            list(repositories[1].sources_list_lines()),
        )
        self.assertEqual(
            [
                "deb [trusted=no] https://canonical.example.org/artifactory/jammy-golang-backport focal main"  # noqa: E501
            ],  # noqa: E501
            list(repositories[2].sources_list_lines()),
        )

    def test_get_ppa_url_parts(self):
        self.assertEqual(
            ("example", "ubuntu", "foo"),
            get_ppa_url_parts(PPAShortFormURL("example/foo")),
        )
        self.assertEqual(
            ("example", "debian", "bar"),
            get_ppa_url_parts(PPAShortFormURL("example/debian/bar")),
        )

    def test_default_values_for_package_repository_suites_and_formats(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              ppa: launchpad/ubuntu/ppa
                            - type: apt
                              url: https://canonical.example.org/ubuntu
                              components: [main]
                """
            )
        )
        config = Config.load(path)

        package_repository_1 = config.jobs["test"][0].package_repositories[0]
        self.assertEqual(["deb"], package_repository_1.formats)
        self.assertEqual(["focal"], package_repository_1.suites)
        self.assertEqual(
            [f"deb {LAUNCHPAD_PPA_BASE_URL}/launchpad/ppa/ubuntu focal main"],
            list(package_repository_1.sources_list_lines()),
        )

        package_repository_2 = config.jobs["test"][0].package_repositories[1]
        self.assertEqual(["deb"], package_repository_2.formats)
        self.assertEqual(["focal"], package_repository_2.suites)
        self.assertEqual(
            ["deb https://canonical.example.org/ubuntu focal main"],
            list(package_repository_2.sources_list_lines()),
        )

    def test_missing_ppa_and_url(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              components: [main]
                              suites: [focal]
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            r"One of the following keys is required with an appropriate"
            r" value: 'url', 'ppa'",
            Config.load,
            path,
        )

    def test_both_ppa_and_url_provided(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              suites: [focal]
                              ppa: launchpad/ppa
                              url: https://canonical.example.com
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            r"Only one of the following keys can be specified:"
            r" 'url', 'ppa'",
            Config.load,
            path,
        )

    def test_missing_components_when_url_is_specified(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              suites: [focal]
                              url: https://canonical.example.com
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            r"The 'components' key is required when the 'url' key"
            r" is specified.",
            Config.load,
            path,
        )

    def test_both_ppa_and_components_specified(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              components: [main]
                              ppa: launchpad/ppa
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            r"The 'components' key is not allowed when the 'ppa' key is"
            r" specified. PPAs only support the 'main' component.",
            Config.load,
            path,
        )

    def test_ppa_shortform_url_and_components_automatically_inferred(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test
                jobs:
                    test:
                        series: focal
                        architectures: amd64
                        packages: [foo]
                        package-repositories:
                            - type: apt
                              formats: [deb, deb-src]
                              suites: [focal]
                              ppa: launchpad/ppa
                            - type: apt
                              formats: [deb, deb-src]
                              suites: [focal]
                              ppa: launchpad/debian/ppa2
                """
            )
        )
        config = Config.load(path)
        package_repository = config.jobs["test"][0].package_repositories[0]
        package_repository_2 = config.jobs["test"][0].package_repositories[1]

        self.assertEqual("launchpad/ppa", package_repository.ppa)
        self.assertEqual(
            "{}/launchpad/ppa/ubuntu".format(
                LAUNCHPAD_PPA_BASE_URL,
            ),
            str(package_repository.url),
        )
        self.assertEqual(["main"], package_repository.components)
        self.assertEqual("launchpad/debian/ppa2", package_repository_2.ppa)
        self.assertEqual(
            "{}/launchpad/ppa2/debian".format(
                LAUNCHPAD_PPA_BASE_URL,
            ),
            str(package_repository_2.url),
        )
        self.assertEqual(["main"], package_repository_2.components)

    def test_specify_license_via_spdx(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                license:
                    spdx: "MIT"
                """  # noqa: E501
            )
        )
        config = Config.load(path)

        # workaround necessary to please mypy
        assert config.license is not None
        self.assertEqual("MIT", config.license.spdx)

    def test_specify_license_via_path(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                license:
                    path: LICENSE.txt
                """  # noqa: E501
            )
        )
        config = Config.load(path)

        # workaround necessary to please mypy
        assert config.license is not None
        self.assertEqual("LICENSE.txt", config.license.path)

    def test_license_setting_both_sources_not_allowed(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        series: focal
                        architectures: amd64
                license:
                    spdx: MIT
                    path: LICENSE.txt
                """  # noqa: E501
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            "You cannot set `spdx` and `path` at the same time.",
            Config.load,
            path,
        )

    def test_root_value(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        root: True
                        series: focal
                        architectures: amd64
                """
            )
        )
        config = Config.load(path)
        root_value = config.jobs["test"][0].root
        self.assertEqual(True, root_value)

    def test_bad_root_value(self):
        path = self.create_config(
            dedent(
                """
                pipeline:
                    - test

                jobs:
                    test:
                        root: bad_value
                        series: focal
                        architectures: amd64
                """
            )
        )
        self.assertRaisesRegex(
            ValidationError,
            "You configured `root` parameter, "
            + "but you did not specify a valid value. "
            + "Valid values would either be `true` or `false`.",
            Config.load,
            path,
        )
