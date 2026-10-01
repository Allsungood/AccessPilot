"""静态检查: 用了工具函数却没导入 —— 这类 bug 只在运行到那一行时才炸.

真实事故(2026-10-01): `process.py` 第 346 行调用了 `dim(...)`, 但那一行的
`from .util import ...` 里没有 `dim`。触发时机极其恶劣 —— 它在**开启系统代理
成功之后**才执行:

    ok(detail)                                    <- 代理已经设好了
    print(dim("    想恢复原状随时执行: ..."))      <- NameError!

结果是内核起来了、系统代理也写对了, 但 `process.start()` 抛异常, 调用方
(control.turn_on / 大圆钮)只能报一条 "name 'dim' is not defined"。用户看到的是
"打开了但报错", 而真正的原因藏在一行和功能毫无关系的提示语里。

这类 bug 单元测试很难覆盖(要真的启动内核才会走到), 所以这里用静态方式扫:
把每个模块里**所有**绑定过的名字收集起来, 再看有没有"调用了 util 的输出函数
但没绑定"的情况。绑定集合刻意收得很宽(含函数局部、参数、推导式变量), 宁可
漏报也不误报。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests  # noqa: E402,F401

PKG = Path(__file__).resolve().parent.parent / "accesspilot"

#: util.py 导出的彩色/格式化输出函数。它们是最容易被忘记导入的一类 ——
#: 因为分散在几十处 print 里, 而且只在特定分支才执行。
UTIL_HELPERS = frozenset({
    "dim", "bold", "green", "red", "yellow", "warn", "ok", "info", "err",
})

#: 内建/关键字, 不算未导入
BUILTINS = frozenset(dir(__builtins__)) | {"__name__", "__file__", "__doc__"}


def _bound_names(tree: ast.AST) -> set[str]:
    """收集模块里**所有**被绑定的名字(不区分作用域, 刻意保守)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                    bound.add(arg.arg)
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bound.update(node.names)
    return bound


class NoUnimportedHelpers(unittest.TestCase):
    def test_modules_do_not_use_unimported_helpers(self) -> None:
        problems: list[str] = []
        modules = sorted(PKG.rglob("*.py"))
        self.assertGreater(len(modules), 10, "没扫到模块, 路径可能不对")

        for path in modules:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as e:  # pragma: no cover
                problems.append(f"{path.name}: 语法错误 {e}")
                continue
            bound = _bound_names(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                        and node.id in UTIL_HELPERS
                        and node.id not in bound and node.id not in BUILTINS):
                    rel = path.relative_to(PKG.parent)
                    problems.append(f"{rel}:{node.lineno} 用了 {node.id}(...) 但没绑定")

        self.assertEqual(problems, [], "发现未导入就使用的工具函数:\n  " + "\n  ".join(problems))

    def test_the_checker_itself_would_catch_the_real_bug(self) -> None:
        """用真实事故的那段代码验证检查器确实能抓到, 免得它是个永远通过的空壳."""
        src = (
            "from .util import ok\n"
            "def f():\n"
            "    ok('代理已设好')\n"
            "    print(dim('想恢复原状随时执行'))\n"
        )
        tree = ast.parse(src)
        bound = _bound_names(tree)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        self.assertIn("dim", used & UTIL_HELPERS)
        self.assertNotIn("dim", bound, "dim 没被绑定 -> 检查器应该报出来")

    def test_checker_does_not_flag_properly_imported_helper(self) -> None:
        src = "from .util import dim\ndef f():\n    print(dim('x'))\n"
        tree = ast.parse(src)
        bound = _bound_names(tree)
        self.assertIn("dim", bound)


if __name__ == "__main__":
    unittest.main()
