#!/usr/bin/env python3

"""
Add/verify/fix anchor ID for every AsciiDoc heading, except document titles.

The anchor of a heading is the Asciidoctor-style slug of the heading text,
for example:

    == Available experimental features
    -> [#_available_experimental_features]

Every anchor is validated against the heading it is attached to, so this also
fixes anchors that are already there but do not follow the convention,
in either the [[old-style]] or the [#new-style] form:

    [[examining-the-bundle-lifecycle-with-the-cli]]
    -> [#_examining_the_bundle_lifecycle_with_the_cli]

and anchors that are are wrong for their heading, including duplicated headings
with the same anchor. An anchor that does not match its own heading's title is
rewritten, and if both match, the second is renumbered with a trailing _2, _3.

Unless --no-xrefs is given, every xref: and <<...>> reference to an anchor that
changed is updated as well. This includes references to the IDs that
Asciidoctor generated implicitly before an explicit anchor was added.

Usage, from the repository root:

  add-heading-anchors.py docs               # rewrite every version
  add-heading-anchors.py docs -n -v         # preview the changes
  add-heading-anchors.py docs \
      --versions v0.14 v0.15                # only these versions
  add-heading-anchors.py docs \
      --modules en                          # only these modules
  add-heading-anchors.py docs \
      --resolve-attributes                  # resolve {attr} in headings
  add-heading-anchors.py docs \
      --attributes-file playbook.yml        # supply global attributes
  add-heading-anchors.py docs \
      --ignore "*.draft.adoc" "temp/*"      # ignore matching adoc files
  add-heading-anchors.py docs --check-links # also report references
                                            # that match no heading
                                            # in their target page
  add-heading-anchors.py docs --check       # only verify (exit 1 on drift)

DOCSROOT is expected to contain one directory per component version, the Antora
way: DOCSROOT/<version>/<modules-dirname>/<module>/<pages-dirname>/...
(for example docs/next/modules/ROOT/pages/... or
versions/v2.11/modules/en/pages/...). Pass --modules to only process-specific
modules (e.g. --modules en), or --modules-dirname/--pages-dirname if a
repository names those directories differently.
"""

import argparse
import fnmatch
import posixpath
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# Names of the Antora directories that make up a page path; overridable via
# --modules-dirname/--pages-dirname since main() reassigns these before use.
MODULES_DIRNAME = "modules"
PAGES_DIRNAME = "pages"

# Files that carry no headings, or that must not be touched.
SKIP_FILENAMES = {"nav.adoc", ".asciidoctorconfig.adoc"}

HEADING_RE = re.compile(r"^(={1,6})[ \t]+(\S.*?)[ \t]*$")
# [[an-anchor]] or [[an-anchor,reftext]] on a line of its own
BLOCK_ANCHOR_RE = re.compile(r"^\[\[([^\[\],]+)(?:,[^\]]*)?\]\][ \t]*$")
# [#an-anchor] or [#an-anchor,options] on a line of its own
ID_ATTR_RE = re.compile(r"^\[#([^\[\],\s]+)(?:,[^\]]*)?\][ \t]*$")
INLINE_ANCHOR_RE = re.compile(r"\[\[([a-zA-Z0-9_][a-zA-Z0-9_.:-]*)\]\]")
HTML_ANCHOR_RE = re.compile(
    r"""<(?:a\s+[^>]*\b(?:id|name)|[a-zA-Z0-9]+\s+[^>]*\bid)=["']([^"']+)["']"""
)
# Any other block attribute line, such as [discrete], that may sit between an
# anchor and the heading both belong to.
ATTR_LINE_RE = re.compile(r"^\[[^\]]*\][ \t]*$")
# Delimited block boundaries. Section titles are never parsed inside a block.
DELIM_RE = re.compile(
    r"^(/{4,}|-{4,}|\.{4,}|={4,}|\*{4,}|_{4,}|\+{4,}|`{3,}|--|[|,;:]={3,})[ \t]*$"
)
# A line comment, which neither ends a paragraph nor starts a section.
COMMENT_RE = re.compile(r"^//(?!/)")
# An attribute entry, such as :revdate: 2026-01-01
ATTR_ENTRY_RE = re.compile(r"^:[\w!][\w!-]*:")

