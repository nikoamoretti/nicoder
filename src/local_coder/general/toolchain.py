"""Make the Vite preview compile the TypeScript Bonsai writes."""
from __future__ import annotations

import json
from pathlib import Path

SWC_PACKAGE = "@vitejs/plugin-react-swc"
SWC_VERSION = "^4.0.1"
_BABEL_REACT = "@vitejs/plugin-react"


def ensure_ts_preview(root: Path) -> list[str]:
    """Switch a Babel React plugin to SWC so .tsx generics compile.

    Returns relative paths that were written. No-op when SWC is already set.
    """
    root = root.expanduser()
    written: list[str] = []
    package_path = root / "package.json"
    if not package_path.is_file():
        return written
    try:
        data = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return written
    if not isinstance(data, dict):
        return written
    deps = data.get("devDependencies")
    if not isinstance(deps, dict):
        deps = {}
        data["devDependencies"] = deps
    if SWC_PACKAGE not in deps and _BABEL_REACT in deps:
        deps.pop(_BABEL_REACT, None)
        deps[SWC_PACKAGE] = SWC_VERSION
        package_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        written.append("package.json")
    vite_path = root / "vite.config.ts"
    if vite_path.is_file():
        try:
            text = vite_path.read_text(encoding="utf-8")
        except OSError:
            return written
        if "plugin-react-swc" not in text and "plugin-react" in text:
            vite_path.write_text(
                text.replace(_BABEL_REACT, SWC_PACKAGE),
                encoding="utf-8",
            )
            written.append("vite.config.ts")
    return written
