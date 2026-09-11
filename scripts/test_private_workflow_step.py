"""Regression coverage for the public-workflow privacy boundary."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WRAPPER = Path(__file__).with_name("private_workflow_step.py")
ROOT = WRAPPER.parent.parent
PRIVATE = "PRIVATE_FIXTURE_CUSTOMER_987654321"


class PrivateWorkflowTests(unittest.TestCase):
    def execute(self, body):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            script = base / "step.sh"
            script.write_text(body)
            env = dict(os.environ, GITHUB_STEP_SUMMARY=str(base / "summary"),
                       GITHUB_ENV=str(base / "env"), GITHUB_OUTPUT=str(base / "output"))
            result = subprocess.run([sys.executable, str(WRAPPER), str(script)],
                                    env=env, capture_output=True, text=True)
            files = {name: (base / name).read_text() if (base / name).exists() else ""
                     for name in ("summary", "env", "output")}
            self.assertNotIn(PRIVATE, result.stdout + result.stderr + files["summary"])
            return result, files

    def test_stdout_stderr_subprocess_and_summary_never_escape(self):
        result, files = self.execute(
            f"echo {PRIVATE}\necho {PRIVATE} >&2\n"
            f"bash -c 'echo {PRIVATE}'\necho {PRIVATE} >> \"$GITHUB_STEP_SUMMARY\"\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Private step passed.", result.stdout)
        self.assertEqual(files["summary"], "")

    def test_exception_and_workflow_annotation_never_escape(self):
        result, _ = self.execute(
            f"echo '::error::{PRIVATE}'\n"
            f"{sys.executable} -c \"raise ValueError('{PRIVATE}')\"\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Private step failed", result.stdout)

    def test_fail_fast_is_preserved(self):
        result, files = self.execute('exit 23\necho unsafe >> "$GITHUB_OUTPUT"\n')
        self.assertEqual(result.returncode, 23)
        self.assertEqual(files["output"], "")

    def test_pipeline_failure_is_preserved(self):
        result, files = self.execute('false | true\necho unsafe >> "$GITHUB_OUTPUT"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(files["output"], "")

    def test_control_channels_and_command_substitution_still_work(self):
        result, files = self.execute(
            'state="$(printf running)"\nprintf "state=%s\\n" "$state" >> "$GITHUB_OUTPUT"\n'
            'echo "OTOMY_WORKING_SET_STATE=/tmp/fixture" >> "$GITHUB_ENV"\n')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(files["output"], "state=running\n")
        self.assertIn("OTOMY_WORKING_SET_STATE=", files["env"])

    def test_shell_trace_cannot_escape(self):
        result, _ = self.execute(f"set -x\necho {PRIVATE}\n")
        self.assertEqual(result.returncode, 0)

    def test_every_workflow_uses_private_shell_after_checkout(self):
        import yaml
        for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
            workflow = yaml.safe_load(path.read_text())
            self.assertEqual(workflow["defaults"]["run"]["shell"],
                             "python3 scripts/private_workflow_step.py {0}", path.name)
            for job in workflow["jobs"].values():
                self.assertNotIn("defaults", job, path.name)
                checked_out = False
                for step in job["steps"]:
                    if str(step.get("uses", "")).startswith("actions/checkout@"):
                        checked_out = True
                    if "run" in step:
                        self.assertTrue(checked_out, path.name)
                        self.assertNotIn("shell", step, path.name)
                    self.assertFalse(str(step.get("uses", "")).startswith("actions/upload-artifact@"), path.name)


if __name__ == "__main__":
    unittest.main()