ATTR_REF_RE = re.compile(r"\{([a-zA-Z0-9_][a-zA-Z0-9_-]*)\}")
# Character entities are dropped by Asciidoctor's ID generator, not replaced.
ENTITY_RE = re.compile(r"&(?:[a-z][a-z]+\d{0,2}|#\d{1,5}|#x[0-9a-f]{1,4});")
# Everything but letters, digits and the separator characters is dropped, ...
INVALID_ID_CHARS_RE = re.compile(r"[^\w \-.]")
# ... while these turn into a single separator.
ID_SEPARATOR_RE = re.compile(r"[ \-._]+")

XREF_RE = re.compile(r"xref:([^\[\]\s]+)\[")
INTERNAL_XREF_RE = re.compile(r"<<([^\s<>,]+(?:[^\n<>,]*[^\s<>,])?)((?:,[^<>\n]*)?)>>")
# An xref target that is a bare ID in the same page rather than a resource
IN_PAGE_XREF_RE = re.compile(r"^[\w.-]+$")


def slugify(text):
    """Turn heading text into an Asciidoctor section ID.

    Mirrors Asciidoctor's own ID generator: character entities and punctuation
    are dropped, spaces, hyphens and dots become the separator, runs of the
    separator collapse, and a trailing separator is removed.
    """
    text = ENTITY_RE.sub("", text.lower())
    text = INVALID_ID_CHARS_RE.sub("", text)
    text = ID_SEPARATOR_RE.sub("_", "_" + text)
    if len(text) > 1 and text.endswith("_"):
        text = text[:-1]
    return text


def literal_id(title):
    """The ID this script assigns: {attr_name} contributes 'attrname'."""
    return slugify(
        ATTR_REF_RE.sub(lambda m: re.sub(r"[^a-z0-9]", "", m.group(1).lower()), title)
    )


def expand_attribute_value(val, attributes, depth=0):
    """Recursively expand {attr} references in an attribute value up to max depth."""
    if depth > 5 or not val or "{" not in val:
        return val
    return ATTR_REF_RE.sub(
        lambda m: expand_attribute_value(attributes.get(m.group(1), m.group(0)), attributes, depth + 1),
        val,
    )


def resolved_id(title, attributes, path=None, line=None, warnings=None):
    """The ID generated with attributes resolved.

    If an attribute cannot be resolved and warnings is provided, a warning
    is emitted and the attribute name is used as fallback.
    """
    def replace(m):
        attr_name = m.group(1)
        if attr_name in attributes:
            return expand_attribute_value(attributes[attr_name], attributes)
        if warnings is not None and path is not None and line is not None:
            warnings.append(
                "%s:%d: unresolved attribute {%s} in heading %r, falling back to literal name"
                % (path, line, attr_name, title)
            )
        return re.sub(r"[^a-z0-9]", "", attr_name.lower())

    return slugify(ATTR_REF_RE.sub(replace, title))


def uniquify(candidate, used):
    """Append _2, _3, ... on collision, the way Asciidoctor deduplicates IDs.

    Also what turns a pre-existing duplicate anchor into a fix: the second
    heading to claim a given ID never gets to keep it.
    """
    unique = candidate
    counter = 1
    while unique in used:
        counter += 1
        unique = "%s_%d" % (candidate, counter)
    used.add(unique)
    return unique


def starts_a_block(lines, index):
    """True if a line begins a block, rather than continuing the one above it.

    A section title only counts as one when nothing but blank lines, comments
    or a block boundary precedes it.
    """
    scan = index - 1
    while scan >= 0 and COMMENT_RE.match(lines[scan]):
        scan -= 1
    if scan < 0:
        return True
    above = lines[scan]
    return (
        not above.strip()
        or bool(DELIM_RE.match(above))
        or bool(ATTR_ENTRY_RE.match(above))
        or bool(HEADING_RE.match(above))
    )


def is_verbatim(delimiter):
    """True for blocks whose content Asciidoctor does not parse."""
    return delimiter != "--" and delimiter[0] in "-./+`"


class Heading:
    """A section title, with the anchor lines already attached to it."""

    def __init__(self, index, title, anchor_lines, insert_at, explicit_ids):
        self.index = index
        self.title = title
        self.anchor_lines = anchor_lines
        self.insert_at = insert_at
        self.explicit_ids = explicit_ids


