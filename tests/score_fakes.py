"""Fakes for the Spot score groups, the score updater and the Spot score routes: an in-memory cache
table with the conditional writes DynamoDB evaluates, and EC2 stand-ins that record every call."""
import json
import threading

from botocore.exceptions import ClientError

import cache_fakes

NOW = 2_000_000_000  # 2033-05-18T03:33:20Z
DAY = 86400


class Table:
    """get_item/put_item by cacheKey (no batch client: cache_reads reads item by item)."""

    def __init__(self):
        self.items, self.puts = {}, []
        self.lock = threading.Lock()
        self.fail_get = None

    def get_item(self, Key, **_):
        if self.fail_get:
            raise self.fail_get
        with self.lock:
            item = self.items.get(Key["cacheKey"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        cache_fakes.conditional_put(self.items, Item, ConditionExpression, ExpressionAttributeValues)
        with self.lock:
            self.puts.append(Item["cacheKey"])

    def puts_of(self, prefix):
        return [k for k in self.puts if k.startswith(prefix)]


def limit_error():
    return ClientError({"Error": {"Code": "MaxConfigLimitExceeded",
                                  "Message": "You have exceeded the maximum number of request configurations."}},
                       "GetSpotPlacementScores")


class Ec2:
    """One Region's EC2: DescribeRegions, Region-level offerings, DescribeInstanceTypes (GPU counts),
    GetSpotPlacementScores (score = 1 + len(region) % 9 unless `scores` says) and
    DescribeSpotPriceHistory. `log` (shared by every Region) records (region, operation, kwargs)."""

    def __init__(self, region, world):
        self.region, self.world = region, world

    def _log(self, op, kwargs):
        with self.world.lock:
            self.world.log.append((self.region, op, kwargs))

    def describe_regions(self, **kwargs):
        self._log("DescribeRegions", kwargs)
        if self.world.fail_regions:
            raise ClientError({"Error": {"Code": "UnauthorizedOperation", "Message": "x"}}, "DescribeRegions")
        return {"Regions": [{"RegionName": r} for r in self.world.enabled]}

    def get_paginator(self, name):
        outer = self

        class Pager:
            def paginate(self, **kwargs):
                return outer._page(name, kwargs)
        return Pager()

    def _page(self, name, kwargs):
        w = self.world
        if name == "describe_instance_type_offerings":
            self._log("DescribeInstanceTypeOfferings", kwargs)
            if self.region in w.fail_offerings:
                raise ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "x"}}, "DescribeInstanceTypeOfferings")
            return [{"InstanceTypeOfferings": [{"InstanceType": t, "Location": self.region}
                                               for t in w.sells.get(self.region, [])]}]
        if name == "describe_instance_types":
            self._log("DescribeInstanceTypes", kwargs)
            if w.fail_gpus or self.region in w.fail_gpus_in:
                raise ClientError({"Error": {"Code": "RequestLimitExceeded", "Message": "x"}}, "DescribeInstanceTypes")
            return [{"InstanceTypes": [{"InstanceType": t, "GpuInfo": {"Gpus": [{"Count": w.gpus[t]}]}}
                                       for t in kwargs["InstanceTypes"]]}]
        if name == "get_spot_placement_scores":
            self._log("GetSpotPlacementScores", kwargs)
            assert set(kwargs) == {"InstanceTypes", "TargetCapacity", "RegionNames"} and kwargs["TargetCapacity"] == 1
            outcome = w.sps_outcome(kwargs)
            if isinstance(outcome, Exception):
                raise outcome
            return [{"SpotPlacementScores": [{"Region": r, "Score": v} for r, v in outcome.items()]}]
        if name == "describe_spot_price_history":
            self._log("DescribeSpotPriceHistory", kwargs)
            return [{"SpotPriceHistory": [{"InstanceType": t, "AvailabilityZone": f"{self.region}{z}",
                                           "SpotPrice": w.price(t, z)}
                                          for t in kwargs["InstanceTypes"] if t in w.sells.get(self.region, [])
                                          for z in "ab"]}]
        raise AssertionError(name)


class World:
    """Every Region's EC2 at once: what each Region sells, the GPU count per type, and the Spot
    placement score answers (`refuse`: fingerprints of requests AWS refuses for the limit)."""

    def __init__(self, sells, gpus, enabled=None):
        self.sells = {r: list(t) for r, t in sells.items()}
        self.gpus = dict(gpus)
        self.enabled = list(enabled or sorted(sells))
        self.log = []
        self.lock = threading.Lock()
        self.fail_regions = False
        self.fail_offerings = set()
        self.fail_gpus = False
        # Regions whose DescribeInstanceTypes fails (fail_gpus: every Region).
        self.fail_gpus_in = set()
        self.refuse_all = False
        self.scores = {}

    def client(self, region):
        return Ec2(region, self)

    def sps_outcome(self, kwargs):
        if self.refuse_all:
            return limit_error()
        return {r: self.scores.get(r, 1 + len(r) % 9) for r in kwargs["RegionNames"]
                if any(t in self.sells.get(r, []) for t in kwargs["InstanceTypes"])}

    @staticmethod
    def price(itype, zone):
        base = {"g6e.xlarge": "1.10", "g6e.2xlarge": "1.30", "g6e.12xlarge": "5.00", "g6e.24xlarge": "9.00",
                "p5.48xlarge": "30.00"}.get(itype, "2.00")
        return base if zone == "a" else str(float(base) + 0.5)

    def calls(self, op):
        return [(r, kw) for r, o, kw in self.log if o == op]


def state_item(groups, gpus, families, daily_at=NOW - 3600, offered=None, version=1):
    """A stored #sps-groups item."""
    return {"cacheKey": "#sps-groups", "version": version, "families": json.dumps(sorted(families)),
            "groups": json.dumps(groups, sort_keys=True), "gpus": json.dumps(gpus, sort_keys=True),
            "offered": json.dumps(offered or {}, sort_keys=True), "dailyAt": daily_at, "updatedAt": daily_at}
