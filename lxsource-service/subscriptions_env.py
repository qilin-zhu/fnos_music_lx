#!/usr/bin/env python3
"""`.env.lxsource.local` 订阅配置的安全读写工具。

供 deploy.sh 的订阅管理子命令使用，也可独立调用：

    python3 subscriptions_env.py list   [--file PATH]
    python3 subscriptions_env.py add    [--file PATH] --entry 'name|url'
    python3 subscriptions_env.py remove [--file PATH] --name NAME
    python3 subscriptions_env.py set    [--file PATH] --value 'a|url,b|url'

设计要点：
- **保留原文件其它内容**：注释、空行、其它键值一律原样保留，只改
  ``LX_SUBSCRIPTIONS`` 一行（缺失则追加）。
- **只做数据解析**：不执行 shell，不做变量展开。
- **脱敏输出**：``list`` 会隐藏 url 中的 key=/token= 等参数。
- 写入采用「临时文件 + os.replace」原子替换，并保持 0600 权限。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

KEY = "LX_SUBSCRIPTIONS"
DEFAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env.lxsource.local")

# shell 单引号转义：' -> '\''
_ESCAPED_QUOTE = "'\\''"
_SECRET_RE = re.compile(r"((?:key|token|apikey|api_key|password|secret)=)[^&,\s]+", re.I)


def unquote(raw: str) -> str:
    """还原 KEY=VALUE 中 VALUE 的引号（与 install.sh 的写法保持一致）。"""
    v = raw.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace(_ESCAPED_QUOTE, "'")
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def quote(value: str) -> str:
    """用单引号包裹，转义内部单引号（posix shell 可安全 source）。"""
    return "'" + str(value).replace("'", _ESCAPED_QUOTE) + "'"


def redact(value: str) -> str:
    """隐藏密钥参数，便于安全打印。"""
    return _SECRET_RE.sub(r"\1***", str(value or ""))


def parse_entries(raw: str) -> "list[tuple[str, str]]":
    """解析订阅串为 [(name, url)]。

    支持三种写法（与 lxsource-service 的 parseSubscriptions 一致）：
        https://x/a.js            -> 自动命名
        name|https://x/a.js       -> 显式命名
        name|file:///p/a.js       -> 本地脚本
    """
    out: list[tuple[str, str]] = []
    used: set[str] = set()
    for idx, item in enumerate(x.strip() for x in re.split(r"[\n,]+", raw or "")):
        if not item:
            continue
        name, url = "", item
        sep = item.find("|")
        if sep > 0:
            name, url = item[:sep].strip(), item[sep + 1:].strip()
        if not url:
            continue
        if not name:
            name = _auto_name(url, idx)
        unique, n = name, 2
        while unique in used:
            unique = f"{name}_{n}"
            n += 1
        used.add(unique)
        out.append((unique, url))
    return out


def _auto_name(url: str, idx: int) -> str:
    if url.startswith("file://"):
        base = os.path.basename(url[len("file://"):])
        return os.path.splitext(base)[0] or f"source{idx + 1}"
    m = re.match(r"^[a-z]+://([^/]+)(/.*)?$", url, re.I)
    if not m:
        return f"source{idx + 1}"
    host, path = m.group(1), (m.group(2) or "")
    stem = os.path.splitext(path)[0]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{host}{stem}") or f"source{idx + 1}"


def format_entries(entries: "list[tuple[str, str]]") -> str:
    return ",".join(f"{n}|{u}" for n, u in entries)


def read_lines(path: str) -> "list[str]":
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError as e:
        raise SystemExit(f"无法读取 {path}: {e}")


def read_subscriptions(path: str) -> "list[tuple[str, str]]":
    """读取当前订阅；文件不存在或无该键时返回空列表。"""
    for line in read_lines(path):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[7:]
        key, sep, value = s.partition("=")
        if sep and key.strip() == KEY:
            return parse_entries(unquote(value))
    return []


def write_subscriptions(path: str, entries: "list[tuple[str, str]]") -> None:
    """原子写回；只替换/追加 KEY 行，其它内容原样保留。"""
    value = format_entries(entries)
    new_line = f"{KEY}={quote(value)}" if value else f"{KEY}="

    lines = read_lines(path)
    out: list[str] = []
    replaced = False
    for line in lines:
        s = line.strip()
        probe = s[7:] if s.startswith("export ") else s
        key = probe.partition("=")[0].strip()
        if probe and "=" in probe and key == KEY:
            if not replaced:
                out.append(new_line)
                replaced = True
            # 重复键只保留第一个，避免歧义
            continue
        out.append(line)

    if not replaced:
        if out and out[-1].strip():
            out.append("")
        if not any(l.strip().startswith("#") and KEY in l for l in out):
            out.append("# ---- 订阅列表（由 deploy.sh 维护；多个用逗号分隔）----")
        out.append(new_line)

    data = "\n".join(out).rstrip("\n") + "\n"
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".env.lxsource.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ------------------------------------------------------------------ CLI
def _print_entries(entries: "list[tuple[str, str]]") -> None:
    if not entries:
        print("(未配置订阅)")
        return
    for i, (name, url) in enumerate(entries, 1):
        kind = "本地脚本" if url.startswith("file://") else "远程"
        print(f"  {i}. {name:16s} [{kind}] {redact(url)}")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="管理 .env.lxsource.local 的订阅列表")
    ap.add_argument("action", choices=["list", "add", "remove", "set"])
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--entry", help="add: name|url 或 url")
    ap.add_argument("--name", help="remove: 订阅名")
    ap.add_argument("--value", help="set: 完整订阅串")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.action == "list":
        _print_entries(read_subscriptions(args.file))
        return 0

    if args.action == "add":
        if not args.entry:
            print("add 需要 --entry", file=sys.stderr)
            return 2
        new = parse_entries(args.entry)
        if not new:
            print("--entry 解析为空", file=sys.stderr)
            return 2
        current = read_subscriptions(args.file)
        existing = {n for n, _ in current}
        if new[0][0] in existing:
            print(f"订阅名已存在：{new[0][0]}（改用 set 或先 remove）", file=sys.stderr)
            return 2
        current.extend(new)
        write_subscriptions(args.file, current)
        if not args.quiet:
            print(f"已添加 {new[0][0]}；当前共 {len(current)} 项")
        return 0

    if args.action == "remove":
        if not args.name:
            print("remove 需要 --name", file=sys.stderr)
            return 2
        current = read_subscriptions(args.file)
        kept = [(n, u) for n, u in current if n != args.name]
        if len(kept) == len(current):
            print(f"未找到订阅：{args.name}", file=sys.stderr)
            return 2
        write_subscriptions(args.file, kept)
        if not args.quiet:
            print(f"已删除 {args.name}；当前共 {len(kept)} 项")
        return 0

    # set
    if args.value is None:
        print("set 需要 --value", file=sys.stderr)
        return 2
    entries = parse_entries(args.value)
    write_subscriptions(args.file, entries)
    if not args.quiet:
        print(f"已设置 {len(entries)} 项订阅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
