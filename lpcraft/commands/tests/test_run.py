# Copyright 2021-2022 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

import io
import json
import os
import re
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import Any, AnyStr, Dict, List, Optional
from unittest.mock import ANY, Mock, call, patch

import responses
from craft_providers.lxd import launch
from fixtures import TempDir
from testtools.matchers import MatchesStructure

from lpcraft.commands.tests import CommandBaseTestCase
from lpcraft.errors import CommandError, ConfigurationError
from lpcraft.providers.tests import makeLXDProvider


class LocalExecuteRun:
    """A fake LXDInstance.execute_run that runs subprocesses locally.

    This allows us to set up a temporary directory with the expected
    contents and then run processes in it more or less normally.  Don't run
    complicated build commands using this, but ordinary system utilities are
    fine.
    """

    def __init__(self, override_cwd: Path):
        super().__init__()
        self.override_cwd = override_cwd
        self.call_args_list: List[Any] = []

    def __call__(
        self,
        command: List[str],
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, Optional[str]]] = None,
        **kwargs: Any,
    ) -> "subprocess.CompletedProcess[AnyStr]":
        run_kwargs = kwargs.copy()
        run_kwargs["cwd"] = self.override_cwd
        if env is not None:  # pragma: no cover
            full_env = os.environ.copy()
            for key, value in env.items():
                if value is None:
                    full_env.pop(key, None)
                else:
                    full_env[key] = value
            run_kwargs["env"] = full_env
        self.call_args_list.append(call(command, **run_kwargs))
        return subprocess.run(command, **run_kwargs)


class RunBaseTestCase(CommandBaseTestCase):
    """Common code for run and run-one tests."""

    def setUp(self):
        super().setUp()
        self.tmp_project_path = Path(
            self.useFixture(TempDir()).join("test-project")
        )
        self.tmp_project_path.mkdir()
        cwd = Path.cwd()
        os.chdir(self.tmp_project_path)

        self.addCleanup(os.chdir, cwd)

    def get_instance_names(self, provider, series, architecture="amd64"):
        return [
            provider.get_instance_name(
                project_name=self.tmp_project_path.name,
                project_path=self.tmp_project_path,
                series=series_name,
                architecture=architecture,
            )
            for series_name in series
        ]


