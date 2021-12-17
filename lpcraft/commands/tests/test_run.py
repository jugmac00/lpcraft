# Copyright 2021 Canonical Ltd.  This software is licensed under the
# GNU General Public License version 3 (see the file LICENSE).

import json
import os
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import Any, AnyStr, Dict, List, Optional
from unittest.mock import ANY, Mock, call, patch

from craft_providers.lxd import LXC, launch
from fixtures import TempDir
from testtools.matchers import MatchesStructure

from lpcraft.commands.tests import CommandBaseTestCase
from lpcraft.errors import CommandError, YAMLError
from lpcraft.providers._lxd import LXDProvider, _LXDLauncher
from lpcraft.providers.tests import FakeLXDInstaller


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
        **kwargs: Any
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

    def makeLXDProvider(
        self,
        is_ready: bool = True,
        lxd_launcher: Optional[_LXDLauncher] = None,
    ) -> LXDProvider:
        lxc = Mock(spec=LXC)
        lxc.remote_list.return_value = {}
        lxd_installer = FakeLXDInstaller(is_ready=is_ready)
        if lxd_launcher is None:
            lxd_launcher = Mock(spec=launch)
        return LXDProvider(
            lxc=lxc,
            lxd_installer=lxd_installer,
            lxd_launcher=lxd_launcher,
        )


class TestRun(RunBaseTestCase):
    def test_missing_config_file(self):
        result = self.run_command("run")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    YAMLError("Couldn't find config file '.launchpad.yaml'")
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_lxd_not_ready(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = self.makeLXDProvider(is_ready=False)
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
        mock_get_provider.return_value = self.makeLXDProvider()
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                    cwd=Path("/root/project"),
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
        mock_get_provider.return_value = self.makeLXDProvider()
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
            cwd=Path("/root/project"),
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                    cwd=Path("/root/project"),
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
                    cwd=Path("/root/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_expands_matrix(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                    cwd=Path("/root/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/project"),
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
                    cwd=Path("/root/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_pass_in_environment_variables(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                    cwd=Path("/root/project"),
                    env={"TOX_SKIP_ENV": "^(?!lint-)"},
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
    def test_output_path_escapes_directly(
        self,
        mock_get_host_architecture,
        mock_get_provider,
        mock_get_project_path,
    ):
        target_path = Path(self.useFixture(TempDir()).path)
        launcher = Mock(spec=launch)
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
        Path("symlink.txt").symlink_to("../target.txt")

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1, errors=[CommandError("File not found", retcode=1)]
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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

        result = self.run_command("run", "--output", str(target_path))

        # The exact error message differs between Python 3.8 and 3.9, so
        # don't test it in detail, but make sure it includes the offending
        # path.
        self.assertEqual(1, result.exit_code)
        [error] = result.errors
        self.assertIn("/target", str(error))

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_install_snaps(
        self, mock_get_host_architecture, mock_get_provider
    ):
        launcher = Mock(spec=launch)
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                        "--channel=stable",
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
                        "--channel=stable",
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
                    cwd=Path("/root/project"),
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
                    cwd=Path("/root/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
                call(
                    ["bash", "--noprofile", "--norc", "-ec", "tox"],
                    cwd=Path("/root/project"),
                    env={},
                    stdout=ANY,
                    stderr=ANY,
                ),
            ],
            execute_run.call_args_list,
        )


class TestRunOne(RunBaseTestCase):
    def test_missing_config_file(self):
        result = self.run_command("run-one", "test", "0")

        self.assertThat(
            result,
            MatchesStructure.byEquality(
                exit_code=1,
                errors=[
                    YAMLError("Couldn't find config file '.launchpad.yaml'")
                ],
            ),
        )

    @patch("lpcraft.commands.run.get_provider")
    @patch("lpcraft.commands.run.get_host_architecture", return_value="amd64")
    def test_job_not_defined(
        self, mock_get_host_architecture, mock_get_provider
    ):
        mock_get_provider.return_value = self.makeLXDProvider()
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
        mock_get_provider.return_value = self.makeLXDProvider()
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
            cwd=Path("/root/project"),
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
        provider = self.makeLXDProvider(lxd_launcher=launcher)
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
            cwd=Path("/root/project"),
            env={},
            stdout=ANY,
            stderr=ANY,
        )
