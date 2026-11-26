#!/usr/bin/env python3
"""Skill entry point.

Resolves the Family CFO package without hard-coded developer paths:

1. an installed ``family_cfo`` package (recommended: ``pip install -e .``);
2. the repository ``src/`` directory three levels above this script;
3. ``$FAMILY_CFO_HOME`` pointing at a checkout.

This keeps ``python scripts/fcfo.py ...`` working both in the repo and after the
skill directory has been copied into ``~/.hermes/skills/family-cfo``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    try:
        import family_cfo  # noqa: F401
        return
    except ImportError:
        pass

    candidates: list[Path] = []
    env_home = os.environ.get("FAMILY_CFO_HOME")
    if env_home:
        candidates.append(Path(env_home).expanduser())
    # skills/family-cfo/scripts/fcfo.py -> repo root is parents[3]
    candidates.append(Path(__file__).resolve().parents[3])

    for candidate in candidates:
        src = candidate / "src"
        if (src / "family_cfo" / "__init__.py").exists():
            sys.path.insert(0, str(src))
            return
        if (candidate / "family_cfo" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            return

    sys.stderr.write(
        "找不到 family_cfo 包。请先安装：pip install -e .\n"
        "或设置 FAMILY_CFO_HOME 指向项目根目录。\n"
    )
    raise SystemExit(2)


def main() -> None:
    _bootstrap()
    from family_cfo.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
