import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_review.process_security import (
    executable_identity, run_git, validate_executable_identity,
)


class ProcessSecurityTests(unittest.TestCase):
    def test_git_ignores_path_and_uses_the_bound_system_binary(self):
        with patch.dict(os.environ, {"PATH": "/private/tmp/fake"}, clear=False), patch(
            "ai_review.process_security.subprocess.run"
        ) as invoked:
            invoked.return_value = subprocess.CompletedProcess([], 0, "", "")
            run_git(["--version"], check=False)
        self.assertEqual(invoked.call_args.args[0][0], "/usr/bin/git")
        self.assertEqual(
            invoked.call_args.kwargs["env"]["PATH"],
            "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        )

    def test_identity_replacement_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as raw:
            executable = Path(raw) / "runner"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            identity = executable_identity(executable)
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_executable_identity(identity)


if __name__ == "__main__":
    unittest.main()
