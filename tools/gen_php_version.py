#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 core/version.py 生成 php/src/Version.php

为什么要生成而不是手写两份：
    版本号一旦有两个来源，就一定会出现「Python 版显示 1.2.7、PHP 版显示 1.2.5」
    这种自相矛盾。单一事实来源仍然是 core/version.py，PHP 侧的 Version.php
    只是它的只读投影，改了 Python 侧重新跑本脚本即可。

用法：
    python3 tools/gen_php_version.py
"""

import ast
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "core", "version.py")
DST = os.path.join(BASE, "php", "src", "Version.php")


def php_str(s: str) -> str:
    """PHP 单引号字符串字面量（单引号内只需转义 \\ 和 '）"""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def extract(src_text: str):
    version = re.search(r'VERSION\s*=\s*"([^"]+)"', src_text)
    if not version:
        raise SystemExit("在 core/version.py 里找不到 VERSION")

    # 用 ast 解析 CHANGELOG，比 eval 更安全（不执行任何代码）
    tree = ast.parse(src_text)
    changelog = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CHANGELOG":
                    changelog = ast.literal_eval(node.value)
    if changelog is None:
        raise SystemExit("在 core/version.py 里找不到 CHANGELOG 字面量")
    return version.group(1), changelog


def _const(text: str, name: str, resolved: dict | None = None):
    """
    从 core/version.py 里取顶层字符串常量。

    支持三种写法（core/version.py 里实际存在的）：
        NAME = "字面量"
        ISSUE_URL = REPO_URL + "/issues"      ← 常量拼接，需要 resolved 里已有 REPO_URL
    """
    m = re.search(rf'^{name}\s*=\s*(.+?)\s*$', text, re.M)
    if not m:
        return None
    expr = m.group(1).strip()

    # 纯字面量：整行就是一个双引号字符串
    if re.fullmatch(r'"[^"]*"', expr):
        return expr[1:-1]

    # 拼接表达式：把所有 "字面量" 与已知常量替换掉后拼接
    parts = re.findall(r'"([^"]*)"|([A-Z_][A-Z0-9_]*)', expr)
    if not parts:
        return None
    out = []
    for lit, ref in parts:
        if lit:
            out.append(lit)
        elif ref:
            if resolved is None or ref not in resolved:
                return None
            out.append(resolved[ref])
    # 只接受纯拼接：把「字面量」「常量名」「+」「空白」都抠掉后必须什么都不剩。
    # （早前用一个字符白名单判定，结果把字面量里的 "/" 也当成非法运算符，
    #   导致 ISSUE_URL = REPO_URL + "/issues" 被判为不可解析。）
    residual = re.sub(r'"[^"]*"', '', expr)
    residual = re.sub(r'\b[A-Z_][A-Z0-9_]*\b', '', residual)
    if residual.replace('+', '').strip() != '':
        return None
    return "".join(out)


def main() -> int:
    src_text = open(SRC, encoding="utf-8").read()
    version, changelog = extract(src_text)

    L = []
    L.append("<?php")
    L.append("/**")
    L.append(" * Version —— 版本号与更新日志")
    L.append(" *")
    L.append(" * ★ 本文件由 tools/gen_php_version.py 从 core/version.py 自动生成，请勿手改。")
    L.append(" *   单一事实来源仍是 core/version.py，避免两个版本号各说各话。")
    L.append(" *   重新生成：python3 tools/gen_php_version.py")
    L.append(" */")
    L.append("")
    L.append("declare(strict_types=1);")
    L.append("")
    L.append("final class Version")
    L.append("{")
    L.append(f"    public const VERSION = {php_str(version)};")

    # 其余元信息：与 core/version.py 顶部的常量一一对应，供 /api/version 与
    # /api/status 复用（前端「关于」卡片要显示仓库地址、issue 链接等）
    meta = {
        "APP_NAME":      _const(src_text, "APP_NAME"),
        "APP_NAME_CN":   _const(src_text, "APP_NAME_CN"),
        "REPO_URL":      _const(src_text, "REPO_URL"),
        "REPO_NAME":     _const(src_text, "REPO_NAME"),
        "ISSUE_URL":     None,
        "README_URL":    None,
    }
    # 第二遍：解析依赖其它常量的拼接（ISSUE_URL = REPO_URL + "/issues"）
    resolved = {k: v for k, v in meta.items() if v}
    meta["ISSUE_URL"]  = _const(src_text, "ISSUE_URL", resolved)
    resolved = {k: v for k, v in meta.items() if v}
    meta["README_URL"] = _const(src_text, "README_URL", resolved)
    for k, v in meta.items():
        if v is not None:
            L.append(f"    public const {k} = {php_str(v)};")

    L.append("")
    L.append("    /**")
    L.append("     * 与 Python 版 core/version.py 的 info() 逐字段对应。")
    L.append("     * 前端 /api/version 与 /api/status 都读这里的字段名。")
    L.append("     * 只包含 core/version.py 里确实存在的常量（缺的不编造）。")
    L.append("     * @return array<string,mixed>")
    L.append("     */")
    L.append("    public static function info(): array")
    L.append("    {")
    L.append("        return [")
    L.append("            'version'       => self::VERSION,")
    # info() 的字段名固定，值取自实际存在的常量；常量缺失时不输出该字段
    field_map = [
        ("name",       "APP_NAME"),
        ("name_cn",    "APP_NAME_CN"),
        ("repo",       "REPO_URL"),
        ("repo_name",  "REPO_NAME"),
        ("issue_url",  "ISSUE_URL"),
        ("readme_url", "README_URL"),
    ]
    for field, const in field_map:
        if meta.get(const) is not None:
            L.append(f"            '{field}'{' ' * max(0, 13 - len(field))}=> self::{const},")
    L.append("            'latest_notes'  => self::changelog()[0]['notes'] ?? [],")
    L.append("            'released'      => self::changelog()[0]['date'] ?? '',")
    L.append("        ];")
    L.append("    }")
    L.append("")
    L.append("    /** @return array<int,array{version:string,date:string,notes:string[]}> */")
    L.append("    public static function changelog(): array")
    L.append("    {")
    L.append("        return [")
    for e in changelog:
        L.append("            [")
        L.append(f"                'version' => {php_str(e.get('version', ''))},")
        L.append(f"                'date'    => {php_str(e.get('date', ''))},")
        L.append("                'notes'   => [")
        for n in e.get("notes", []) or []:
            L.append(f"                    {php_str(n)},")
        L.append("                ],")
        L.append("            ],")
    L.append("        ];")
    L.append("    }")
    L.append("}")
    L.append("")

    os.makedirs(os.path.dirname(DST), exist_ok=True)
    open(DST, "w", encoding="utf-8").write("\n".join(L))
    print(f"✅ {os.path.relpath(DST, BASE)}  ← {os.path.relpath(SRC, BASE)}")
    print(f"   版本 {version}，CHANGELOG {len(changelog)} 条")

    # 自我保护：生成后立刻用 php -l 校验
    import subprocess
    try:
        r = subprocess.run(["php", "-l", DST], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            print("❌ 生成的 PHP 语法有误：", r.stdout + r.stderr, file=sys.stderr)
            return 1
        print("   php -l 通过")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("   （本机没有 php，跳过语法校验）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
