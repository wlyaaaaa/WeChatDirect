"""Hide this machine's WeChatDirect settings from every test.

Every test module imports this first. Discovery imports all modules before
any test runs, and a single-module run still imports it, so no test can read
the checkout's ``.wechatdirect.local.json``, the real ``%LOCALAPPDATA%``, the
real plaintext cache under TEMP or a ``WECHAT_DIRECT_*`` variable. Tests that
exercise settings resolution patch the exact values they need.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile

import wechat_cli
import wechat_plaincache

ENVIRONMENT_PREFIX = "WECHAT_DIRECT_"

for _name in [
    name for name in os.environ if name.upper().startswith(ENVIRONMENT_PREFIX)
]:
    del os.environ[_name]

ROOT = Path(tempfile.mkdtemp(prefix="wechat-direct-tests-"))
atexit.register(shutil.rmtree, ROOT, True)
LOCAL_APPDATA = ROOT / "LocalAppData"
LOCAL_APPDATA.mkdir()
os.environ["LOCALAPPDATA"] = str(LOCAL_APPDATA)
# Never created, so local discovery settings are absent unless a test patches them.
wechat_cli.LOCAL_SETTINGS_PATH = ROOT / ".wechatdirect.local.json"
# The persistent plaintext cache defaults under the real TEMP; keep tests'
# synthetic snapshots inside this disposable root instead.
wechat_plaincache.DEFAULT_ROOT = ROOT / "plain-cache"
