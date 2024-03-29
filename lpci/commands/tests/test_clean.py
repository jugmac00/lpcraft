# Copyright 2022 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

import os
from pathlib import Path
from unittest.mock import patch

from fixtures import TempDir
from testtools.matchers import MatchesStructure

from lpci.commands.tests import CommandBaseTestCase
from lpci.errors import CommandError, ConfigurationError
from lpci.providers.tests import makeLXDProvider


class TestClean(CommandBaseTestCase):
    def setUp(self):
        super().setUp()
        self.tmp_project_path = Path(
            self.useFixture(TempDir()).join("test-clean-project")
        )
        self.tmp_project_path.mkdir()
        cwd = Path.cwd()
        os.chdir(self.tmp_project_path)
        self.addCleanup(os.chdir, cwd)

    def test_config_file_not_under_project_directory(self):
        paths = [
            "/",
            "/etc/init.d",
            "../../foo",
            "a/b/c/../../../../d",
        ]

        for path in paths:
            config_file = f"{path}/config.yaml"
            result = self.run_command(
                "clean",
                "-c",
                config_file,
            )
            config_file_path = Path(config_file).resolve()
            self.assertThat(
                result,
                MatchesStructure.byEquality(
                    exit_code=1,
                    errors=[
                        ConfigurationError(
                            f"'{config_file_path}' is not in the subpath of "
                            f"'{self.tmp_project_path}'."
                        )
                    ],
                ),
            )

    def test_missing_config_file(self):
        result = self.run_command("clean")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    ConfigurationError(
                        "Couldn't find config file '.launchpad.yaml'"
                    ),
                ],
            ),
        )

        tmp_config_file = os.path.join(
            self.tmp_project_path,
            "test-clean-config/lpci-configuration.yaml",
        )

        result = self.run_command("clean", "-c", tmp_config_file)

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    ConfigurationError(
                        f"Couldn't find config file '{tmp_config_file}'"
                    )
                ],
            ),
        )

    @patch("lpci.commands.clean.get_provider")
    def test_lxd_not_ready(self, mock_get_provider):
        mock_get_provider.return_value = makeLXDProvider(is_ready=False)
        Path(".launchpad.yaml").write_text("pipeline: []\njobs: {}")

        result = self.run_command("clean")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[CommandError("LXD is broken")],
            ),
        )

    @patch("lpci.commands.clean.get_provider")
    @patch("lpci.providers._lxd.LXDProvider.clean_project_environments")
    def test_clean_cleans_project_environments(
        self, mock_clean_project_environments, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider()
        Path(".launchpad.yaml").write_text("pipeline: []\njobs: {}")

        self.run_command("clean")

        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
        )
