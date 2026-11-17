"""Which EC2 instance types this deployment covers, and which of them get Spot placement scores.

Two sets, each decided in one place so every consumer agrees:

* The page set (tracked): every GPU instance family by the AWS naming rule, a name that starts with
  "g" or "p" followed by the generation digit (g4dn, g5, g6e, g7e, p4d, p5en, p6-b200, and families
  EC2 adds later). Other letters (gr6, trn1, inf2, vt1, dl1) are left out. The supply matrix,
  prices, quotas, test launches and Spot score alerts use it. The probe role's IAM condition
  cannot express "a digit follows", so it uses WILDCARDS ("g*", "p*") and the API's guard enforces
  the rule before any launch request.
* The Spot score set (scored): the tracked types that match the deploy setting `spotScoreFamilies`
  (instance-type patterns such as "p5.*"; DEFAULT_SPOT_SCORE_PATTERNS when unset). AWS restricts how
  many different Spot placement score requests an account can make, and every scored family costs
  one request per GPU count and per 10 Regions that sell it (score_groups), so only these types get
  scores.

Capacity Blocks use neither set: they follow each Region's own detected list
(capacity_blocks.supported_types).
"""
import fnmatch
import re

# The page set: a family name of letters "g" or "p", then the generation digit.
GPU_FAMILY_RE = re.compile(r"^[gp]\d")
# The instance-type wildcards that cover the page set, for an EC2 filter and the probe IAM
# condition. They are wider than the rule (g* also matches gr6), so callers narrow with tracked().
WILDCARDS = ("g*", "p*")
# The default Spot score set: g6e, g7, g7e, p5, p5e, p5en, p6-b200, p6-b300. Each pattern names one
# family with every size: "g7.*" matches g7.xlarge but not g7e.xlarge (the dot comes right after
# the family name).
DEFAULT_SPOT_SCORE_PATTERNS = ("g6e.*", "g7.*", "g7e.*", "p5.*", "p5e.*", "p5en.*", "p6-b200.*", "p6-b300.*")
# A pattern that names one family with every size: "p6-b200.*" -> "p6-b200".
_WHOLE_FAMILY_RE = re.compile(r"^([a-z][a-z0-9-]*)\.\*$")
# The cache tag of offering items built for the page set (see cache_tag).
_PAGE_SET_TAG = "gpu:g*,p*"


def family_of(itype):
    """The family part of an instance type name ("g6e.xlarge" -> "g6e"); a bare family stays itself."""
    return itype.split(".", 1)[0]


def tracked(itype):
    """True when the instance type (or bare family name) is in the page set: a GPU family by the
    naming rule. The single check every consumer shares."""
    return isinstance(itype, str) and bool(GPU_FAMILY_RE.match(family_of(itype)))


def filter_values():
    """The instance-type values for an EC2 `DescribeInstanceTypeOfferings` filter and for the probe
    IAM condition: wide enough to cover the page set. Callers narrow with tracked()."""
    return list(WILDCARDS)


def rule_text():
    """The page set in words, for an error message and /api/config."""
    return "g and p (GPU) instance families"


def cache_tag():
    """Identifies the page set inside a cached offering item, so an item built for another set (an
    earlier version of the sample) is fetched again instead of read."""
    return _PAGE_SET_TAG


def score_patterns_or_default(patterns):
    """The configured Spot score patterns, or DEFAULT_SPOT_SCORE_PATTERNS when none are given (a
    function started without the deploy-time setting)."""
    found = [p for p in (patterns or ()) if p]
    return list(dict.fromkeys(found)) if found else list(DEFAULT_SPOT_SCORE_PATTERNS)


def pattern_matches(itype, pattern):
    """True when the instance type matches one `spotScoreFamilies` pattern, as fnmatch does. The
    deploy accepts only patterns that name tracked families ("g6e.*", "g7*"), not letter-only ones
    such as "g*"."""
    return fnmatch.fnmatchcase(itype, pattern)


def scored(itype, patterns):
    """True when this deployment requests Spot placement scores for the instance type: a tracked
    type that matches one of the `spotScoreFamilies` patterns."""
    return tracked(itype) and any(pattern_matches(itype, p) for p in patterns)


def names(patterns):
    """The Spot score set for people, for /api/config and messages: the family name of each
    whole-family pattern ("p5.*" -> "p5"), any other pattern as it is."""
    out = []
    for p in patterns:
        found = _WHOLE_FAMILY_RE.match(p)
        out.append(found.group(1) if found else p)
    return list(dict.fromkeys(out))


def description(patterns):
    """The Spot score set in words: "g6e, g7, p5"."""
    return ", ".join(names(patterns))
