#!/usr/bin/env python3
"""
Compare env vars declared in .env.example vs render.yaml.

Every var in .env.example must appear in render.yaml, and vice versa
(except PYTHON_VERSION which only belongs in render.yaml).
"""

import re
import sys
from pathlib import Path


def parse_dotenv_example(path: Path) -> set[str]:
    """Return the set of active (non-commented) env var keys."""
    vars_: set[str] = set()
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", stripped)
        if m:
            vars_.add(m.group(1))
    return vars_


def parse_render_yaml(path: Path) -> set[str]:
    """Return the set of env var keys declared in render.yaml's envVars."""
    vars_: set[str] = set()
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*-\s+key:\s*([A-Z_][A-Z0-9_]*)", line)
        if m:
            vars_.add(m.group(1))
    return vars_


def main():
    repo_root = Path(__file__).resolve().parent.parent
    env_example = repo_root / ".env.example"
    render = repo_root / "render.yaml"

    if not env_example.exists():
        print(f"ERROR: {env_example} not found")
        sys.exit(1)
    if not render.exists():
        print(f"ERROR: {render} not found")
        sys.exit(1)

    env_vars = parse_dotenv_example(env_example)
    render_vars = parse_render_yaml(render)

    # PYTHON_VERSION is only expected in render.yaml, never in .env.example
    render_vars.discard("PYTHON_VERSION")

    only_in_env = env_vars - render_vars
    only_in_render = render_vars - env_vars

    exit_code = 0

    if only_in_env:
        print("ERROR: vars in .env.example but missing from render.yaml:")
        for v in sorted(only_in_env):
            print(f"  {v}")
        exit_code = 1

    if only_in_render:
        print("ERROR: vars in render.yaml but missing from .env.example:")
        for v in sorted(only_in_render):
            print(f"  {v}")
        exit_code = 1

    if exit_code == 0:
        print("OK: all env vars are in sync between .env.example and render.yaml")
        print(f"  ({len(env_vars)} vars in .env.example)")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
