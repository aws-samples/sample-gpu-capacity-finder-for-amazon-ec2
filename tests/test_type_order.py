"""Table-driven spec for the natural instance-type order (lambda/type_order.py).

Each case is a list already in the expected order. The web page implements the same rules in
JavaScript and reuses these cases, so both sides order types the same way.
"""
import random

import pytest

from type_order import family_key, size_key, sort_families, sort_types, type_key

CASES = [
    # Families: letter runs as text, digit runs as numbers, a prefix first.
    ["g6e.2xlarge", "g7.2xlarge", "p4d.24xlarge", "p5.48xlarge", "p5e.48xlarge", "p5en.48xlarge",
     "p6-b200.48xlarge", "p6-b300.48xlarge"],
    ["p6-b200.48xlarge", "p6-b300.48xlarge"],
    ["p5.48xlarge", "p5e.48xlarge"],
    ["p5e.48xlarge", "p5en.48xlarge"],
    ["p4d.24xlarge", "p4de.24xlarge", "p5.4xlarge"],
    ["g6.xlarge", "g6e.xlarge", "g6f.large"],
    ["g6e.48xlarge", "g7.xlarge"],
    ["g7.48xlarge", "g7e.2xlarge"],
    ["g5.xlarge", "g6.xlarge", "g10.xlarge"],
    ["p3.2xlarge", "p3dn.24xlarge", "p4d.24xlarge"],
    ["inf1.xlarge", "inf2.xlarge", "p5.48xlarge"],
    ["trn1.2xlarge", "trn1.32xlarge", "trn1n.32xlarge", "trn2.48xlarge"],
    ["trn1.2xlarge", "trn1.32xlarge"],
    ["c7g.large", "c7gd.large", "c7gn.large", "c7i.large", "c7i-flex.large"],
    ["m7i.large", "m7i-flex.large", "m8g.large"],
    ["u-3tb1.56xlarge", "u-6tb1.56xlarge", "u-12tb1.112xlarge"],
    ["g4ad.xlarge", "g4dn.xlarge", "g5.xlarge"],
    # Sizes: named sizes, then Nxlarge by N, then metal variants by number.
    ["t3.nano", "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge", "t3.2xlarge"],
    ["g6e.xlarge", "g6e.2xlarge", "g6e.4xlarge", "g6e.8xlarge", "g6e.12xlarge", "g6e.16xlarge",
     "g6e.24xlarge", "g6e.48xlarge"],
    ["g6e.2xlarge", "g6e.12xlarge"],
    ["g6e.4xlarge", "g6e.16xlarge"],
    ["g7e.2xlarge", "g7e.4xlarge", "g7e.8xlarge", "g7e.12xlarge", "g7e.24xlarge", "g7e.48xlarge"],
    ["g7e.8xlarge", "g7e.48xlarge"],
    ["c5.large", "c5.xlarge", "c5.2xlarge", "c5.4xlarge", "c5.9xlarge", "c5.12xlarge", "c5.18xlarge",
     "c5.24xlarge", "c5.metal"],
    ["m5.2xlarge", "m5.3xlarge", "m5.4xlarge"],
    ["m7i.32xlarge", "m7i.48xlarge", "m7i.metal-24xl", "m7i.metal-48xl"],
    ["c7i.48xlarge", "c7i.metal", "c7i.metal-24xl", "c7i.metal-48xl"],
    ["i4i.32xlarge", "i4i.metal"],
    ["m5.metal", "m5.metal-24xl"],
    ["g4dn.metal", "g5.xlarge"],
    ["p5.4xlarge", "p5.48xlarge"],
    ["p5en.48xlarge", "p6-b200.48xlarge"],
    ["x2iedn.32xlarge", "x2iedn.metal", "x8g.large"],
    # Unknown sizes sort after the known ones, as text.
    ["z1d.metal", "z1d.huge", "z1d.weird"],
]


@pytest.mark.parametrize("ordered", CASES, ids=lambda c: f"{c[0]}..{c[-1]}")
def test_case_sorts_into_place(ordered):
    shuffled = list(ordered)
    random.Random(len(ordered)).shuffle(shuffled)
    assert sort_types(shuffled) == ordered
    assert sort_types(reversed(ordered)) == ordered


@pytest.mark.parametrize("ordered", CASES, ids=lambda c: f"{c[0]}..{c[-1]}")
def test_neighbours_compare_strictly(ordered):
    for a, b in zip(ordered, ordered[1:]):
        assert type_key(a) < type_key(b), (a, b)


def test_there_are_enough_cases():
    assert len(CASES) >= 30


def test_family_examples_from_the_brief():
    assert sort_families(["p6-b300", "p5en", "g7", "p5", "p6-b200", "p4d", "p5e", "g6e"]) == [
        "g6e", "g7", "p4d", "p5", "p5e", "p5en", "p6-b200", "p6-b300"]


def test_keys_are_case_insensitive_and_stable():
    assert family_key("P5EN") == family_key("p5en")
    assert size_key("48XLARGE") == size_key("48xlarge")
    # The same key falls back to the full name, so the order is total.
    assert sort_types(["g6e.2xlarge", "g6e.2xlarge"]) == ["g6e.2xlarge", "g6e.2xlarge"]


def test_size_ranks():
    sizes = ["nano", "micro", "small", "medium", "large", "xlarge", "2xlarge", "3xlarge", "4xlarge", "8xlarge",
             "9xlarge", "12xlarge", "16xlarge", "24xlarge", "32xlarge", "48xlarge", "metal", "metal-16xl", "metal-24xl",
             "metal-48xl", "other"]
    assert sorted(reversed(sizes), key=size_key) == sizes
