"""Natural ordering of EC2 instance type names, shared by every route that returns types.

The rules (the web page implements the same ones; tests/test_type_order.py is the shared spec):

1. Family first: the part before the first ".". It is split into runs of letters and runs of
   digits ("-" only separates runs). Runs are compared left to right: two digit runs as
   numbers, two letter runs as text, and a digit run before a letter run. A family that is a
   prefix of another comes first. So g6e < g7 < p4d < p5 < p5e < p5en < p6-b200 < p6-b300.
2. Then size: nano, micro, small, medium, large, xlarge, then Nxlarge by N ascending
   (2xlarge < 3xlarge < ... < 48xlarge), then metal, then metal-Nxl by N ascending.
   Any other size sorts after those, as text.
3. Ties (the same key for two different names) fall back to the full name as text.
"""
import re

_RUN_RE = re.compile(r"[a-z]+|\d+")
_NAMED_SIZES = {"nano": 0, "micro": 1, "small": 2, "medium": 3, "large": 4, "xlarge": 5}
_NXLARGE_RE = re.compile(r"^(\d+)xlarge$")
_METAL_RE = re.compile(r"^metal(?:-(\d+)xl)?$")


def family_key(family):
    """Sort key for a family name such as "p5en" or "p6-b200"."""
    return tuple((0, int(run), "") if run.isdigit() else (1, 0, run) for run in _RUN_RE.findall(family.lower()))


def size_key(size):
    """Sort key for a size such as "xlarge", "48xlarge", or "metal-24xl"."""
    size = size.lower()
    if size in _NAMED_SIZES:
        return (0, _NAMED_SIZES[size], "")
    match = _NXLARGE_RE.match(size)
    if match:
        return (1, int(match.group(1)), "")
    match = _METAL_RE.match(size)
    if match:
        return (2, int(match.group(1) or 0), "")
    return (3, 0, size)


def type_key(itype):
    """Sort key for a full instance type name such as "g6e.12xlarge"."""
    family, _, size = itype.partition(".")
    return (family_key(family), size_key(size), itype)


def sort_types(types):
    """The instance types in natural order, as a new list."""
    return sorted(types, key=type_key)


def sort_families(families):
    """Family names in natural order, as a new list."""
    return sorted(families, key=lambda f: (family_key(f), f))
