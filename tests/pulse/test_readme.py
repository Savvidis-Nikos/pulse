"""README.md must match the code. When this fails after a change to Pulse, update README.md (not this test).

Checks every `python -m <module> ...` line (module, subcommand and flags exist), every repo path the README names,
the number of CEO KPIs and the number of tests. Static only: nothing is imported or run.
"""
from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
PACKAGES = ("pulse", "marketplace", "argus")
CMD = re.compile(r"python(?:\.exe)? -m ([\w.]+)(.*)")


def _module_file(mod: str) -> Path | None:
    base = ROOT.joinpath(*mod.split("."))
    for p in (base / "__main__.py", base.with_suffix(".py")):
        if p.exists():
            return p
    return None


def _subcommands(src: str) -> set[str]:
    names = set(re.findall(r'add_parser\("([\w-]+)"', src))
    for group in re.findall(r"for \w+ in \(([^)]*)\):\s*\n\s*\w+ = \w+\.add_parser\(\w+\)", src):
        names |= set(re.findall(r'"([\w-]+)"', group))
    return names


def _code_lines() -> list[str]:
    blocks = re.findall(r"```[a-z]*\n(.*?)```", README, flags=re.S)
    return [line.split(" #")[0].strip() for b in blocks for line in b.splitlines() if line.strip()]


def _commands() -> list[tuple[str, list[str]]]:
    out = []
    for line in _code_lines():
        m = CMD.search(line)
        if m and m.group(1).split(".")[0] in PACKAGES:
            out.append((m.group(1), shlex.split(m.group(2))))
    return out


def test_readme_commands_exist():
    cmds = _commands()
    assert cmds, "no python -m commands found in README.md"
    for mod, args in cmds:
        f = _module_file(mod)
        assert f, f"README runs `python -m {mod}` but there is no such module"
        src = f.read_text(encoding="utf-8")
        for a in args:
            if a.startswith("--"):
                assert f'"{a}"' in src, f"README uses `{mod} {a}` but {f.relative_to(ROOT)} has no such flag"
        subs = _subcommands(src)
        if subs:
            first = next((a for a in args if not a.startswith("-")), None)
            assert first in subs, f"README uses `{mod} {first}` but the subcommands are {sorted(subs)}"


def test_readme_paths_exist():
    tokens = re.findall(r"`([^`\s]+)`", README) + re.findall(r"\]\(([^)\s]+)\)", README)
    tokens += [t for line in _code_lines() for t in line.split()]
    for t in {t.rstrip("/.,") for t in tokens}:
        if t.split("/")[0] in PACKAGES + ("docs", "tests") and "/" in t or t in PACKAGES:
            assert (ROOT / t).exists(), f"README mentions {t}, which does not exist"


def test_readme_counts():
    kpis = yaml.safe_load((ROOT / "pulse" / "kpis.yaml").read_text(encoding="utf-8"))["kpis"]
    for n in re.findall(r"(\d+) CEO KPIs", README):
        assert int(n) == len(kpis), f"README says {n} CEO KPIs, pulse/kpis.yaml has {len(kpis)}"
    for n in re.findall(r"(\d+) tests\b", README):
        assert int(n) == _count_tests(), f"README says {n} tests, tests/pulse has {_count_tests()}"


def _count_tests() -> int:
    total = 0
    for f in (ROOT / "tests" / "pulse").glob("test_*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                n = 1
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "parametrize"
                            and len(d.args) > 1 and isinstance(d.args[1], (ast.List, ast.Tuple))):
                        n *= len(d.args[1].elts)
                total += n
    return total
