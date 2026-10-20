import isolation  # noqa: F401

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools import smoke_runtime


class SmokeRuntimeTests(unittest.TestCase):
    def test_required_voice_default_and_explicit_interpreters(self):
        doctor = subprocess.CompletedProcess(
            [], 0, stdout=b'{"status":"success","configuration":{"status":"not_checked"}}'
        )
        for options, expected in (
            ([], ["py", "-3.11"]),
            (["--voice-python", "custom.exe"], ["custom.exe"]),
        ):
            with (
                self.subTest(options=options),
                patch.dict(os.environ, {"WECHAT_DIRECT_VOICE_PYTHON": ""}),
                patch.object(sys, "argv", ["smoke_runtime", "--require-voice", *options]),
                patch.object(smoke_runtime, "run", side_effect=[doctor, RuntimeError("encoding_reached")]) as run,
            ):
                with self.assertRaisesRegex(RuntimeError, "encoding_reached"):
                    smoke_runtime.main()
                self.assertEqual(run.call_args.args[0][:len(expected)], expected)
