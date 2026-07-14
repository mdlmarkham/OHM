"""Extract a cluster of methods from graph.py into a new handler mixin file.

Usage:
    python scripts/extract_cluster.py \
        --methods _get_stats,_get_status,... \
        --file reports_misc \
        --class ReportsHandlerMixin \
        --bases OhmHandlerBase \
        --imports "from ohm.server.handlers._base import OhmHandlerBase" \
        --docstring "Reports and misc handler mixin."

The --imports argument can be repeated to add multiple import lines.
The --bases argument specifies the base classes (comma-separated).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

GRAPH_PY = Path("src/ohm/server/handlers/graph.py")
HANDLERS_DIR = Path("src/ohm/server/handlers")


def find_method_boundaries(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Return {method_name: (start_line, end_line)} for all methods in graph.py.

    start_line and end_line are 0-indexed. Method occupies lines[start:end].
    end is exclusive (line before the next def, or end of file).
    """
    methods: dict[str, tuple[int, int]] = {}
    method_starts: list[tuple[int, str]] = []

    for i, line in enumerate(lines):
        m = re.match(r"^    def (_\w+)\(", line)
        if m:
            method_starts.append((i, m.group(1)))

    for idx, (start, name) in enumerate(method_starts):
        if idx + 1 < len(method_starts):
            end = method_starts[idx + 1][0]
        else:
            end = len(lines)
        methods[name] = (start, end)

    return methods


def extract_methods(lines: list[str], method_names: list[str], boundaries: dict[str, tuple[int, int]]) -> list[str]:
    """Return the verbatim text of the specified methods (list of lines, no trailing newlines)."""
    result: list[str] = []
    for name in method_names:
        if name not in boundaries:
            print(f"WARNING: method {name} not found in graph.py", file=sys.stderr)
            continue
        start, end = boundaries[name]
        chunk = lines[start:end]
        result.extend(chunk)
    return result


def remove_methods(lines: list[str], method_names: list[str], boundaries: dict[str, tuple[int, int]]) -> list[str]:
    """Return graph.py lines with the specified methods removed."""
    remove_ranges: list[tuple[int, int]] = []
    for name in method_names:
        if name not in boundaries:
            continue
        start, end = boundaries[name]
        remove_ranges.append((start, end))

    remove_ranges.sort()

    result: list[str] = []
    last = 0
    for start, end in remove_ranges:
        result.extend(lines[last:start])
        last = end
    result.extend(lines[last:])

    # Collapse 3+ consecutive blank lines into a single blank line
    cleaned: list[str] = []
    blank_count = 0
    for line in result:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    # Remove leading/trailing blank lines
    while cleaned and cleaned[0].strip() == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1].strip() == "":
        cleaned.pop()

    return cleaned


def build_mixin_file(
    class_name: str,
    bases: list[str],
    imports: list[str],
    docstring: str,
    methods_text: list[str],
    module_code: list[str] | None = None,
) -> str:
    """Build the content of the new mixin file."""
    parts: list[str] = []
    parts.append(f'"""{docstring}"""')
    parts.append("")
    parts.append("from __future__ import annotations")
    parts.append("")
    for imp in imports:
        if imp == "---blank---":
            parts.append("")
        else:
            parts.append(imp)
    if module_code:
        parts.append("")
        for line in module_code:
            parts.append(line)
    parts.append("")
    parts.append("")
    bases_str = ", ".join(bases)
    parts.append(f"class {class_name}({bases_str}):")
    parts.append(f'    """Handler mixin for {docstring.lower().rstrip(".")}."""')
    parts.append("")
    for line in methods_text:
        parts.append(line)

    return "\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--methods", required=True, help="Comma-separated method names")
    parser.add_argument("--file", required=True, help="Output filename (without .py)")
    parser.add_argument("--class", dest="class_name", required=True, help="Class name")
    parser.add_argument("--bases", required=True, help="Comma-separated base classes")
    parser.add_argument("--imports", action="append", default=[], help="Import lines (repeatable)")
    parser.add_argument("--module-code", action="append", default=[], help="Module-level code lines, e.g. logger setup (repeatable)")
    parser.add_argument("--docstring", required=True, help="Module/class docstring")
    args = parser.parse_args()

    method_names = [m.strip() for m in args.methods.split(",")]
    bases = [b.strip() for b in args.bases.split(",")]
    output_path = HANDLERS_DIR / f"{args.file}.py"

    lines = GRAPH_PY.read_text(encoding="utf-8").splitlines()
    boundaries = find_method_boundaries(lines)

    methods_text = extract_methods(lines, method_names, boundaries)
    new_graph_lines = remove_methods(lines, method_names, boundaries)

    mixin_content = build_mixin_file(
        class_name=args.class_name,
        bases=bases,
        imports=args.imports,
        docstring=args.docstring,
        methods_text=methods_text,
        module_code=args.module_code if args.module_code else None,
    )

    output_path.write_text(mixin_content, encoding="utf-8")
    GRAPH_PY.write_text("\n".join(new_graph_lines) + "\n", encoding="utf-8")

    print(f"Created {output_path} ({len(mixin_content.splitlines())} lines)")
    print(f"Updated {GRAPH_PY} ({len(new_graph_lines)} lines)")


if __name__ == "__main__":
    main()
