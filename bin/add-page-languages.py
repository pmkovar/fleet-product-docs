#!/usr/bin/env python3
"""
Adds or updates an ifeval block with :page-languages: below the document title in AsciiDoc files.

Usage:
    ./bin/add-page-languages.py -v <version> -d <base_dir> -a <attribute> --value <attribute_value> [-l <languages>]

Defaults:
    -l:   "en, de, es, fr, ja, pt, zh"
"""

import argparse
import os
import re
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Add or update :page-languages: ifeval block in AsciiDoc files."
    )
    parser.add_argument(
        "-v",
        "--version",
        required=True,
        help="Target version directory (e.g. v0.15, v0.16, next)",
    )
    parser.add_argument(
        "-d",
        "--dir",
        required=True,
        help="Base directory containing version subdirectories (e.g. community-docs)",
    )
    parser.add_argument(
        "-a",
        "--attribute",
        required=True,
        help="ifeval attribute name (e.g. product)",
    )
    parser.add_argument(
        "--value",
        required=True,
        help="ifeval attribute expected value",
    )
    parser.add_argument(
        "-l",
        "--languages",
        default="en, de, es, fr, ja, pt, zh",
        help='Language codes list (default: "en, de, es, fr, ja, pt, zh")',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without writing changes to files.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Clean up languages if brackets are passed
    languages = args.languages.strip()
    if languages.startswith("[") and languages.endswith("]"):
        languages = languages[1:-1].strip()

    target_dir = Path(args.dir) / args.version / "modules" / "ROOT" / "pages"

    if not target_dir.is_dir():
        print(f"Error: Target directory does not exist: {target_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Processing *.adoc files in: {target_dir}")
    print(f"  Attribute : {args.attribute} == {args.value}")
    print(f"  Languages : [{languages}]")
    if args.dry_run:
        print("  Mode      : DRY RUN (no files will be modified)")

    replacement_block = (
        f'ifeval::["{{{args.attribute}}}" == "{args.value}"]\n'
        f":page-languages: [{languages}]\n"
        f"endif::[]"
    )

    # Regex pattern to match existing ifeval block wrapping :page-languages:
    ifeval_lang_pattern = re.compile(
        r'ifeval::\["[^"]*"\s*==\s*"[^"]*"\]\s*\n\s*:page-languages:[^\n]*\n\s*endif::\[\]\n?',
        re.MULTILINE,
    )
    # Regex pattern for standalone :page-languages: attribute line
    standalone_lang_pattern = re.compile(
        r"^[ \t]*:page-languages:[^\n]*\n?",
        re.MULTILINE,
    )
    # Matches "= Title" at line start
    title_pattern = re.compile(r"^(=[ \t]+[^\n]+)$", re.MULTILINE)

    added_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0

    for file_path in sorted(target_dir.rglob("*.adoc")):
        content = file_path.read_text(encoding="utf-8")

        # Check if identical replacement block is already present
        if replacement_block in content:
            unchanged_count += 1
            continue

        had_existing = False

        # Remove existing ifeval block if present
        if ifeval_lang_pattern.search(content):
            content = ifeval_lang_pattern.sub("", content)
            had_existing = True

        # Remove standalone :page-languages: if present
        if standalone_lang_pattern.search(content):
            content = standalone_lang_pattern.sub("", content)
            had_existing = True

        # Find document title
        title_match = title_pattern.search(content)
        if not title_match:
            print(f"Warning: No document title found in {file_path}, skipping.")
            skipped_count += 1
            continue

        # Insert directly below "= Title"
        title_end = title_match.end()
        new_content = (
            content[:title_end] + "\n" + replacement_block + content[title_end:]
        )

        if not args.dry_run:
            file_path.write_text(new_content, encoding="utf-8")

        if had_existing:
            updated_count += 1
        else:
            added_count += 1

    print("\nSummary:")
    print(f"  Added:     {added_count}")
    print(f"  Updated:   {updated_count}")
    print(f"  Unchanged: {unchanged_count}")
    if skipped_count:
        print(f"  Skipped:   {skipped_count}")


if __name__ == "__main__":
    main()