def collect_headings(lines):
    """Return the headings of a document, ignoring delimited block content."""
    headings = []
    open_blocks = []
    title_seen = False

    for index, line in enumerate(lines):
        delimiter = DELIM_RE.match(line)
        if delimiter:
            token = delimiter.group(1)
            if open_blocks and open_blocks[-1] == token:
                open_blocks.pop()
            elif not (open_blocks and is_verbatim(open_blocks[-1])):
                open_blocks.append(token)
            continue
        if open_blocks:
            continue

        heading = HEADING_RE.match(line)
        if not heading:
            continue
        if len(heading.group(1)) == 1 and not title_seen:
            title_seen = True  # the document title gets no anchor
            continue
        title_seen = True

        anchor_lines = []
        explicit_ids = []
        insert_at = index
        scan = index - 1
        while scan >= 0:
            anchor = BLOCK_ANCHOR_RE.match(lines[scan]) or ID_ATTR_RE.match(lines[scan])
            if anchor:
                anchor_lines.append(scan)
                explicit_ids.append(anchor.group(1))
                insert_at = scan
            elif ATTR_LINE_RE.match(lines[scan]):
                insert_at = scan
            elif not COMMENT_RE.match(lines[scan]):
                break
            scan -= 1
        explicit_ids.reverse()

        if insert_at == index and not starts_a_block(lines, index):
            # Attached to the paragraph or list above it, so not a section.
            continue

        headings.append(
            Heading(index, heading.group(2), anchor_lines, insert_at, explicit_ids)
        )

    return headings


def collect_extra_anchors(lines, skip_lines=None):
    """Collect explicit anchors from lines not attached to headings."""
    anchors = set()
    open_blocks = []
    skip = skip_lines or set()
    for index, line in enumerate(lines):
        delimiter = DELIM_RE.match(line)
        if delimiter:
            token = delimiter.group(1)
            if open_blocks and open_blocks[-1] == token:
                open_blocks.pop()
            elif not (open_blocks and is_verbatim(open_blocks[-1])):
                open_blocks.append(token)
            continue
        if open_blocks and is_verbatim(open_blocks[-1]):
            continue
        if index in skip:
            continue
        m = ID_ATTR_RE.match(line) or BLOCK_ANCHOR_RE.match(line)
        if m:
            anchors.add(m.group(1))
            continue
        for m in INLINE_ANCHOR_RE.finditer(line):
            anchors.add(m.group(1))
        for m in HTML_ANCHOR_RE.finditer(line):
            anchors.add(m.group(1))
    return anchors


def page_key(path):
    """(component version dir, module, path below pages/) of an Antora page."""
    parts = path.parts
    if MODULES_DIRNAME not in parts:
        return None
    modules_at = parts.index(MODULES_DIRNAME)
    if len(parts) < modules_at + 4 or parts[modules_at + 2] != PAGES_DIRNAME:
        return None
    return (
        "/".join(parts[:modules_at]),
        parts[modules_at + 1],
        "/".join(parts[modules_at + 3 :]),
    )


def module_of(path):
    """The Antora module name of a path, or None if outside modules/."""
    parts = path.parts
    if MODULES_DIRNAME not in parts:
        return None
    modules_at = parts.index(MODULES_DIRNAME)
    if len(parts) > modules_at + 1:
        return parts[modules_at + 1]
    return None


def normalize_list(items):
    """Flatten and strip comma- or space-separated CLI values."""
    if not items:
        return None
    result = []
    for item in items:
        for part in item.split(","):
            part = part.strip()
            if part:
                result.append(part)
    return result if result else None


PAGE_ATTR_RE = re.compile(r"^:([A-Za-z0-9_-]+):\s*(.*?)\s*$")


def clean_attribute_value(val):
    """Normalize attribute value: convert to str, strip whitespace, quotes, and soft-set @."""
    if val is None:
        return ""
    val = str(val).strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1].strip()
    if val.endswith("@"):
        val = val[:-1].strip()
    return val


def extract_yaml_attributes(data):
    """Extract attributes dictionary from parsed YAML data."""
    if not isinstance(data, dict):
        return {}
    attrs = {}
    if "asciidoc" in data and isinstance(data["asciidoc"], dict):
        if "attributes" in data["asciidoc"] and isinstance(data["asciidoc"]["attributes"], dict):
            for k, v in data["asciidoc"]["attributes"].items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    attrs[str(k)] = clean_attribute_value(v)
    if "attributes" in data and isinstance(data["attributes"], dict):
        for k, v in data["attributes"].items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                attrs[str(k)] = clean_attribute_value(v)
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            attrs[str(k)] = clean_attribute_value(v)
    return attrs