class TestRun(RunBaseTestCase):
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
                "run",
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
        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    ConfigurationError(
                        "Couldn't find config file '.launchpad.yaml'"
                    )
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_path_config_file(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # When a custom location / config file ensure we
        # pick it up instead of defaulting to .launchpad.yaml.
        self.tmp_config_path = os.path.join(
            self.tmp_project_path, "test-config"
        )
        Path(self.tmp_config_path).mkdir(parents=True, exist_ok=True)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - build-wheel

            jobs:
                build-wheel:
                    series: focal
                    architectures: amd64
                    run: pyproject-build
            """
        )
        path = "%s/lpcraft-configuration.yaml" % self.tmp_config_path
        Path(path).write_text(config)

        self.run_command("run", "-c", path)

        execute_run.assert_called_once_with(
            ["bash", "--noprofile", "--norc", "-ec", "pyproject-build"],
            cwd=Path("/root/lpcraft/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_lxd_not_ready(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider(is_ready=False)
        config = dedent(
            """
            pipeline: []
            jobs: {}
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[CommandError("LXD is broken")],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_job_not_defined(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider()
        config = dedent(
            """
            pipeline:
                - test

            jobs: {}
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[CommandError("No job definition for 'test'")],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="arm64")
    def test_job_not_defined_for_host_architecture(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # Jobs not defined for the host architecture are skipped.  (It is
        # assumed that the dispatcher won't dispatch anything for an
        # architecture if it has no jobs at all.)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    series: focal
                    architectures: [amd64, arm64]
                    run: tox
                build-wheel:
                    series: focal
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            ["focal"],
            [c.kwargs["image_name"] for c in launcher.call_args_list],
        )
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_no_run_definition(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider()
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    CommandError(
                        "Job 'test' for focal/amd64 does not set 'run'"
                    )
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_one_job_fails(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 2)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: focal
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=2,
                errors=[
                    CommandError(
                        "Job 'test' for focal/amd64 failed with exit status "
                        "2.",
                        retcode=2,
                    )
                ],
            ),
        )
        execute_run.assert_called_once_with(
            ["bash", "--noprofile", "--norc", "-ec", "tox"],
            cwd=Path("/root/lpcraft/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_all_jobs_succeed(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            ["focal", "bionic"],
            [c.kwargs["image_name"] for c in launcher.call_args_list],
        )
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-ec",
                        "pyproject-build",
                    ],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_apt_replace_repositories(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        launcher.return_value.pull_file
        config = dedent(
            """
            pipeline:
                - test
            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: ls -la
                    packages: [git]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--apt-replace-repositories", "repo info"
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["apt", "update"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["apt", "install", "-y", "git"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "ls -la"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

        mock_info = launcher.return_value.push_file_io.call_args_list[0][1]
        self.assertEqual(
            Path("/etc/apt/sources.list"), mock_info["destination"]
        )
        self.assertEqual("repo info\n", mock_info["content"].read().decode())
        self.assertEqual("0644", mock_info["file_mode"])
        self.assertEqual("root", mock_info["group"])
        self.assertEqual("root", mock_info["user"])

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_updating_package_info_fails(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 100)
        config = dedent(
            """
            pipeline:
                - test
            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: ls -la
                    packages: [git]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--apt-replace-repositories", "repo info"
        )

        self.assertEqual(100, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["apt", "update"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_default_to_run_command(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # calling `lpcraft` with no arguments triggers the run command
        # and is functionally equivalent to `lpcraft run`
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command()

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_parallel_jobs_some_fail(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # Right now "parallel" jobs are not in fact executed in parallel,
        # but we act if they are for the purpose of error handling: even if
        # one job in a stage fails, we run all the jobs in that stage before
        # stopping.
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.side_effect = iter(
            [subprocess.CompletedProcess([], ret) for ret in (2, 0, 0)]
        )
        config = dedent(
            """
            pipeline:
                - [lint, test]
                - build-wheel

            jobs:
                lint:
                    series: focal
                    architectures: amd64
                    run: flake8
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    CommandError(
                        "Job 'lint' for focal/amd64 failed with exit status "
                        "2.",
                        retcode=2,
                    ),
                    CommandError(
                        "Some jobs in ['lint', 'test'] failed; stopping."
                    ),
                ],
            ),
        )
        self.assertEqual(
            ["focal", "focal"],
            [c.kwargs["image_name"] for c in launcher.call_args_list],
        )
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", command],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                )
                for command in ("flake8", "tox")
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_parallel_jobs_all_succeed(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # Right now "parallel" jobs are not in fact executed in parallel,
        # but we do at least wait for all of them to succeed before
        # proceeding to the next stage in the pipeline.
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.side_effect = iter(
            [subprocess.CompletedProcess([], ret) for ret in (0, 0, 0)]
        )
        config = dedent(
            """
            pipeline:
                - [lint, test]
                - build-wheel

            jobs:
                lint:
                    series: focal
                    architectures: amd64
                    run: flake8
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            ["focal", "focal", "bionic"],
            [c.kwargs["image_name"] for c in launcher.call_args_list],
        )
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", command],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                )
                for command in ("flake8", "tox", "pyproject-build")
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_expands_matrix(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    matrix:
                        - series: bionic
                          architectures: amd64
                        - series: focal
                          architectures: [amd64, s390x]
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            ["bionic", "focal", "bionic"],
            [c.kwargs["image_name"] for c in launcher.call_args_list],
        )
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    [
                        "bash",
                        "--noprofile",
                        "--norc",
                        "-ec",
                        "pyproject-build",
                    ],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_configuration(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                    environment:
                        TOX_SKIP_ENV: '^(?!lint-)'
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")
        self.assertEqual(0, result.exit_code)

        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={"TOX_SKIP_ENV": "^(?!lint-)"},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--set-env", "PIP_INDEX_URL=http://pypi.example.com/simple"
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={"PIP_INDEX_URL": "http://pypi.example.com/simple"},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_copies_output_paths(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        def fake_pull_file(source: Path, destination: Path) -> None:
            destination.touch()

        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        launcher.return_value.pull_file.side_effect = fake_pull_file
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["*.tar.gz", "*.whl"]
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("test_1.0.tar.gz").write_bytes(b"")
        Path("test_1.0.whl").write_bytes(b"")
        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "build" / "focal" / "amd64"
        self.assertEqual(
            [
                call(
                    source=self.tmp_project_path / "test_1.0.tar.gz",
                    destination=job_output / "files" / "test_1.0.tar.gz",
                ),
                call(
                    source=self.tmp_project_path / "test_1.0.whl",
                    destination=job_output / "files" / "test_1.0.whl",
                ),
            ],
            launcher.return_value.pull_file.call_args_list,
        )
        self.assertEqual(
            ["files", "properties"],
            sorted(path.name for path in job_output.iterdir()),
        )
        self.assertEqual(
            ["test_1.0.tar.gz", "test_1.0.whl"],
            sorted(path.name for path in (job_output / "files").iterdir()),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_output_path_in_immediate_parent(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        def fake_pull_file(source: Path, destination: Path) -> None:
            destination.touch()

        target_path = Path(self.useFixture(TempDir()).path) / "build"
        target_path.mkdir()
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        launcher.return_value.pull_file.side_effect = fake_pull_file
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: touch ../test_1.0_all.deb
                    output:
                        paths: ["../*.deb"]
            """
        )
        Path(".launchpad.yaml").write_text(config)
        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "build" / "focal" / "amd64"
        self.assertEqual(
            [
                call(
                    source=self.tmp_project_path.parent / "test_1.0_all.deb",
                    destination=job_output / "files" / "test_1.0_all.deb",
                ),
            ],
            launcher.return_value.pull_file.call_args_list,
        )
        self.assertEqual(
            ["files", "properties"],
            sorted(path.name for path in job_output.iterdir()),
        )
        self.assertEqual(
            ["test_1.0_all.deb"],
            sorted(path.name for path in (job_output / "files").iterdir()),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_output_path_escapes_directly(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["../../etc/shadow"]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        # The exact error message differs between Python 3.8 and 3.9, so
        # don't test it in detail, but make sure it includes the offending
        # path.
        self.assertEqual(1, result.exit_code)
        [error] = result.errors
        self.assertIn("/etc/shadow", str(error))

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_output_path_escapes_symlink(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["*.txt"]
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("symlink.txt").symlink_to("../../target.txt")

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        # The exact error message differs between Python 3.8 and 3.9, so
        # don't test it in detail, but make sure it includes the offending
        # path.
        self.assertEqual(1, result.exit_code)
        [error] = result.errors
        self.assertIn("/target.txt", str(error))

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_output_path_pull_file_fails(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        launcher.return_value.pull_file.side_effect = FileNotFoundError(
            "File not found"
        )
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["*.whl"]
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("test_1.0.whl").write_bytes(b"")

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1, errors=[CommandError("File not found", retcode=1)]
            ),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_shows_error_message_when_no_output_files(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["*.whl"]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    CommandError("*.whl has not matched any output files.")
                ],
            ),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_reads_properties(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        properties:
                            foo: bar
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "build" / "focal" / "amd64"
        self.assertEqual(
            {"foo": "bar"},
            json.loads((job_output / "properties").read_text()),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_reads_dynamic_properties(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        dynamic-properties: properties
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("properties").write_text("version=0.1\n")

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "test" / "focal" / "amd64"
        self.assertEqual(
            {"version": "0.1"},
            json.loads((job_output / "properties").read_text()),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_dynamic_properties_override_properties(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        properties:
                            version: "0.1"
                            to-be-removed: "x"
                        dynamic-properties: properties
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("properties").write_text(
            "version=0.2\nto-be-removed\nalready-missing\n"
        )

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "test" / "focal" / "amd64"
        self.assertEqual(
            {"version": "0.2"},
            json.loads((job_output / "properties").read_text()),
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_run_dynamic_properties_escapes_directly(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        dynamic-properties: ../properties
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        # The exact error message differs between Python 3.8 and 3.9, so
        # don't test it in detail, but make sure it includes the offending
        # path.
        self.assertEqual(1, result.exit_code)
        [error] = result.errors
        self.assertIn("/properties", str(error))

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_run_dynamic_properties_escapes_symlink(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        dynamic-properties: properties
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("properties").symlink_to("../target")

        result = self.run_command(
            "run", "--output-directory", str(target_path)
        )

        # The exact error message differs between Python 3.8 and 3.9, so
        # don't test it in detail, but make sure it includes the offending
        # path.
        self.assertEqual(1, result.exit_code)
        [error] = result.errors
        self.assertIn("/target", str(error))

    @responses.activate
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_install_snaps(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        responses.add(
            "GET",
            "http+unix://%2Frun%2Fsnapd.socket/v2/find?name=chromium",
            json={
                "result": [{"channels": {"latest/stable": {"revision": "1"}}}]
            },
        )
        responses.add(
            "GET",
            "http+unix://%2Frun%2Fsnapd.socket/v2/find?name=firefox",
            json={
                "result": [{"channels": {"latest/stable": {"revision": "1"}}}]
            },
        )
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                    snaps: [chromium, firefox]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    [
                        "snap",
                        "download",
                        "chromium",
                        "--channel=latest/stable",
                        "--basename=chromium",
                        "--target-directory=/tmp",
                    ],
                    check=True,
                    capture_output=True,
                ),
                call(
                    [
                        "snap",
                        "install",
                        "/tmp/chromium.snap",
                        "--classic",
                        "--dangerous",
                    ],
                    check=True,
                    capture_output=True,
                ),
                call(
                    [
                        "snap",
                        "download",
                        "firefox",
                        "--channel=latest/stable",
                        "--basename=firefox",
                        "--target-directory=/tmp",
                    ],
                    check=True,
                    capture_output=True,
                ),
                call(
                    [
                        "snap",
                        "install",
                        "/tmp/firefox.snap",
                        "--classic",
                        "--dangerous",
                    ],
                    check=True,
                    capture_output=True,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_install_system_packages(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                    packages: [nginx, apache2]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")
        self.assertEqual(0, result.exit_code)

        self.assertEqual(
            [
                call(
                    [
                        "apt",
                        "install",
                        "-y",
                        "nginx",
                        "apache2",
                    ],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_installing_unknown_system_package_fails(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 100)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: ls -la
                    packages: [unknown_package]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")

        self.assertEqual(100, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["apt", "install", "-y", "unknown_package"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("sys.stderr", new_callable=io.StringIO)
    def test_quiet(
        self, mock_stderr, mock_get_host_architecture, mock_get_provider
    ):
        def execute_run(
            command: List[str], **kwargs: Any
        ) -> "subprocess.CompletedProcess[AnyStr]":
            os.write(kwargs["stdout"], b"test\n")
            return subprocess.CompletedProcess([], 0)

        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        launcher.return_value.execute_run.side_effect = execute_run
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("-q", "run")
        self.assertEqual(0, result.exit_code)
        self.assertEqual("", mock_stderr.getvalue())

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("sys.stderr", new_callable=io.StringIO)
    def test_normal(
        self, mock_stderr, mock_get_host_architecture, mock_get_provider
    ):
        def execute_run(
            command: List[str], **kwargs: Any
        ) -> "subprocess.CompletedProcess[AnyStr]":
            os.write(kwargs["stdout"], b"test\n")
            return subprocess.CompletedProcess([], 0)

        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        launcher.return_value.execute_run.side_effect = execute_run
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run")
        self.assertEqual(0, result.exit_code)
        stderr_lines = [
            re.sub(
                r"^(?P<date>.+?) (?P<time>.+?) (?P<text>.*?) *$",
                r"\g<text>",
                line,
            )
            for line in mock_stderr.getvalue().splitlines()
        ]
        self.assertEqual(
            [
                "Running ['bash', '--noprofile', '--norc', '-ec', "
                "'echo test']",
                ":: test",
            ],
            stderr_lines[-2:],
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_cleans_up_the_managed_environment(
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
    ):
        def execute_run(
            command: List[str], **kwargs: Any
        ) -> "subprocess.CompletedProcess[AnyStr]":
            os.write(kwargs["stdout"], b"test\n")
            return subprocess.CompletedProcess([], 0)

        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        launcher.return_value.execute_run.side_effect = execute_run
        config = dedent(
            """
            pipeline:
                - test
                - test2

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: echo test
                test2:
                    series: bionic
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run", "--clean")

        self.assertEqual(0, result.exit_code)
        expected_instance_names = self.get_instance_names(
            provider, ("focal", "bionic")
        )
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=expected_instance_names,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_clean_flag_cleans_up_even_when_there_are_errors(
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
    ):
        mock_get_provider.return_value = makeLXDProvider()
        # There are no jobs defined. So there will be an error.
        config = dedent(
            """
            pipeline:
                - test

            jobs: {}
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run", "--clean")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[CommandError("No job definition for 'test'")],
            ),
        )
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=[],
        )

    @patch("lpcraft.commands.run._run_job")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_cleans_up_only_the_instances_created_for_the_current_run(
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
        mock_run_job,
    ):
        mock_get_provider.return_value = makeLXDProvider()
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: xenial
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(config)

        self.run_command("run")

        updated_config = dedent(
            """
            pipeline:
                - test
                - test2
                - test3

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: echo test
                test2:
                    series: bionic
                    architectures: amd64
                    run: echo test
                test3:
                    series: jammy
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(updated_config)

        result = self.run_command("run", "--clean")

        self.assertEqual(0, result.exit_code)
        expected_instance_names = self.get_instance_names(
            provider, ("focal", "bionic", "jammy")
        )
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=expected_instance_names,
        )

    @patch("lpcraft.commands.run._run_job")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_cleans_up_only_the_instances_created_in_the_current_run_when_a_job_errors_out(  # noqa: E501
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
        mock_run_job,
    ):
        mock_get_provider.return_value = makeLXDProvider()
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        mock_run_job.side_effect = [None, CommandError("Mock error")]
        config = dedent(
            """
            pipeline:
                - test
                - test2
                - test3

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: echo test
                test2:
                    series: bionic
                    architectures: amd64
                    run: echo test
                test3:
                    series: jammy
                    architectures: amd64
                    run: echo test
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run", "--clean")

        self.assertEqual(1, result.exit_code)
        expected_instance_names = self.get_instance_names(
            provider,
            ("focal", "bionic"),
        )
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=expected_instance_names,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli_copes_with_equal_sign_in_value(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run",
            "--set-env",
            "DOUBLE_EQUAL=value_with=another_equal_sign",
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={
                        "DOUBLE_EQUAL": "value_with=another_equal_sign",
                    },
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli_ensure_merge_order(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # env from CLI wins over env from configuration
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                    environment:
                        PIP_INDEX_URL: http://pypi.example.com/simple
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run",
            "--set-env",
            "PIP_INDEX_URL=http://local-pypi.example.com/simple",
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={
                        "PIP_INDEX_URL": "http://local-pypi.example.com/simple"
                    },
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )


class TestRunOne(RunBaseTestCase):
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
                "run-one",
                "-c",
                config_file,
                "test",
                "0",
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
        result = self.run_command("run-one", "test", "0")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    ConfigurationError(
                        "Couldn't find config file '.launchpad.yaml'"
                    )
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_path_config_file(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # When a custom location / config file is provided, ensure we
        # pick it up instead of defaulting to .launchpad.yaml.
        self.tmp_config_path = os.path.join(
            self.tmp_project_path, "test-config"
        )
        Path(self.tmp_config_path).mkdir(parents=True, exist_ok=True)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    matrix:
                        - series: bionic
                          architectures: amd64
                        - series: focal
                          architectures: [amd64, s390x]
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        path = "%s/configuration.yaml" % self.tmp_config_path
        Path(path).write_text(config)

        self.run_command("run-one", "-c", path, "test", "1")

        execute_run.assert_called_once_with(
            ["bash", "--noprofile", "--norc", "-ec", "tox"],
            cwd=Path("/root/lpcraft/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_job_not_defined(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider()
        config = dedent(
            """
            pipeline:
                - test

            jobs: {}
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "test", "0")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[CommandError("No job definition for 'test'")],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_job_index_not_defined(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = makeLXDProvider()
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "test", "1")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    CommandError("No job definition with index 1 for 'test'")
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_job_fails(self, mock_get_host_architecture, mock_get_provider):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 2)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: focal
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "build-wheel", "0")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=2,
                errors=[
                    CommandError(
                        "Job 'build-wheel' for focal/amd64 failed with exit "
                        "status 2.",
                        retcode=2,
                    )
                ],
            ),
        )
        execute_run.assert_called_once_with(
            ["bash", "--noprofile", "--norc", "-ec", "pyproject-build"],
            cwd=Path("/root/lpcraft/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_expands_matrix(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    matrix:
                        - series: bionic
                          architectures: amd64
                        - series: focal
                          architectures: [amd64, s390x]
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "test", "1")

        self.assertEqual(0, result.exit_code)
        # We selected only one job, which ran on focal.
        launcher.assert_called_once()
        self.assertEqual(
            "focal", launcher.call_args_list[0].kwargs["image_name"]
        )
        execute_run.assert_called_once_with(
            ["bash", "--noprofile", "--norc", "-ec", "tox"],
            cwd=Path("/root/lpcraft/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )

    @patch("lpcraft.env.get_managed_environment_project_path")
    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_copies_output_paths(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        def fake_pull_file(source: Path, destination: Path) -> None:
            destination.touch()

        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = LocalExecuteRun(self.tmp_project_path)
        launcher.return_value.execute_run = execute_run
        mock_get_project_path.return_value = self.tmp_project_path
        launcher.return_value.pull_file.side_effect = fake_pull_file
        config = dedent(
            """
            pipeline:
                - build

            jobs:
                build:
                    series: focal
                    architectures: amd64
                    run: |
                        true
                    output:
                        paths: ["*.tar.gz", "*.whl"]
            """
        )
        Path(".launchpad.yaml").write_text(config)
        Path("test_1.0.tar.gz").write_bytes(b"")
        Path("test_1.0.whl").write_bytes(b"")
        result = self.run_command(
            "run-one", "--output-directory", str(target_path), "build", "0"
        )

        self.assertEqual(0, result.exit_code)
        job_output = target_path / "build" / "focal" / "amd64"
        self.assertEqual(
            [
                call(
                    source=self.tmp_project_path / "test_1.0.tar.gz",
                    destination=job_output / "files" / "test_1.0.tar.gz",
                ),
                call(
                    source=self.tmp_project_path / "test_1.0.whl",
                    destination=job_output / "files" / "test_1.0.whl",
                ),
            ],
            launcher.return_value.pull_file.call_args_list,
        )
        self.assertEqual(
            ["files", "properties"],
            sorted(path.name for path in job_output.iterdir()),
        )
        self.assertEqual(
            ["test_1.0.tar.gz", "test_1.0.whl"],
            sorted(path.name for path in (job_output / "files").iterdir()),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_run_one_clean_flag_cleans_up_even_when_there_are_errors(
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 2)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                build-wheel:
                    series: focal
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "--clean", "build-wheel", "0")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=2,
                errors=[
                    CommandError(
                        "Job 'build-wheel' for focal/amd64 failed with exit "
                        "status 2.",
                        retcode=2,
                    )
                ],
            ),
        )
        instance_names = self.get_instance_names(
            provider,
            ("focal",),
        )
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=instance_names,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    @patch("lpcraft.providers._lxd.LXDProvider.clean_project_environments")
    def test_run_one_clean_flag_cleans_up_the_managed_environment(
        self,
        mock_clean_project_environments,
        mock_get_host_architecture,
        mock_get_provider,
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test
                - build-wheel

            jobs:
                test:
                    matrix:
                        - series: bionic
                          architectures: amd64
                        - series: focal
                          architectures: [amd64, s390x]
                    run: tox
                build-wheel:
                    series: bionic
                    architectures: amd64
                    run: pyproject-build
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command("run-one", "--clean", "test", "1")

        self.assertEqual(0, result.exit_code)
        instance_names = self.get_instance_names(provider, ("focal",))
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=instance_names,
        )

        result = self.run_command("run-one", "--clean", "test", "0")

        self.assertEqual(0, result.exit_code)
        instance_names = self.get_instance_names(provider, ("bionic",))
        mock_clean_project_environments.assert_called_with(
            project_name=self.tmp_project_path.name,
            project_path=self.tmp_project_path,
            instances=instance_names,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_apt_replace_repositories(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        launcher.return_value.pull_file
        config = dedent(
            """
            pipeline:
                - test
            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: ls -la
                    packages: [git]
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run-one", "--apt-replace-repositories", "repo info", "test", "0"
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["apt", "update"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["apt", "install", "-y", "git"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "ls -la"],
                    cwd=Path("/root/lpcraft/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

        mock_info = launcher.return_value.push_file_io.call_args_list[0][1]
        self.assertEqual(
            Path("/etc/apt/sources.list"), mock_info["destination"]
        )
        self.assertEqual("repo info\n", mock_info["content"].read().decode())
        self.assertEqual("0644", mock_info["file_mode"])
        self.assertEqual("root", mock_info["group"])
        self.assertEqual("root", mock_info["user"])

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run-one",
            "--set-env",
            "PIP_INDEX_URL=http://pypi.example.com/simple",
            "test",
            "0",
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={"PIP_INDEX_URL": "http://pypi.example.com/simple"},
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli_copes_with_equal_sign_in_value(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run-one",
            "--set-env",
            "DOUBLE_EQUAL=value_with=another_equal_sign",
            "test",
            "0",
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={
                        "DOUBLE_EQUAL": "value_with=another_equal_sign",
                    },
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_set_environment_variables_via_cli_ensure_merge_order(
        self, mock_get_host_architecture, mock_get_provider
    ):
        # env from CLI wins over env from configuration
        launcher = Mock(spec=launch)
        provider = makeLXDProvider(lxd_launcher=launcher)
        mock_get_provider.return_value = provider
        execute_run = launcher.return_value.execute_run
        execute_run.return_value = subprocess.CompletedProcess([], 0)
        config = dedent(
            """
            pipeline:
                - test

            jobs:
                test:
                    series: focal
                    architectures: amd64
                    run: tox
                    environment:
                        PIP_INDEX_URL: http://pypi.example.com/simple
            """
        )
        Path(".launchpad.yaml").write_text(config)

        result = self.run_command(
            "run-one",
            "--set-env",
            "PIP_INDEX_URL=http://local-pypi.example.com/simple",
            "test",
            "0",
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual(
            [
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/lpcraft/project"),
                    env={
                        "PIP_INDEX_URL": "http://local-pypi.example.com/simple"
                    },
                    stdout=ANY,
                    stderr=ANY,
                )
            ],
            execute_run.call_args_list,
        )
