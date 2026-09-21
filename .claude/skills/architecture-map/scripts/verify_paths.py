#!/usr/bin/env python3
"""
Verifies that every path referenced in a "Path" column of a markdown table
actually exists, relative to a given repo root.

The architecture-map templates deliberately put every path reference inside
a markdown table with a column header literally named "Path" (case-
insensitive) so it can be checked mechanically here, instead of trying to
regex-extract paths out of free prose.

Usage: verify_paths.py <repo-root> <file1.md> [file2.md ...]

Prints one line per missing path: "MISSING: <path> (in <file>)"
Exits 0 if every referenced path exists, 1 if any are missing.
"""
import sys
import os
import re


def find_path_column_tables(lines):
    """Yield (path_column_index, row_lines) for each markdown table that has
    a column header named 'Path'."""
    i = 0
    sep_re = re.compile(r'^\s*\|?[\s:|-]+\|?\s*$')
    while i < len(lines):
        line = lines[i]
        if '|' in line and i + 1 < len(lines) and sep_re.match(lines[i + 1]):
            headers = [h.strip().lower() for h in line.strip().strip('|').split('|')]
            if 'path' in headers:
                path_idx = headers.index('path')
                rows = []
                j = i + 2
                while j < len(lines) and '|' in lines[j] and lines[j].strip():
                    rows.append(lines[j])
                    j += 1
                yield path_idx, rows
                i = j
                continue
        i += 1


def extract_path(cell):
    cell = cell.strip()
    m = re.search(r'`([^`]+)`', cell)
    if m:
        return m.group(1)
    if cell and cell not in ('-', '—', ''):
        return cell
    return None


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_paths.py <repo-root> <file1.md> [file2.md ...]", file=sys.stderr)
        sys.exit(2)

    repo_root = sys.argv[1]
    files = sys.argv[2:]
    missing = []

    for f in files:
        if not os.path.exists(f):
            continue
        with open(f, encoding='utf-8') as fh:
            lines = fh.readlines()
        for path_idx, rows in find_path_column_tables(lines):
            for row in rows:
                cells = [c.strip() for c in row.strip().strip('|').split('|')]
                if path_idx >= len(cells):
                    continue
                path = extract_path(cells[path_idx])
                if not path:
                    continue
                full = os.path.join(repo_root, path)
                if not os.path.exists(full):
                    missing.append((path, f))

    if missing:
        for path, f in missing:
            print(f"MISSING: {path} (in {f})")
        sys.exit(1)

    print("All referenced paths exist.")
    sys.exit(0)


if __name__ == '__main__':
    main()
