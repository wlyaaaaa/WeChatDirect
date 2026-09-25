from __future__ import annotations

import isolation

import ast
import os
from pathlib import Path
import unittest

import wechat_cli


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_machine_settings_are_hidden(self) -> None:
        self.assertEqual(
            [name for name in os.environ if name.upper().startswith("WECHAT_DIRECT_")],
            [],
        )
        self.assertEqual(Path(os.environ["LOCALAPPDATA"]), isolation.LOCAL_APPDATA)
        self.assertFalse(wechat_cli.LOCAL_SETTINGS_PATH.exists())
        self.assertNotEqual(
            wechat_cli.LOCAL_SETTINGS_PATH.parent, Path(wechat_cli.__file__).parent
        )
        self.assertEqual({}, wechat_cli._local_settings())
        self.assertEqual(
            isolation.LOCAL_APPDATA / "WeChatDirect" / "accounts.json",
            wechat_cli._resolve_config_path(None),
        )

    def test_every_test_module_imports_isolation_first(self) -> None:
        missing = []
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                node
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                and not (
                    isinstance(node, ast.ImportFrom) and node.module == "__future__"
                )
            ]
            first = imports[0] if imports else None
            if not (
                isinstance(first, ast.Import)
                and [alias.name for alias in first.names] == ["isolation"]
            ):
                missing.append(path.name)
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
