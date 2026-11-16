import isolation  # noqa: F401
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_completion_reliability as fixture
from test_archive_reliability import SyntheticContactReader
import wechat_live as live
import wechat_cli as cli
class LiveSnapshotTests(unittest.TestCase):
    def test_counts_scope_cached_verification_and_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "contacts" / "primary" / "contact-synthetic"
            case = fixture.CompletionReliabilityTests()
            case.sync(case.args(archive), SyntheticContactReader)
            stage = root / "other.wechat-transaction" / "staged"
            stage.mkdir(parents=True)
            (stage / "manifest.json").write_bytes((archive / "manifest.json").read_bytes())
            live.write_snapshot(root, verify=True)
            first = live.load(root / "live.snapshot.json")
            artifact = next(a for a in live.load(root / "live.owner.json")["artifacts"] if a["kind"] == "archive")
            self.assertEqual(artifact["open"], dict(source="wechat.contacts", path="primary/contact-synthetic/context.md"))
            self.assertEqual(artifact["public"], "owner")
            self.assertNotIn("contact-synthetic", json.dumps(first))
            m = first["metrics"]
            self.assertEqual((m["archives"], m["integrity"]), (1, "1/1 份"))
            self.assertEqual(m["saved"] + m["missing"], m["media"])
            self.assertEqual(m["messages"], live.load(archive / "manifest.json")["messageCount"])
            self.assertNotIn('"account"', json.dumps(first))
            live.write_snapshot(root)
            self.assertEqual(first, live.load(root / "live.snapshot.json"))
            live.write_snapshot(root, public_copy=root / "public.json")
            before = (root / "public.json").stat().st_mtime_ns
            live.write_snapshot(root, public_copy=root / "public.json")
            self.assertEqual((before, False), ((root / "public.json").stat().st_mtime_ns, "archives" in live.load(root / "public.json")["metrics"]))
            with patch.object(cli, "_default_export_root", return_value=root):
                _, receipt = case.sync(case.args(archive), SyntheticContactReader)
            self.assertEqual(receipt["liveSnapshot"]["status"], "success")
            (archive / "context.md").write_text("changed", encoding="utf-8")
            live.write_snapshot(root)
            self.assertIsNone(live.load(root / "live.snapshot.json")["metrics"]["integrity"])
            self.assertIsNone(live.load(root / "live.snapshot.json")["verification_at"])
            new_archive = root / "contacts" / "secondary" / "contact-next"
            case.sync(case.args(new_archive), SyntheticContactReader)
            live.write_snapshot(root, verify=True)
            opens = [a["open"] for a in live.load(root / "live.owner.json")["artifacts"] if a["kind"] == "archive"]
            self.assertEqual(opens, [dict(source="wechat.contacts", path="secondary/contact-next/context.md")])
