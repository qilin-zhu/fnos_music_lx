"""`subscriptions_env.py`（订阅配置读写工具）测试。

覆盖：解析/命名/去重、shell 安全转义、保留注释与其它键、
原子写入与权限、以及 deploy.sh 依赖的 CLI 行为。

全部使用 tmp_path，不触碰真实 .env.lxsource.local。
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = _HERE
_TOOL = os.path.join(_SERVICE, "subscriptions_env.py")


def _load():
    spec = importlib.util.spec_from_file_location("subscriptions_env", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subscriptions_env"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load()


@pytest.fixture
def envfile(tmp_path):
    return str(tmp_path / ".env.lxsource.local")


BASE = """# 头部注释
LX_SUBSCRIPTIONS='ikun|https://c.wwwweb.top/script/lxmusic?key=ABC'
LXSOURCE_REFRESH_S=3600
# 行内注释
LXSOURCE_PORT=8774
"""


class TestParseEntries:
    def test_explicit_name(self):
        assert S.parse_entries("a|https://x/1.js") == [("a", "https://x/1.js")]

    def test_auto_name_from_url(self):
        got = S.parse_entries("https://a.com/path/src.js")
        assert len(got) == 1
        assert got[0][1] == "https://a.com/path/src.js"
        assert got[0][0]  # 有自动命名

    def test_auto_name_from_file_url(self):
        assert S.parse_entries("file:///opt/lx-src/local.js") == [("local", "file:///opt/lx-src/local.js")]

    def test_multiple_comma_separated(self):
        got = S.parse_entries("a|https://1.js,b|https://2.js")
        assert [n for n, _ in got] == ["a", "b"]

    def test_newline_separated(self):
        got = S.parse_entries("a|https://1.js\nb|https://2.js")
        assert [n for n, _ in got] == ["a", "b"]

    def test_dedupes_names(self):
        got = S.parse_entries("a|https://1.js,a|https://2.js")
        assert [n for n, _ in got] == ["a", "a_2"]

    def test_empty(self):
        assert S.parse_entries("") == []
        assert S.parse_entries(None) == []
        assert S.parse_entries(" , , ") == []

    def test_missing_url_dropped(self):
        assert S.parse_entries("a|") == []


class TestQuoting:
    def test_quote_escapes_single_quote(self):
        q = S.quote("a'b")
        assert q.startswith("'") and q.endswith("'")
        assert "'\\''" in q

    def test_unquote_roundtrip(self):
        for v in ["simple", "a'b", "https://x/y?key=z", "a|b"]:
            assert S.unquote(S.quote(v)) == v

    def test_redact_hides_secrets(self):
        out = S.redact("https://x/s.js?key=SECRET&token=TOK")
        assert "SECRET" not in out and "TOK" not in out
        assert "key=***" in out and "token=***" in out

    def test_redact_keeps_plain(self):
        assert S.redact("https://x/plain.js") == "https://x/plain.js"


class TestReadWrite:
    def test_read_existing(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        assert S.read_subscriptions(envfile) == [("ikun", "https://c.wwwweb.top/script/lxmusic?key=ABC")]

    def test_missing_file_returns_empty(self, envfile):
        assert S.read_subscriptions(envfile) == []

    def test_write_preserves_comments_and_other_keys(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        S.write_subscriptions(envfile, [("new", "https://new.js")])
        text = open(envfile, encoding="utf-8").read()
        assert "# 头部注释" in text
        assert "# 行内注释" in text
        assert "LXSOURCE_REFRESH_S=3600" in text
        assert "LXSOURCE_PORT=8774" in text
        assert "new|https://new.js" in text
        assert "ikun|" not in text  # 旧值被替换

    def test_write_then_read_roundtrip(self, envfile):
        S.write_subscriptions(envfile, [("a", "https://1.js"), ("b", "https://2.js")])
        assert S.read_subscriptions(envfile) == [("a", "https://1.js"), ("b", "https://2.js")]

    def test_write_appends_when_missing(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write("LXSOURCE_PORT=8774\n")
        S.write_subscriptions(envfile, [("a", "https://1.js")])
        text = open(envfile, encoding="utf-8").read()
        assert "LXSOURCE_PORT=8774" in text
        assert "a|https://1.js" in text
        assert S.read_subscriptions(envfile) == [("a", "https://1.js")]

    def test_write_empty_clears(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        S.write_subscriptions(envfile, [])
        assert S.read_subscriptions(envfile) == []

    def test_duplicate_key_collapsed(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write("LX_SUBSCRIPTIONS='a|https://1.js'\nLX_SUBSCRIPTIONS='b|https://2.js'\n")
        S.write_subscriptions(envfile, [("c", "https://3.js")])
        text = open(envfile, encoding="utf-8").read()
        assert text.count("LX_SUBSCRIPTIONS=") == 1

    def test_write_sets_0600(self, envfile):
        S.write_subscriptions(envfile, [("a", "https://1.js")])
        mode = stat.S_IMODE(os.stat(envfile).st_mode)
        assert mode == 0o600, f"期望 0600，实际 {oct(mode)}"

    def test_write_is_atomic_no_temp_left(self, envfile):
        S.write_subscriptions(envfile, [("a", "https://1.js")])
        target = os.path.basename(envfile)
        left = [
            p for p in os.listdir(os.path.dirname(envfile))
            if p.startswith(".env.lxsource.") and p != target
        ]
        assert left == [], f"残留临时文件: {left}"

    def test_single_quote_in_url_is_shell_safe(self, envfile):
        """写入的文件必须能被 bash source 且取回原值。"""
        url = "https://x/a'b.js?key=K"
        S.write_subscriptions(envfile, [("q", url)])
        out = subprocess.run(
            ["bash", "-c", f'set -a; . "{envfile}"; printf "%s" "$LX_SUBSCRIPTIONS"'],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout == f"q|{url}"


class TestCli:
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, _TOOL, *args],
            capture_output=True, text=True,
        )

    def test_list_empty(self, envfile):
        r = self._run("list", "--file", envfile)
        assert r.returncode == 0
        assert "未配置订阅" in r.stdout

    def test_list_redacts_secret(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("list", "--file", envfile)
        assert r.returncode == 0
        assert "ABC" not in r.stdout
        assert "key=***" in r.stdout

    def test_add(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("add", "--file", envfile, "--entry", "x|https://x.js")
        assert r.returncode == 0
        names = [n for n, _ in S.read_subscriptions(envfile)]
        assert names == ["ikun", "x"]

    def test_add_duplicate_fails(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("add", "--file", envfile, "--entry", "ikun|https://dup.js")
        assert r.returncode != 0
        assert "已存在" in (r.stderr + r.stdout)

    def test_remove(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("remove", "--file", envfile, "--name", "ikun")
        assert r.returncode == 0
        assert S.read_subscriptions(envfile) == []

    def test_remove_missing_fails(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("remove", "--file", envfile, "--name", "nope")
        assert r.returncode != 0
        assert "未找到" in (r.stderr + r.stdout)

    def test_set_replaces_all(self, envfile):
        with open(envfile, "w", encoding="utf-8") as f:
            f.write(BASE)
        r = self._run("set", "--file", envfile, "--value", "a|https://1.js,b|https://2.js")
        assert r.returncode == 0
        assert [n for n, _ in S.read_subscriptions(envfile)] == ["a", "b"]

    def test_add_without_entry_fails(self, envfile):
        r = self._run("add", "--file", envfile)
        assert r.returncode == 2

    def test_set_without_value_fails(self, envfile):
        r = self._run("set", "--file", envfile)
        assert r.returncode == 2
