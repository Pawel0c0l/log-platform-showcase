#!/usr/bin/env python3
"""Isolated subprocess regression tests for the log-job-runner.sh argument contract.

Covers JSON handling and the forwarding of explicit runner options, against
BOTH maintained wrappers. The two share their whole argument-handling tail by
design, so a fix applied to one and not the other is itself the defect: the
release wrapper is what production executes, and the development wrapper is
what the cutover contract compares against.

NOT DONE ANYWHERE IN THIS FILE: a real job, an e-mail, an SMTP or IMAP
connection, a publisher call or any network access. `ops/runner.py` is never
executed -- a stub captures the argv it would have received -- and the option
contract itself is exercised as pure functions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_SOURCE = REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh"
RELEASE_WRAPPER_SOURCE = REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh"

#: The four Eco mailing modules, and one module that is not one of them.
ECO_MODULE = "jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications"
NON_ECO_MODULE = "jobs.workflow_b.job_stage_2_clean"


PYTHON_STUB = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

if sys.argv[1:2] == ["-c"]:
    Path(os.environ["WRAPPER_RAW_CAPTURE"]).write_text(sys.argv[3], encoding="utf-8")
    print(json.dumps(json.loads(sys.argv[3]), ensure_ascii=False))
else:
    Path(os.environ["WRAPPER_EXEC_CAPTURE"]).write_text(
        json.dumps({"argv": sys.argv, "cwd": os.getcwd()}, ensure_ascii=False),
        encoding="utf-8",
    )
"""


class LogJobRunnerWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        (self.base_dir / ".venv/bin").mkdir(parents=True)
        (self.base_dir / "jobs/config").mkdir(parents=True)

        self.wrapper = self.base_dir / "log-job-runner.sh"
        wrapper_text = WRAPPER_SOURCE.read_text(encoding="utf-8")
        expected = 'BASE_DIR="/opt/log-platform"'
        self.assertEqual(wrapper_text.count(expected), 1)
        self.wrapper.write_text(
            wrapper_text.replace(expected, f'BASE_DIR="{self.base_dir}"'),
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)

        self.python_stub = self.base_dir / ".venv/bin/python"
        self.python_stub.write_text(PYTHON_STUB, encoding="utf-8")
        self.python_stub.chmod(0o755)

        self.raw_capture = self.base_dir / "raw.txt"
        self.exec_capture = self.base_dir / "exec.json"
        self.env = os.environ.copy()
        self.env["WRAPPER_RAW_CAPTURE"] = str(self.raw_capture)
        self.env["WRAPPER_EXEC_CAPTURE"] = str(self.exec_capture)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def invoke(self, params: str | None, *options: str,
               module: str = "jobs.test.capture",
               trailing: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        args = [str(self.wrapper), module]
        if params is not None:
            args.append(params)
        args.extend(options)
        args.extend(trailing)
        return subprocess.run(
            args,
            cwd="/",
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )

    def assert_successful_handoff(self, supplied: str | None, expected_raw: str,
                                  *options: str) -> list:
        result = self.invoke(supplied, *options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.raw_capture.read_text(encoding="utf-8"), expected_raw)

        handoff = json.loads(self.exec_capture.read_text(encoding="utf-8"))
        argv = handoff["argv"]
        self.assertEqual(argv[1], "ops/run_with_environment_identity.py")
        self.assertEqual(argv[2], "--")
        self.assertEqual(argv[3], str(self.python_stub))
        self.assertEqual(argv[4], "ops/runner.py")
        self.assertEqual(argv[5], "jobs.test.capture")
        self.assertEqual(json.loads(argv[6]), json.loads(expected_raw))
        # The JSON is exactly ONE argument, and every explicit option follows it
        # verbatim and in order. Anything else here is silent argument loss.
        self.assertEqual(argv[7:], list(options))
        self.assertEqual(handoff["cwd"], str(self.base_dir))
        return argv

    def test_omitted_and_empty_params_default_to_empty_object(self) -> None:
        for supplied in (None, ""):
            with self.subTest(supplied=supplied):
                self.assert_successful_handoff(supplied, "{}")
                self.raw_capture.unlink()
                self.exec_capture.unlink()

    def test_objects_are_preserved_before_canonicalization(self) -> None:
        values = (
            "{}",
            '{"dry_run":true,"batch_size":5000}',
            '{"outer":{"items":[{"enabled":true},{"value":null}]}}',
            '{"message":"Zażółć gęślą jaźń — 東京"}',
        )
        for value in values:
            with self.subTest(value=value):
                self.assert_successful_handoff(value, value)
                self.assertFalse(self.raw_capture.read_text(encoding="utf-8").endswith("}}}"))
                self.raw_capture.unlink()
                self.exec_capture.unlink()

    def test_all_json_value_types_reach_the_existing_runner_path(self) -> None:
        for value in ('[1,{"nested":true}]', '"text"', "42", "true", "false", "null"):
            with self.subTest(value=value):
                self.assert_successful_handoff(value, value)
                self.raw_capture.unlink()
                self.exec_capture.unlink()

    def test_malformed_json_is_rejected_before_handoff(self) -> None:
        result = self.invoke('{"dry_run":true}}')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.raw_capture.read_text(encoding="utf-8"),
            '{"dry_run":true}}',
        )
        self.assertFalse(self.exec_capture.exists())

    # --- explicit runner options -------------------------------------------
    # The regression these close: the final exec used to end at "${PARAMS_JSON}",
    # so `log-job-runner.sh <module> '<json>' --with-dashboard` produced a run
    # that silently behaved as if the operator had never asked.

    def test_an_explicit_option_reaches_the_runner_argv_boundary(self) -> None:
        argv = self.assert_successful_handoff(
            '{"limit":1}', '{"limit":1}', "--with-dashboard")
        self.assertIn("--with-dashboard", argv)
        self.assertEqual(argv[-1], "--with-dashboard")

    def test_an_option_before_the_json_is_equally_preserved(self) -> None:
        """Options may appear anywhere; `ops/runner.py` splits them the same way."""
        result = self.invoke(None, "--with-dashboard", '{"limit":1}')
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.exec_capture.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(json.loads(argv[6]), {"limit": 1})
        self.assertEqual(argv[7:], ["--with-dashboard"])

    def test_an_option_with_no_json_still_defaults_the_params(self) -> None:
        result = self.invoke(None, "--with-dashboard")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.exec_capture.read_text(encoding="utf-8"))["argv"]
        self.assertEqual(json.loads(argv[6]), {})
        self.assertEqual(argv[7:], ["--with-dashboard"])

    def test_several_options_are_all_forwarded_in_order(self) -> None:
        """The wrapper keeps no allowlist, so it must not lose an option it does
        not recognise either -- refusing an unknown flag is the runner's job."""
        argv = self.assert_successful_handoff(
            "{}", "{}", "--with-dashboard", "--some-future-option")
        self.assertEqual(argv[7:], ["--with-dashboard", "--some-future-option"])

    def test_an_invocation_without_options_is_byte_for_byte_unchanged(self) -> None:
        argv = self.assert_successful_handoff('{"a":1}', '{"a":1}')
        self.assertEqual(len(argv), 7)
        self.assertEqual(argv[7:], [])

    def test_a_second_positional_argument_is_refused_not_dropped(self) -> None:
        """Fail closed. Silent argument loss is the defect being removed, so an
        argument the contract has no place for must stop the run, not vanish."""
        result = self.invoke('{"a":1}', trailing=("unexpected",))
        self.assertEqual(result.returncode, 2)
        self.assertIn("unexpected extra positional argument", result.stderr)
        self.assertFalse(self.exec_capture.exists())

    def test_an_option_is_never_parsed_as_the_json_parameter(self) -> None:
        result = self.invoke(None, "--with-dashboard")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.raw_capture.read_text(encoding="utf-8"), "{}")

    def test_shell_metacharacters_remain_data(self) -> None:
        marker = self.base_dir / "command-injection-marker"
        value = json.dumps(
            {
                "value": (
                    f"$(touch {marker}); `touch {marker}`; "
                    f"'; touch {marker}; # \" & | < >"
                )
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assert_successful_handoff(value, value)
        self.assertFalse(marker.exists())


class ReleaseWrapperTests(LogJobRunnerWrapperTests):
    """The same argument contract, against the wrapper production actually runs.

    The release wrapper resolves BASE_DIR through `<release root>/current`, so a
    disposable release root is built here rather than a plain directory. Nothing
    outside the temporary directory is touched: no real release root, no
    installed wrapper, no pointer.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name).resolve()
        self.release_root = root / "release-root"
        release_id = "0123456789ab"
        self.base_dir = self.release_root / "releases" / release_id
        (self.base_dir / ".venv/bin").mkdir(parents=True)
        (self.base_dir / "jobs/config").mkdir(parents=True)
        (self.base_dir / "ops").mkdir(parents=True)
        (self.base_dir / "ops/runner.py").write_text("", encoding="utf-8")
        (self.base_dir / ".env").write_text("", encoding="utf-8")
        (self.release_root / "current").symlink_to(Path("releases") / release_id)

        self.wrapper = root / "log-job-runner.sh"
        wrapper_text = RELEASE_WRAPPER_SOURCE.read_text(encoding="utf-8")
        expected = ('RELEASE_ROOT="/home/logplatform/ops/'
                    'log-platform-release"')
        self.assertEqual(wrapper_text.count(expected), 1)
        self.wrapper.write_text(
            wrapper_text.replace(expected, f'RELEASE_ROOT="{self.release_root}"'),
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)

        self.python_stub = self.base_dir / ".venv/bin/python"
        self.python_stub.write_text(PYTHON_STUB, encoding="utf-8")
        self.python_stub.chmod(0o755)

        self.raw_capture = root / "raw.txt"
        self.exec_capture = root / "exec.json"
        self.env = os.environ.copy()
        self.env["WRAPPER_RAW_CAPTURE"] = str(self.raw_capture)
        self.env["WRAPPER_EXEC_CAPTURE"] = str(self.exec_capture)

    def test_it_still_executes_only_a_release(self) -> None:
        """The forwarding fix must not have loosened the boundary assertions."""
        (self.release_root / "current").unlink()
        (self.release_root / "current").symlink_to(Path("..") / "elsewhere")
        result = self.invoke("{}")
        self.assertEqual(result.returncode, 3)
        self.assertIn("RELEASE_POINTER_INVALID", result.stderr)
        self.assertFalse(self.exec_capture.exists())


class WrapperParityTests(unittest.TestCase):
    """Both wrappers must keep the SAME argument-handling tail.

    They are deliberately identical from the usage line onwards so the cutover
    diff stays reviewable. A forwarding fix that landed in only one of them
    would leave the other silently dropping operator options.
    """

    MARKER = 'USAGE="Usage:'

    def test_the_argument_handling_tail_is_byte_identical(self) -> None:
        dev = WRAPPER_SOURCE.read_text(encoding="utf-8")
        rel = RELEASE_WRAPPER_SOURCE.read_text(encoding="utf-8")
        self.assertIn(self.MARKER, dev)
        self.assertIn(self.MARKER, rel)
        self.assertEqual(dev[dev.index(self.MARKER):], rel[rel.index(self.MARKER):])

    def test_neither_wrapper_keeps_its_own_option_allowlist(self) -> None:
        """A second allowlist would drift from `ops/runner.py`, which owns the
        contract. The wrapper classifies `--*` and forwards; it never judges."""
        for source in (WRAPPER_SOURCE, RELEASE_WRAPPER_SOURCE):
            with self.subTest(wrapper=source.name):
                text = source.read_text(encoding="utf-8")
                tail = text[text.index(self.MARKER):]
                self.assertNotIn("--with-dashboard", tail)

    def test_no_wrapper_infers_the_flag_from_anything(self) -> None:
        """Dashboard enablement stays an explicit operator action."""
        for source in (WRAPPER_SOURCE, RELEASE_WRAPPER_SOURCE):
            with self.subTest(wrapper=source.name):
                text = source.read_text(encoding="utf-8")
                self.assertNotIn("with_dashboard", text)


class RunnerOptionContractTests(unittest.TestCase):
    """`ops/runner.py` remains the single authority. Pure functions only."""

    @classmethod
    def setUpClass(cls) -> None:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        import ops.runner as runner
        cls.runner = runner

    def test_an_unknown_option_is_refused_not_ignored(self) -> None:
        with self.assertRaises(self.runner.RunnerUsageError) as caught:
            self.runner._apply_options(ECO_MODULE, {}, ["--with-dashboards"])
        self.assertIn("unknown option(s)", str(caught.exception))

    def test_the_flag_is_refused_for_a_non_eco_module(self) -> None:
        with self.assertRaises(self.runner.RunnerUsageError) as caught:
            self.runner._apply_options(NON_ECO_MODULE, {}, ["--with-dashboard"])
        self.assertIn("applies only to the Eco Driving mailing", str(caught.exception))

    def test_the_flag_sets_the_param_for_an_eco_module(self) -> None:
        self.assertEqual(
            self.runner._apply_options(ECO_MODULE, {}, ["--with-dashboard"]),
            {"with_dashboard": True})

    def test_no_options_means_no_param(self) -> None:
        self.assertEqual(self.runner._apply_options(ECO_MODULE, {}, []), {})

    def test_the_wrapper_argv_shape_splits_as_the_runner_expects(self) -> None:
        """The exact argv the fixed wrapper hands over, through the real split."""
        positional, options = self.runner._split_options(
            [ECO_MODULE, '{"limit":1}', "--with-dashboard"])
        self.assertEqual(positional, [ECO_MODULE, '{"limit":1}'])
        self.assertEqual(options, ["--with-dashboard"])
        self.assertEqual(
            self.runner._apply_options(positional[0], {"limit": 1}, options),
            {"limit": 1, "with_dashboard": True})


class ScheduledInvocationSurfaceTests(unittest.TestCase):
    """Nothing automatic may acquire the flag. Static inspection only."""

    def test_no_unit_timer_or_script_opts_in(self) -> None:
        scanned = 0
        for directory, patterns in (("ops/systemd", ("*.service", "*.timer", "*.sh")),
                                    ("ops/systemd/proposed", ("*",)),
                                    ("scripts", ("*.py", "*.sh"))):
            base = REPO_ROOT / directory
            if not base.exists():
                continue
            for pattern in patterns:
                for path in base.glob(pattern):
                    if not path.is_file():
                        continue
                    scanned += 1
                    text = path.read_text(encoding="utf-8", errors="replace")
                    with self.subTest(path=str(path)):
                        self.assertNotIn("--with-dashboard", text)
                        self.assertNotIn("with_dashboard", text)
        self.assertGreater(scanned, 0)

    def test_the_dispatcher_declares_the_eco_mailing_datasets_without_params(self) -> None:
        dispatcher = (REPO_ROOT / "jobs/api/telematics/dispatcher.py").read_text(
            encoding="utf-8")
        self.assertNotIn("with-dashboard", dispatcher)
        self.assertNotIn("with_dashboard", dispatcher)
        self.assertIn('"eco_person_driving_weekly_email_notifications": {},', dispatcher)
        self.assertIn('"eco_person_driving_monthly_email_notifications": {},', dispatcher)


if __name__ == "__main__":
    unittest.main()