def parse_yaml_attributes_regex(text):
    """Fallback line-by-line regex parser for attributes in YAML."""
    attrs = {}
    in_attrs = False
    for line in text.splitlines():
        if re.match(r"^\s*attributes:\s*$", line):
            in_attrs = True
            continue
        if in_attrs:
            if line.strip() and not line.startswith(" "):
                in_attrs = False
            else:
                m = re.match(r"^\s+([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
                if m:
                    attrs[m.group(1)] = clean_attribute_value(m.group(2))
                    continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if m and not line.startswith(" "):
            attrs[m.group(1)] = clean_attribute_value(m.group(2))
    return attrs


def load_yaml_attributes(path):
    """Load attributes from a YAML file (antora.yml, playbook, or attributes file)."""
    p = Path(path)
    if not p.is_file():
        return {}
    text = p.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            return extract_yaml_attributes(data)
        except Exception:
            pass
    return parse_yaml_attributes_regex(text)


def read_attributes(version_dir):
    """The asciidoc attributes of a component version, read from antora.yml."""
    return load_yaml_attributes(Path(version_dir) / "antora.yml")


def read_page_attributes(lines):
    """Extract document-level attribute definitions from an AsciiDoc file header."""
    attrs = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("=="):
            break
        m = PAGE_ATTR_RE.match(line)
        if m:
            attrs[m.group(1)] = clean_attribute_value(m.group(2))
    return attrs


class Page:
    """One .adoc file, its heading IDs, and the anchor edits it needs."""

    def __init__(self, path):
        self.path = path
        self.id_map = {}    # ID that used to resolve here -> ID that does now
        self.new_ids = set()
        self.heading_titles = set()
        self.new_text = None
        self.anchors = 0


def plan_file(path, attributes, warnings, resolve_attributes=False):
    """Work out the anchors of one file and the text it should end up with.

    Every heading's anchor is recomputed from its own title and compared
    against what is already there. So an existing anchor is left alone only
    if it is both the right ID for its heading and the first heading on the
    page to claim that ID. A wrong anchor, or a correct one that a duplicate
    elsewhere on the page beat it to, gets rewritten.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    page = Page(path)
    used_old, used_new = set(), set()
    edits = []

    page_attrs = dict(attributes)
    page_attrs.update(read_page_attributes(lines))

    headings = collect_headings(lines)
    heading_anchor_lines = set()
    for heading in headings:
        heading_anchor_lines.update(heading.anchor_lines)

    extra_anchors = collect_extra_anchors(lines, skip_lines=heading_anchor_lines)
    used_new.update(extra_anchors)
    page.new_ids.update(extra_anchors)

    for heading in headings:
        page.heading_titles.add(heading.title)
        if "{" in heading.title:
            page.heading_titles.add(expand_attribute_value(heading.title, page_attrs))

        if resolve_attributes:
            new_id = resolved_id(
                heading.title,
                page_attrs,
                path=path,
                line=heading.index + 1,
                warnings=warnings,
            )
        else:
            new_id = literal_id(heading.title)
        if new_id == "_":
            warnings.append(
                "%s:%d: no usable anchor for heading %r, skipped"
                % (path, heading.index + 1, heading.title)
            )
            continue
        new_id = uniquify(new_id, used_new)

        # An explicit anchor keeps Asciidoctor from generating one, so only the
        # ID actually in effect takes a slot in the document's ID namespace.
        if heading.explicit_ids:
            old_ids = heading.explicit_ids
            for old_id in old_ids:
                used_old.add(old_id)
        else:
            old_ids = [uniquify(resolved_id(heading.title, page_attrs), used_old)]

        page.new_ids.add(new_id)
        for old_id in old_ids:
            if old_id != new_id:
                page.id_map[old_id] = new_id

        # True only for the first heading to legitimately own new_id: a wrong
        # anchor fails the equality check, and a duplicate of a correct one
        # fails it too, because uniquify() already moved new_id past it.
        placed = (
            heading.explicit_ids == [new_id]
            and len(heading.anchor_lines) == 1
            and heading.insert_at == heading.anchor_lines[0]
        )
        if not placed:
            edits.append((heading, new_id))

    # Never redirect a reference to an ID that is still a live anchor here.
    for old_id in list(page.id_map):
        if old_id in page.new_ids:
            del page.id_map[old_id]

    if not edits:
        return page

    for heading, new_id in reversed(edits):
        for index in sorted(heading.anchor_lines, reverse=True):
            del lines[index]
        lines.insert(heading.insert_at, "[#%s]" % new_id)

    page.anchors = len(edits)
    page.new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return page


def target_page(location, source_key):
    """The page key an xref location points at, or None if it is elsewhere."""
    if not location:
        return source_key
    if "@" in location or ":" in location:
        return None
    version, module, _ = source_key
    return version, module, posixpath.normpath(location.lstrip("/"))


def rewrite_references(path, source_key, index, warnings, check_links):
    """Point every reference at the new anchor of the heading it targets."""
    text = path.read_text(encoding="utf-8")
    rewrites = []

    def new_anchor(key, anchor, reference):
        page = index.get(key) if key else None
        if page is None:
            return None
        if anchor in page.id_map:
            rewrites.append((anchor, page.id_map[anchor]))
            return page.id_map[anchor]
        if (
            check_links
            and anchor not in page.new_ids
            and not (key == source_key and anchor in page.heading_titles)
        ):
            warnings.append(
                "%s: '%s' matches no heading in %s" % (path, reference, page.path)
            )
        return None

    def replace_xref(match):
        target = match.group(1)
        if "#" not in target:
            # xref:an-anchor[] is a reference to an ID in the same page.
            if target.endswith(".adoc") or not IN_PAGE_XREF_RE.match(target):
                return match.group(0)
            anchor_now = new_anchor(source_key, target, "xref:" + target)
            if anchor_now is None:
                return match.group(0)
            return "xref:%s[" % anchor_now
        location, _, anchor = target.rpartition("#")
        if not anchor:
            return match.group(0)
        if location and ("@" in location or ":" in location):
            warnings.append(
                "%s: cross-component 'xref:%s' left alone" % (path, target)
            )
            return match.group(0)
        anchor_now = new_anchor(
            target_page(location, source_key), anchor, "xref:" + target
        )
        if anchor_now is None:
            return match.group(0)
        return "xref:%s#%s[" % (location, anchor_now)

    def replace_internal(match):
        raw_target = match.group(1)
        location, sep, anchor = raw_target.rpartition("#")
        if not sep:
            anchor = raw_target
            target_k = source_key
            prefix = ""
        else:
            target_k = target_page(location, source_key) if location else source_key
            prefix = "%s#" % location

        if not anchor:
            return match.group(0)

        anchor_now = new_anchor(target_k, anchor, "<<%s>>" % raw_target)
        if anchor_now is None:
            return match.group(0)
        return "<<%s%s%s>>" % (prefix, anchor_now, match.group(2))

    new_text = XREF_RE.sub(replace_xref, text)
    new_text = INTERNAL_XREF_RE.sub(replace_internal, new_text)
    return new_text if new_text != text else None, rewrites


def should_ignore(path, patterns, docsroot=None):
    """True if path matches any ignore pattern (supports wildcards and filenames)."""
    if not patterns:
        return False
    posix_path = path.as_posix()
    posix_name = path.name
    rel_path = None
    if docsroot:
        try:
            rel_path = path.relative_to(docsroot).as_posix()
        except ValueError:
            pass

    for pattern in patterns:
        clean_pat = pattern.strip()
        if not clean_pat:
            continue
        # Direct filename match (e.g. 'foo.adoc', '*.draft.adoc')
        if fnmatch.fnmatch(posix_name, clean_pat):
            return True
        # Direct full path match (e.g. 'versions/v2.15/**/*.adoc', '*/air-gapped/*')
        if fnmatch.fnmatch(posix_path, clean_pat):
            return True
        # Normalized pattern without leading './' or '/'
        norm_pat = clean_pat
        if norm_pat.startswith("./"):
            norm_pat = norm_pat[2:]
        norm_pat = norm_pat.lstrip("/")
        if fnmatch.fnmatch(posix_path, norm_pat):
            return True
        if fnmatch.fnmatch(posix_path, f"*/{norm_pat}"):
            return True
        if rel_path is not None:
            if fnmatch.fnmatch(rel_path, clean_pat) or fnmatch.fnmatch(rel_path, norm_pat):
                return True
            if fnmatch.fnmatch(rel_path, f"*/{norm_pat}"):
                return True
    return False


def collect_files(
    docsroot, versions, modules=None, exclude_modules=None, ignore_patterns=None
):
    """The .adoc files to process: all of DOCSROOT, or only its --versions/--modules subdirs."""
    if docsroot.is_file():
        if versions:
            sys.exit("error: --versions requires DOCSROOT to be a directory")
        if modules:
            sys.exit("error: --modules requires DOCSROOT to be a directory")
        if exclude_modules:
            sys.exit("error: --exclude-modules requires DOCSROOT to be a directory")
        if should_ignore(docsroot, ignore_patterns, docsroot.parent):
            return []
        return [docsroot]
    if not docsroot.is_dir():
        sys.exit("error: %s does not exist" % docsroot)
    if not versions:
        candidates = sorted(docsroot.rglob("*.adoc"))
    else:
        candidates = []
        for version in versions:
            version_dir = docsroot / version
            if not version_dir.is_dir():
                sys.exit("error: version directory %s does not exist" % version_dir)
            candidates.extend(sorted(version_dir.rglob("*.adoc")))

    files = []
    modules_set = set(modules) if modules else None
    exclude_set = set(exclude_modules) if exclude_modules else set()
    for path in candidates:
        if should_ignore(path, ignore_patterns, docsroot):
            continue
        mod = module_of(path)
        if modules_set is not None and mod not in modules_set:
            continue
        if mod in exclude_set:
            continue
        files.append(path)
    return files


def main():
    global MODULES_DIRNAME, PAGES_DIRNAME

    parser = argparse.ArgumentParser(
        description="Add standard anchor IDs to AsciiDoc headings, "
        "validating and fixing any that are already there (including "
        "duplicated anchors on the same page).",
        epilog="Run from the repository root.",
    )
    parser.add_argument(
        "docsroot",
        metavar="DOCSROOT",
        help="documentation root directory to process, e.g. docs or versions",
    )
    parser.add_argument(
        "--versions", nargs="+", metavar="VERSION",
        help="only process these version subdirectories of DOCSROOT, "
        "e.g. --versions v0.14 v0.15",
    )
    parser.add_argument(
        "--modules", "--module", nargs="+", metavar="MODULE",
        help="only process these module subdirectories of <modules-dirname>, "
        "e.g. --modules en or --modules ROOT",
    )
    parser.add_argument(
        "--exclude-modules", "--exclude-module", nargs="+", metavar="MODULE",
        help="exclude these module subdirectories of <modules-dirname>, "
        "e.g. --exclude-modules zh ja",
    )
    parser.add_argument(
        "--modules-dirname", default=MODULES_DIRNAME,
        help="name of the Antora modules directory in the page path "
        "(default: %s)" % MODULES_DIRNAME,
    )
    parser.add_argument(
        "--pages-dirname", default=PAGES_DIRNAME,
        help="name of the Antora pages directory in the page path "
        "(default: %s)" % PAGES_DIRNAME,
    )
    parser.add_argument(
        "--resolve-attributes", action="store_true",
        help="resolve AsciiDoc attributes in headings when generating anchors "
        "(e.g. {rke2-product-name} -> rke2 instead of rke2productname)",
    )
    parser.add_argument(
        "--attributes-file", nargs="+", metavar="FILE",
        help="path to YAML attribute file(s) (e.g. playbook-community-local.yml); "
        "implies --resolve-attributes",
    )
    parser.add_argument(
        "--ignore", "--ignore-files", "--exclude-files",
        nargs="+", metavar="PATTERN", dest="ignore_files",
        help="ignore .adoc files matching the given pattern(s) or filename(s); "
        "supports wildcards (*, ?, **)",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="report what would change without writing anything",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="verify that all heading anchors and references are up to date "
        "without modifying files; exit with code 1 if drift is found",
    )
    parser.add_argument(
        "--fail-on-warnings", action="store_true",
        help="exit with code 1 if any warnings are encountered",
    )
    parser.add_argument(
        "--no-xrefs", action="store_true",
        help="rewrite anchors only, leave references to them alone",
    )
    parser.add_argument(
        "--check-links", action="store_true",
        help="also report references that match no heading in their target page",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list every change")
    args = parser.parse_args()

    if args.check:
        args.dry_run = True

    if args.attributes_file:
        args.resolve_attributes = True

    MODULES_DIRNAME = args.modules_dirname
    PAGES_DIRNAME = args.pages_dirname

    global_attributes = {}
    if args.attributes_file:
        for attr_file in args.attributes_file:
            attr_path = Path(attr_file)
            if not attr_path.is_file():
                sys.exit("error: attributes file %s does not exist" % attr_path)
            global_attributes.update(load_yaml_attributes(attr_path))

    files = collect_files(
        Path(args.docsroot),
        args.versions,
        normalize_list(args.modules),
        normalize_list(args.exclude_modules),
        ignore_patterns=normalize_list(args.ignore_files),
    )
    if not files:
        sys.exit("error: no .adoc files found")

    warnings = []
    attributes_cache = {}
    index = {}
    written = set()   # a symlinked page is only written through once
    anchors = 0
    anchor_files = 0

    for path in files:
        if path.name in SKIP_FILENAMES:
            continue
        key = page_key(path)
        version_dir = key[0] if key else str(path.parent)
        if version_dir not in attributes_cache:
            version_attrs = dict(global_attributes)
            version_attrs.update(read_attributes(version_dir))
            attributes_cache[version_dir] = version_attrs

        page = plan_file(
            path,
            attributes_cache[version_dir],
            warnings,
            resolve_attributes=args.resolve_attributes,
        )
        if key:
            index[key] = page
        if not page.anchors or path.resolve() in written:
            continue
        written.add(path.resolve())
        anchors += page.anchors
        anchor_files += 1
        if args.verbose or args.check:
            print("%s: %d anchor(s)" % (path, page.anchors))
        if not args.dry_run:
            path.write_text(page.new_text, encoding="utf-8")

    if args.check:
        print(
            "Found %d anchor(s) needing updates in %d file(s)"
            % (anchors, anchor_files)
        )
    else:
        print(
            "%s %d anchor(s) in %d file(s)"
            % ("Would write" if args.dry_run else "Wrote", anchors, anchor_files)
        )

    if args.no_xrefs:
        stale = sum(len(page.id_map) for page in index.values())
        if stale:
            print("Left references to %d renamed anchor(s) alone (--no-xrefs)" % stale)
        for warning in warnings:
            print("warning: %s" % warning, file=sys.stderr)
        failed = False
        if args.check and anchors > 0:
            print(
                "error: heading anchors are out of date. "
                "Run 'add-heading-anchors.py' to update them.",
                file=sys.stderr,
            )
            failed = True
        if args.fail_on_warnings and warnings:
            print("error: %d warning(s) encountered." % len(warnings), file=sys.stderr)
            failed = True
        if failed:
            sys.exit(1)
        return

    rewrites = 0
    rewrite_files = 0
    rewritten = set()
    for path in files:
        key = page_key(path)
        if not key:
            continue
        new_text, changed = rewrite_references(
            path, key, index, warnings, args.check_links
        )
        if not changed or path.resolve() in rewritten:
            continue
        rewritten.add(path.resolve())
        rewrites += len(changed)
        rewrite_files += 1
        if args.verbose or args.check:
            for old_anchor, anchor_now in changed:
                print("%s: #%s -> #%s" % (path, old_anchor, anchor_now))
        if not args.dry_run and new_text is not None:
            path.write_text(new_text, encoding="utf-8")

    if args.check:
        print(
            "Found %d reference(s) needing updates in %d file(s)"
            % (rewrites, rewrite_files)
        )
    else:
        print(
            "%s %d reference(s) in %d file(s)"
            % ("Would rewrite" if args.dry_run else "Rewrote", rewrites, rewrite_files)
        )

    for warning in warnings:
        print("warning: %s" % warning, file=sys.stderr)

    failed = False
    if args.check and (anchors > 0 or rewrites > 0):
        print(
            "error: heading anchors or references are out of date. "
            "Run 'add-heading-anchors.py' to update them.",
            file=sys.stderr,
        )
        failed = True
    elif args.check:
        print("All heading anchors and references are up to date.")

    if args.fail_on_warnings and warnings:
        print(
            "error: %d warning(s) encountered." % len(warnings),
            file=sys.stderr,
        )
        failed = True

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
