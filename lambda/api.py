"""HTTP API handler for GPU Capacity Finder.

Routes (all behind a Cognito JWT authorizer):
  GET  /api/config                 deployment settings for the web page, with the Region long names
  GET  /api/instance-types         instance types EC2 offers in a Region (allowed families only;
                                   ?capacityBlocks=true keeps the types that support Capacity
                                   Blocks there: DescribeInstanceTypes, once a day per Region)
  GET  /api/instance-type-zones    Availability Zones that offer one instance type
                                   (both read the offering matrix's DynamoDB item for the Region)
  GET  /api/capacity-blocks        Capacity Block offerings (DescribeCapacityBlockOfferings); startDate is a UTC day
  GET  /api/capacity-blocks/max    largest instance count with a Capacity Block offering, and its earliest start,
                                   in up to 3 short requests (?lo=&hi= from the previous answer; done: true ends)
  GET  /api/spot-scores            Spot placement scores of every score group (family + GPU count) by Region,
                                   from the score updater's cache (GetSpotPlacementScores repeats on Refresh)
  GET  /api/spot-heatmap           the same scores for up to 30 instance types (each type shows its group's)
  GET  /api/spot-prices            latest Spot prices of a Region: every scored type, or one type, per
                                   Availability Zone, with the "from" price of each score group
  GET  /api/on-demand-price        On-Demand list price per hour from the AWS Price List Query API (cached in DynamoDB)
  GET  /api/quotas                 GPU On-Demand, Spot, and Capacity Block quotas with usage
  GET  /api/offering-matrix        instance families each enabled Region offers, and every accelerator
                                   family the Regions offer (cached in DynamoDB)
  GET  /api/probes                 today's test launch counts and caps, the Region's default VPC, and the
                                   observed launch results of the last 24 hours per score group
  POST /api/probe                  test launch: one instance of a score group (or one size) in any zone of
                                   a Region (EC2 Fleet of type instant), terminated at once
  GET  /api/subscriptions          the caller's email alerts
  POST /api/subscriptions          add an email alert (subscribes the token's email once)
  DELETE /api/subscriptions        delete one of the caller's alerts (?id=...)

Cached answers carry fetchedAt (when AWS was asked, from the stored item) and fromCache. A forced
refresh (refresh=true) takes one lock per cached item for REFRESH_WINDOW_SECONDS; the answer then
reports refreshed, and retryAfterSeconds when the item's window was still running. Offering data
past its freshness window is still the answer (stale: true, with its own fetchedAt) while the
request asks AWS again inside a small budget.

A Region that fails to answer what it offers is reported as an error or as unknown. It never turns
into an empty offering list, so no answer and no cached item ever says that a Region or an instance
type is not sold because a lookup failed.

Every route accepts the Regions the account has enabled (EC2 DescribeRegions, cached for a day in
the offering cache under regions#enabled) minus excludeRegions; test launches too.

Spot placement scores: the score updater (score_updater.py) is the only code that creates request
configurations or changes the score groups. This function only reads its cache items and, on a
Refresh, repeats configurations AWS accepted in the last day (free).

The tool reads capacity signals and runs one bounded test launch; it creates no other billed resource.
"""
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
from botocore.config import Config
from botocore.exceptions import (BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError,
                                    ReadTimeoutError)

import alerts
import cache_reads
import capacity_blocks as cb
import families
import fleet_launch
import regions as region_list
import score_groups
import spot_cache
import spot_configs
import spot_prices
from type_order import sort_families, sort_types

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# The function's own Region: DescribeRegions is asked there, and REGIONS (only this Region) is the
# Region list while discovery has no answer at all. Every route covers the enabled Regions minus
# excludeRegions (read_regions).
HOME_REGION = os.environ.get("AWS_REGION") or score_groups.ENDPOINT
REGIONS = [HOME_REGION]
# The families that get Spot placement scores: the spotScoreFamilies setting (instance-type
# patterns, lambda/families.py), the default list when it is missing. Every other part of the page
# covers every GPU family by the naming rule (families.tracked).
SPOT_SCORE_FAMILIES = families.score_patterns_or_default(os.environ.get("SPOT_SCORE_FAMILIES", "").split(","))
TABLE_NAME = os.environ.get("TABLE_NAME", "")
# Offering items are served past their freshness window while they are asked again
# (stale-while-revalidate): OFFERING_CACHE_HOURS ends the freshness window ("refreshAfter" on the
# item) and the item itself is kept this long (its "expires", the table's TTL attribute), the
# max-stale bound after which it is gone and the next reader waits for the lookup again.
OFFERING_KEEP_SECONDS = 7 * 86400
# A request that answers from a stale item asks AWS again inside the same request, because a Lambda
# function stops working when it returns. It does so only when it took that item's refresh lock (one
# caller per item per window) and only within this budget, never into the last
# DEADLINE_RESERVE_SECONDS of the invocation, so the answer is never held back for long.
STALE_REFRESH_BUDGET_SECONDS = 4
DEADLINE_RESERVE_SECONDS = 6
ENABLE_PROBE = os.environ.get("ENABLE_PROBE", "true") == "true"
ENABLE_SPOT_PROBE = os.environ.get("TEST_LAUNCH_ALLOW_SPOT", "true") == "true"
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "15"))
ENABLE_NOTIFICATIONS = os.environ.get("ENABLE_NOTIFICATIONS", "false") == "true"
NOTIFY_INTERVAL_MINUTES = int(os.environ.get("NOTIFY_INTERVAL_MINUTES", "15"))
MAX_SUBSCRIPTIONS_PER_USER = int(os.environ.get("MAX_SUBSCRIPTIONS_PER_USER", "10"))
SUBSCRIPTIONS_TABLE_NAME = os.environ.get("SUBSCRIPTIONS_TABLE_NAME", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")
OFFERING_CACHE_TABLE_NAME = os.environ.get("OFFERING_CACHE_TABLE_NAME", "")
OFFERING_CACHE_HOURS = int(os.environ.get("OFFERING_CACHE_HOURS", "6"))
SPOT_SCORE_CACHE_MINUTES = int(os.environ.get("SPOT_SCORE_CACHE_MINUTES", "15"))
# The governor of Spot placement score request configurations (spot_configs): the most different
# configurations in use in 24 hours (spotScoreConfigsPerDay). Only the score updater grants new ones.
SPOT_SCORE_CONFIGS_PER_DAY = int(os.environ.get("SPOT_SCORE_CONFIGS_PER_DAY", "40"))
SPOT_SCORE_BUDGET = spot_configs.Budget(SPOT_SCORE_CONFIGS_PER_DAY, SPOT_SCORE_CONFIGS_PER_DAY)
# The score updater function, started once (asynchronously) when the score groups must be built.
SCORE_UPDATER_FUNCTION = os.environ.get("SCORE_UPDATER_FUNCTION", "")
QUOTA_CACHE_MINUTES = int(os.environ.get("QUOTA_CACHE_MINUTES", "30"))
# Informational: API Gateway enforces this on the default route; /api/config shows it.
API_RATE_LIMIT = int(os.environ.get("API_RATE_LIMIT", "10"))
# Real launch attempts one user may start per UTC day; 0 turns the cap off.
MAX_PROBES_PER_USER_PER_DAY = int(os.environ.get("MAX_PROBES_PER_USER_PER_DAY", "50"))
# Real launch attempts of all users together per UTC day (maxTestLaunchesPerDay).
MAX_TEST_LAUNCHES_PER_DAY = int(os.environ.get("MAX_TEST_LAUNCHES_PER_DAY", "100"))

# The probe tag, the security group tag and name, and the self-shutdown user data of every test
# launch (fleet_launch.py explains each).
PROBE_TAG = fleet_launch.PROBE_TAG
PROBE_SG_TAG = fleet_launch.PROBE_SG_TAG
PROBE_SG_NAME = fleet_launch.PROBE_SG_NAME
PROBE_USER_DATA = fleet_launch.PROBE_USER_DATA
# The probe image follows the instance type's architecture (Amazon Linux 2023 public parameters).
AMI_PARAMETERS = {
    "x86_64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "arm64": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
}
DEFAULT_ARCHITECTURE = "x86_64"
RETENTION_DAYS = 30

# GPU vCPU quotas are found by name instead of by quota code, so renamed or new
# per-family quotas (for example "All P5 Spot Instance Requests") still show up.
# The group names the family: "Running On-Demand Trn instances", "All P5 Spot Instance Requests".
# The DL quotas (Gaudi and AI 100 instances) are left out: the page covers GPU, Trainium, and Inferentia families.
QUOTA_NAME_RE = re.compile(r"\b(P\d*|G and VT|Trn\d*|Inf\d*)\b")
# Cached items written before the DL quotas were left out can still hold their rows.
OLD_DL_QUOTA_RE = re.compile(r"\bDL\d*\b")
# Capacity Block quotas ("Concurrent P5 Capacity Blocks per account", "... per organization").
CAPACITY_BLOCK_QUOTA = "Capacity Blocks"
CAPACITY_BLOCK_QUOTA_RE = re.compile(r"^Concurrent (\S+) Capacity Blocks\b")
QUOTA_FAMILIES = {"P": "p", "G and VT": "g", "Trn": "trn", "Inf": "inf"}
QUOTA_CATEGORIES = ("on-demand", "spot", "capacity-block")
# Every DynamoDB-backed cache keeps a copy in the container's memory for this long.
MEMORY_TTL_SECONDS = 60
REGIONS_FALLBACK_MEMORY_SECONDS = 5
MEMORY_CACHE_MAX = 500
# Prices: list prices change rarely, Spot prices often. A Price List lookup without a match is
# cached for an hour; a failed lookup is not cached.
OD_PRICE_TTL_SECONDS = 24 * 3600
OD_PRICE_NOT_FOUND_TTL_SECONDS = 3600
SPOT_PRICE_TTL_SECONDS = spot_prices.CACHE_SECONDS
# The Price List Query API answers from us-east-1 for every Region.
PRICING_REGION = "us-east-1"
OD_PRICE_MAX_PRODUCTS = 20
# AND-matched Price List attributes for a Linux On-Demand list price on shared tenancy.
# marketoption excludes the Capacity Block product of the same type, whose On-Demand term is 0.
OD_PRICE_FILTERS = (
    ("operatingSystem", "Linux"),
    ("tenancy", "Shared"),
    ("preInstalledSw", "NA"),
    ("capacitystatus", "Used"),
    ("termType", "OnDemand"),
    ("marketoption", "OnDemand"),
)
USAGE_WINDOW_SECONDS = 3600
USAGE_PERIOD_SECONDS = 300
# GetMetricData accepts at most 500 queries in one request.
METRIC_QUERIES_PER_CALL = 500
# Region fan-out of the offering lookups: all enabled Regions (about 30) on a cold cache fit the
# 25-second function timeout with 8 threads (two paginated calls per Region).
REGION_WORKERS = 8
# Codes EC2 and other services return in an opt-in Region the account has not enabled.
NOT_ENABLED_ERRORS = {"AuthFailure", "UnrecognizedClientException", "InvalidClientTokenId", "OptInRequired"}
NOT_ENABLED = "not enabled"
NOT_ENABLED_CODE = "not-enabled"
# Forced refresh: one lock item per cached item ("#lock#" + the item's name), one window for all.
REFRESH_LOCK_PREFIX = "#lock#"
REFRESH_WINDOW_SECONDS = 60
# Spot heatmap: types per request, worker threads, and time budget.
HEATMAP_MAX_TYPES = 30
HEATMAP_WORKERS = 8
HEATMAP_BUDGET_SECONDS = 22
# DynamoDB cache reads, writes, and lock claims of one request run on this many threads.
CACHE_WORKERS = 8
# The two EC2 quota lists of a Region (applied values and defaults) are read at the same time.
QUOTA_LIST_WORKERS = 2
# GetSpotPlacementScores errors. The 24-hour limit on new request configurations is recognized by
# spot_cache.is_limit_error; plain throttling has its own codes and passes within seconds.
SPS_THROTTLE_ERRORS = {"RequestLimitExceeded", "Throttling", "ThrottlingException"}
SPS_THROTTLE_RE = re.compile(r"rate exceeded", re.I)
SPS_UNSUPPORTED_ERRORS = {"InvalidParameterValue", "Unsupported"}

TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}\.[a-z0-9-]{1,20}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Dates further ahead than this are refused before any EC2 call.
DATE_MAX_DAYS = 366
# A Capacity Block search starts at most this many UTC days from today.
START_DATE_MAX_DAYS = 14
CB_MAX_ITEMS = 200
BAD_DAYS_MESSAGE = "days must be 1 to 14, or a multiple of 7 up to 182"
# Per-user daily probe counter in the probe table. "#" sorts before any Region name, so the
# counter partitions never mix with probe history.
PROBE_COUNT_PREFIX = "#probe-count#"
# The deployment-wide daily counter (maxTestLaunchesPerDay): one item per UTC day, partition
# "#probes#YYYY-MM-DD", kept two days after the day ends.
ALL_COUNT_PREFIX = "#probes#"

_clients = {}
_clients_lock = threading.Lock()
_ami_cache = {}
_arch_cache = {}
_table = None
_subscriptions_table = None
_offering_mem = {}
_quota_cache = {}
_price_cache = {}
_sps_mem = {}
_offering_cache_table = None
# The container's copy of the enabled Region list: (monotonic time, {"regions", "fetchedAt"}, seconds kept).
_regions_mem = None
# Cache hits and misses of the current request, for the request log line (one request per container at a time).
_cache_counts = {"hits": 0, "misses": 0}
_cache_counts_lock = threading.Lock()
_cold = True


class HttpError(Exception):
    """An error answer. `code` is a stable machine code the page maps to its own text; `params`
    fills the placeholders of that text. The English `message` stays as the fallback."""

    def __init__(self, status, message, code=None, params=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.params = params


def error_body(message, code=None, params=None):
    body = {"error": message}
    if code:
        body["code"] = code
    if params:
        body["params"] = params
    return body


# EC2 calls retry with botocore's standard mode, 3 attempts in all; nothing in this file retries on
# top of it. The page retries a 429 "throttled" itself.
EC2_RETRIES = {"mode": "standard", "total_max_attempts": 3}
# A Region whose EC2 endpoint cannot be reached must not hold a request for botocore's default
# 60 seconds: connect fast, and give a slow answer at most 15 seconds.
EC2_CONNECT_TIMEOUT = 3
EC2_READ_TIMEOUT = 15


# Every other client (Service Quotas, CloudWatch, Price List, SSM, SNS) is bounded too, so one slow
# endpoint cannot hold a request past the API function's timeout: 2 attempts, short timeouts.
OTHER_CLIENT_CONFIG = {"retries": {"mode": "standard", "total_max_attempts": 2},
                       "connect_timeout": 3, "read_timeout": 10}
# The score updater is started with an asynchronous invoke that answers at once; one attempt only,
# since a failed start is tried again by a later request once the start lock runs out.
LAMBDA_INVOKE_CONFIG = {"retries": {"mode": "standard", "total_max_attempts": 1},
                        "connect_timeout": 3, "read_timeout": 5}


def _client_options(service):
    if service == "ec2":
        return {"config": Config(retries=dict(EC2_RETRIES), connect_timeout=EC2_CONNECT_TIMEOUT,
                                 read_timeout=EC2_READ_TIMEOUT)}
    settings = LAMBDA_INVOKE_CONFIG if service == "lambda" else OTHER_CLIENT_CONFIG
    return {"config": Config(retries=dict(settings["retries"]), connect_timeout=settings["connect_timeout"],
                             read_timeout=settings["read_timeout"])}


def client(service, region):
    key = (service, region)
    # Client creation from the default session is not thread-safe; the Region fan-out needs the lock.
    with _clients_lock:
        if key not in _clients:
            _clients[key] = boto3.client(service, region_name=region, **_client_options(service))
        return _clients[key]


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def subscriptions_table():
    global _subscriptions_table
    if _subscriptions_table is None:
        _subscriptions_table = boto3.resource("dynamodb").Table(SUBSCRIPTIONS_TABLE_NAME)
    return _subscriptions_table


def offering_cache_table():
    global _offering_cache_table
    if _offering_cache_table is None:
        _offering_cache_table = boto3.resource("dynamodb").Table(OFFERING_CACHE_TABLE_NAME)
    return _offering_cache_table


def _prewarm():
    """Create the table handles and the clients of the function's own Region while the function
    initializes, so the first request does not pay for them. Creating a client sends no request.
    A failure only leaves that handle to the lazy path; initialization never fails here."""
    handles = [table, offering_cache_table] + ([subscriptions_table] if SUBSCRIPTIONS_TABLE_NAME else [])
    handles += [lambda s=s: client(s, HOME_REGION) for s in ("ec2", "service-quotas", "cloudwatch")]
    handles.append(lambda: client("pricing", PRICING_REGION))
    if TOPIC_ARN:
        handles.append(lambda: client("sns", None))
    for make in handles:
        try:
            make()
        except Exception:  # noqa: BLE001 - the handle is created again on first use
            logger.warning("could not create a client during initialization", exc_info=True)


def _count_cache(hit):
    with _cache_counts_lock:
        _cache_counts["hits" if hit else "misses"] += 1


def _cache_map(fn, items):
    """[fn(item) for item in items] with the DynamoDB calls on a small thread pool. Table actions
    only build parameters and call the thread-safe low-level client. An exception is raised in
    item order, as the loop would."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(CACHE_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def _epoch():
    return int(time.time())


def _conditional_failed(err):
    return err.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _now():
    return time.monotonic()


# The monotonic time at which the platform stops this invocation, taken from the Lambda context at
# the start of each request; None when it is unknown (a local run or a test). Only one request runs
# in a container at a time, so one value per container is enough.
_deadline = None


def _set_deadline(context):
    """Keep the invocation's own deadline, so work that may be skipped (a refresh of stale data)
    can leave the answer enough time."""
    global _deadline
    try:
        _deadline = _now() + context.get_remaining_time_in_millis() / 1000
    except Exception:  # noqa: BLE001 - without a usable context the budgets fall back to their own limits
        _deadline = None


def _time_left():
    """Seconds left of this invocation, or None when it is unknown."""
    return None if _deadline is None else _deadline - _now()


def _revalidate_budget():
    """Seconds this request may spend asking AWS again for data it has already answered from: at
    most STALE_REFRESH_BUDGET_SECONDS, and never the last DEADLINE_RESERVE_SECONDS of the
    invocation. 0 means there is no time to spare, so the stale answer stands."""
    left = _time_left()
    if left is None:
        return STALE_REFRESH_BUDGET_SECONDS
    return max(0.0, min(STALE_REFRESH_BUDGET_SECONDS, left - DEADLINE_RESERVE_SECONDS))


def iso(dt):
    """Fixed-width UTC timestamp so DynamoDB sort keys compare in time order."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value)}")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


# ---------------------------------------------------------- enabled Regions
def _describe_regions():
    """Live EC2 DescribeRegions in the function's own Region: the enabled commercial Regions."""
    return region_list.describe(client("ec2", HOME_REGION))


# Every route covers the enabled Regions minus EXCLUDE_REGIONS.
def _region_info():
    """{regions, fetchedAt}: the enabled Regions minus the excluded ones, from the container's copy
    (MEMORY_TTL_SECONDS), else the shared cache item, refreshed at most once per day by the caller
    that takes the "#lock#regions#enabled" lock. Never raises; falls back to the stale list, else to
    REGIONS, the function's own Region (fetchedAt None)."""
    global _regions_mem
    hit = _regions_mem
    if hit and _now() - hit[0] < hit[2]:
        return hit[1]
    now = _epoch()
    try:
        names, stamp, source = region_list.resolve(
            offering_cache_table(), _describe_regions, REGIONS, now,
            take_lock=lambda: _claim_refresh(now, region_list.CACHE_KEY) == 0)
    except Exception:  # noqa: BLE001 - a failed discovery never fails the request
        logger.warning("Region discovery failed; using the function's own Region", exc_info=True)
        names, stamp, source = region_list.order(REGIONS), None, "configured"
    info = {"regions": names, "fetchedAt": stamp, "source": source}
    # The fallback list is kept briefly, so the container picks up the list soon after
    # another caller (or the next lock window) stores it.
    _regions_mem = (_now(), info, REGIONS_FALLBACK_MEMORY_SECONDS if source == "configured" else MEMORY_TTL_SECONDS)
    return info


def read_regions():
    """The Regions every route covers: the enabled Regions minus the excluded ones."""
    return list(_region_info()["regions"])


# ------------------------------------------------------------- Region names
# The long name of each Region ("Asia Pacific (Tokyo)") from the AWS public SSM parameters, read
# in the function's own Region (they answer for Regions the account has not enabled too). One
# shared cache item holds every name asked so far for a day; each container keeps it for
# MEMORY_TTL_SECONDS. A failed lookup is never stored, and a Region without a name is left out:
# the page shows its code then.
REGION_NAMES_KEY = "regionnames#v1"
REGION_NAMES_TTL_SECONDS = 24 * 3600
REGION_NAME_PARAMETER = "/aws/service/global-infrastructure/regions/{}/longName"
REGION_NAME_RE = re.compile(r"^/aws/service/global-infrastructure/regions/([a-z0-9-]+)/longName$")
# GetParameters takes at most 10 names per call.
SSM_NAMES_PER_CALL = 10
# /api/config waits for this lookup at most once a day; it must not wait for long.
SSM_NAMES_CONFIG = {"retries": {"mode": "standard", "total_max_attempts": 2}, "connect_timeout": 2, "read_timeout": 3}
_region_names_mem = {}  # REGION_NAMES_KEY -> (monotonic time, {"names", "asked", "expires"})


def _names_client():
    """The SSM client of the function's own Region (AWS_REGION), the only Region the role may read
    the parameters in; short timeouts of its own."""
    key = ("ssm#region-names", os.environ.get("AWS_REGION", ""))
    with _clients_lock:
        if key not in _clients:
            _clients[key] = boto3.client("ssm", region_name=key[1] or None, config=Config(**SSM_NAMES_CONFIG))
        return _clients[key]


def _fetch_region_names(codes):
    """{code: long name} for the Region codes, in GetParameters calls of SSM_NAMES_PER_CALL names.
    A Region the parameters do not know is left out. Raises on any failed call."""
    codes = sorted(set(codes))
    batches = [codes[i:i + SSM_NAMES_PER_CALL] for i in range(0, len(codes), SSM_NAMES_PER_CALL)]

    def one(batch):
        resp = _names_client().get_parameters(Names=[REGION_NAME_PARAMETER.format(c) for c in batch])
        found = {}
        for p in resp.get("Parameters", []):
            m = REGION_NAME_RE.match(p.get("Name", ""))
            if m and m.group(1) in batch and isinstance(p.get("Value"), str) and p["Value"]:
                found[m.group(1)] = p["Value"]
        return found

    names = {}
    for found in _cache_map(one, batches):
        names.update(found)
    return names


def _read_region_names(now):
    """{names, asked, expires} of the shared item, or None when missing, expired, or unreadable."""
    item = cache_reads.get_one(offering_cache_table(), REGION_NAMES_KEY)
    if not item or int(item.get("expires", 0)) <= now:
        return None
    try:
        names, asked = json.loads(item["names"]), json.loads(item["asked"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(names, dict) or not isinstance(asked, list):
        return None
    return {"names": {k: v for k, v in names.items() if isinstance(k, str) and isinstance(v, str)},
            "asked": [c for c in asked if isinstance(c, str)], "expires": int(item["expires"])}


def _write_region_names(entry):
    try:
        offering_cache_table().put_item(Item={
            "cacheKey": REGION_NAMES_KEY, "names": json.dumps(entry["names"], sort_keys=True),
            "asked": json.dumps(sorted(entry["asked"])), "expires": entry["expires"],
        })
    except ClientError as err:
        logger.warning("Region name cache write failed (%s)", err.response["Error"]["Code"])


def region_names(regions):
    """{code: long name} for the listed Regions that have one. The container's copy, else the shared
    item; Regions the stored entry has not asked about yet are looked up and added to it. Never
    raises: a failed lookup answers with the names already known (possibly none) and stores nothing."""
    regions = list(regions)
    try:
        now = _epoch()
        hit = _region_names_mem.get(REGION_NAMES_KEY)
        entry = hit[1] if hit and _now() - hit[0] < MEMORY_TTL_SECONDS and hit[1]["expires"] > now else None
        if entry is None:
            entry = _read_region_names(now)
        _count_cache(entry is not None)
        if entry is None:
            entry = {"names": {}, "asked": [], "expires": now + REGION_NAMES_TTL_SECONDS}
        todo = [r for r in regions if r not in entry["asked"]]
        if todo:
            try:
                found = _fetch_region_names(todo)
            except Exception:  # noqa: BLE001 - the names are optional; the config answers without them
                logger.warning("Region name lookup failed", exc_info=True)
                return {r: entry["names"][r] for r in regions if r in entry["names"]}
            entry = {"names": {**entry["names"], **found}, "asked": sorted(set(entry["asked"]) | set(todo)),
                     "expires": entry["expires"]}
            _write_region_names(entry)
        _region_names_mem[REGION_NAMES_KEY] = (_now(), entry)
        return {r: entry["names"][r] for r in regions if r in entry["names"]}
    except Exception:  # noqa: BLE001 - /api/config never fails over the names
        logger.warning("Region names unavailable", exc_info=True)
        return {}


# ---------------------------------------------------------------- validation
def need_region(q):
    """A Region for a read route: any enabled Region (read_regions)."""
    region = q.get("region", "")
    if region not in read_regions():
        raise HttpError(400, "region is not enabled in this deployment", "region-not-enabled")
    return region


def need_probe_region(q):
    """A Region for a test launch: any Region of read_regions (enabled minus excluded)."""
    return need_region(q)


def matches_families(itype):
    """True when the page covers the instance type: a GPU family by the naming rule
    (families.tracked)."""
    return isinstance(itype, str) and bool(TYPE_RE.match(itype)) and families.tracked(itype)


def tracked_description():
    """The page set in words, for an error message."""
    return families.rule_text()


def scored(itype):
    """True when the deployment requests Spot placement scores for the type (spotScoreFamilies)."""
    return matches_families(itype) and families.scored(itype, SPOT_SCORE_FAMILIES)


def need_scored(itype):
    """A Spot score route or alert takes only a type the deployment scores (spotScoreFamilies)."""
    if not scored(itype):
        listed = families.description(SPOT_SCORE_FAMILIES)
        raise HttpError(400, f"this deployment requests Spot placement scores only for {listed}", "sps-not-scored",
                        {"type": itype, "families": listed})
    return itype


def region_offerings(region):
    """Instance types in the allowed families that EC2 offers in the Region, from the Region's
    offering cache item (the one the offering matrix shows)."""
    return _entry_types(_offering_entry(region))


def check_family(itype):
    """Every route that takes an instance type goes through this guard, including POST /api/probe
    before any launch request: the probe role's IAM condition can only match the coarse wildcards
    (families.WILDCARDS), so the naming rule itself is enforced here."""
    if not matches_families(itype):
        tracked = tracked_description()
        raise HttpError(400, f"instanceType must match an enabled family ({tracked})", "type-not-enabled",
                        {"families": tracked})
    return itype


def need_instance_type(q, region, entry=None):
    """The type must match an allowed family and be offered in the Region (`entry`: the Region's
    offering entry when the caller already read it)."""
    itype = check_family(q.get("instanceType", ""))
    offered = _entry_types(entry) if entry is not None else region_offerings(region)
    if itype not in offered:
        raise HttpError(400, f"{itype} is not offered in {region}", "type-not-offered", {"type": itype, "region": region})
    return itype


def need_cb_type(q, region):
    """An instance type for a Capacity Block route or alert: any type the Region's detection says
    supports Capacity Blocks (cb_types), or, while detection has no answer, a type of the
    capacity_blocks.FAMILIES fallback. The spotScoreFamilies setting does not apply here."""
    itype = q.get("instanceType", "")
    if not isinstance(itype, str) or not TYPE_RE.match(itype):
        raise HttpError(400, "instanceType must be an instance type name, such as p5.48xlarge", "bad-parameter",
                        {"name": "instanceType"})
    supported = cb_types(region)
    if not cb.is_supported(itype, supported):
        accepted = ", ".join(cb.FAMILIES if supported is None else sort_families({family_of(t) for t in supported}))
        raise HttpError(400, f"{itype} has no Capacity Blocks in {region}; Capacity Blocks accept {accepted or 'none'}",
                        "cb-unsupported-type", {"type": itype, "families": accepted})
    return itype


def need_user(claims):
    sub = claims.get("sub", "")
    if not sub:
        raise HttpError(403, "the token has no user identity", "no-identity")
    return sub


def int_param(q, name, default, low, high):
    raw = q.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise HttpError(400, f"{name} must be an integer", "bad-parameter", {"name": name})
    if not low <= value <= high:
        raise HttpError(400, f"{name} must be between {low} and {high}", "out-of-range", {"name": name, "low": low, "high": high})
    return value


# -------------------------------------------------------------------- routes
def get_config(_q, _claims):
    info = _region_info()
    now = _epoch()
    state = _groups_state_or_none()
    groups = _scored_groups(state)
    return {
        # Every Region the routes cover: the enabled Regions minus excludeRegions. Test launches run
        # in the same Regions (probeRegions is the same list).
        "regions": list(info["regions"]),
        # {code: long name} for the listed Regions that have one; {} when the lookup failed.
        "regionNames": region_names(info["regions"]),
        "probeRegions": list(info["regions"]),
        "excludeRegions": sorted(region_list.EXCLUDED),
        "regionsFetchedAt": info["fetchedAt"],
        # The page covers every GPU family (trackedFamilyRule); Spot placement scores cover the
        # spotScoreFamilies only: the family names ("p5") and the patterns they come from ("p5.*").
        "trackedFamilyRule": families.rule_text(),
        "spotScoreFamilies": families.names(SPOT_SCORE_FAMILIES),
        "spotScoreFamilyPatterns": list(SPOT_SCORE_FAMILIES),
        # Which types share one Region-level score: one family and one GPU count per instance.
        "spotScoreGrouping": "family+gpus",
        "spotScoreEndpoint": score_groups.ENDPOINT,
        # building: the score groups are being built (first deploy or a new spotScoreFamilies).
        "spotScoreState": _score_state(state),
        "spotScoreGroups": [{"key": k, "label": score_groups.label(k), "family": score_groups.parse_key(k)[0],
                             "gpus": score_groups.parse_key(k)[1], "sizes": sort_types(groups[k]["types"]),
                             "regionCount": len(score_groups.members(groups[k])),
                             "configurations": len(groups[k]["chunks"])}
                            for k in score_groups.sort_keys(groups)],
        "spotScoreConfigsNeeded": sum(len(g["chunks"]) for g in groups.values()),
        # spotScoreConfigsPerDay, spotScoreConfigsInUse, remainingNewConfigs, spotScoreLimitedUntil.
        **_spot_score_budget(now),
        # lastDailyUpdateAt, nextDailyUpdateAt, nextWarmAt, warmMinutes, dailyUpdateUtc.
        **score_groups.schedule_view(state, now),
        "probeEnabled": ENABLE_PROBE,
        "spotProbeEnabled": ENABLE_PROBE and ENABLE_SPOT_PROBE,
        "cooldownMinutes": COOLDOWN_MINUTES,
        "notificationsEnabled": ENABLE_NOTIFICATIONS,
        "notifyIntervalMinutes": NOTIFY_INTERVAL_MINUTES,
        "maxSubscriptionsPerUser": MAX_SUBSCRIPTIONS_PER_USER,
        "offeringCacheHours": OFFERING_CACHE_HOURS,
        "spotScoreCacheMinutes": SPOT_SCORE_CACHE_MINUTES,
        "quotaCacheMinutes": QUOTA_CACHE_MINUTES,
        "apiRateLimit": API_RATE_LIMIT,
        "maxProbesPerUserPerDay": MAX_PROBES_PER_USER_PER_DAY,
        "maxTestLaunchesPerDay": MAX_TEST_LAUNCHES_PER_DAY,
        # The fallback list; the Capacity Block routes and alerts use each Region's detected types.
        "capacityBlockFamilies": list(cb.FAMILIES),
        "capacityBlockMaxCount": cb.MAX_COUNT,
        "priceCache": {"onDemandHours": OD_PRICE_TTL_SECONDS // 3600, "spotMinutes": SPOT_PRICE_TTL_SECONDS // 60},
    }


def _spot_score_budget(now):
    """The governor's numbers for /api/config; never fails the request (no table: nothing left)."""
    try:
        return spot_configs.budget_view(offering_cache_table(), SPOT_SCORE_BUDGET, now)
    except Exception:  # noqa: BLE001 - the config answer goes on without the budget
        logger.warning("Spot score budget unavailable", exc_info=True)
        return spot_configs.budget_view(None, SPOT_SCORE_BUDGET, now)


def bool_param(q, name):
    raw = q.get(name, "false")
    if raw not in ("true", "false"):
        raise HttpError(400, f"{name} must be true or false", "bad-parameter", {"name": name})
    return raw == "true"


_cb_types_mem = {}


def cb_types(region):
    """The instance types that support Capacity Blocks in the Region (capacity_blocks.supported_types:
    detected once a day, cached under cbtypes#<Region>), or None when unknown, so callers fall back to
    capacity_blocks.FAMILIES. The container keeps each answer, None too, for MEMORY_TTL_SECONDS."""
    hit = _cb_types_mem.get(region)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS:
        return hit[1]
    types = cb.supported_types(offering_cache_table(), lambda r: client("ec2", r), region, _epoch(),
                               iso(datetime.now(timezone.utc)))
    if len(_cb_types_mem) > MEMORY_CACHE_MAX:
        _cb_types_mem.clear()
    _cb_types_mem[region] = (_now(), types)
    return types


def get_instance_types(q, _claims):
    """The tracked types EC2 offers in the Region; with capacityBlocks=true, every type that
    supports Capacity Blocks there instead, whatever the page tracks: the Region's detected
    list (cb_types), or, while detection has no answer, the offered types of the
    capacity_blocks.FAMILIES fallback (blockTypes of the offering item; the tracked ones for an
    item written before that field)."""
    region = need_region(q)
    only_blocks = bool_param(q, "capacityBlocks")
    entry = _offering_entry(region)
    types = _entry_types(entry)
    if only_blocks:
        supported = cb_types(region)
        if supported is not None:
            types = sort_types(set(supported))
        elif entry.get("blockTypes") is not None:
            types = sort_types(entry["blockTypes"])
        else:
            types = [t for t in types if cb.is_family(t)]
    return {"region": region, "instanceTypes": types, "fetchedAt": entry["fetchedAt"],
            "fromCache": entry["fromCache"], **_stale_info(entry)}


def get_instance_type_zones(q, _claims):
    region = need_region(q)
    check_family(q.get("instanceType", ""))
    # One read of the Region's item, so fromCache describes this answer, not the validation.
    entry = _offering_entry(region)
    itype = need_instance_type(q, region, entry)
    return {"region": region, "instanceType": itype, "zones": list(entry["zones"].get(itype, [])),
            "fetchedAt": entry["fetchedAt"], "fromCache": entry["fromCache"], **_stale_info(entry)}


def need_days(q):
    days = int_param(q, "days", 1, 1, 182)
    if not alerts.valid_days(days):
        raise HttpError(400, BAD_DAYS_MESSAGE, "bad-days")
    return days


def date_param(q, name, now):
    """A UTC calendar day (YYYY-MM-DD) as the datetime at its start, or None."""
    raw = q.get(name)
    if raw in (None, ""):
        return None
    try:
        if not DATE_RE.match(raw):
            raise ValueError(raw)
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HttpError(400, f"{name} must be a date in the form YYYY-MM-DD", "bad-date", {"name": name})
    if day > now + timedelta(days=DATE_MAX_DAYS):
        raise HttpError(400, f"{name} must be within {DATE_MAX_DAYS} days from today", "date-too-far",
                        {"name": name, "days": DATE_MAX_DAYS})
    return day


def _offering_error(err):
    return HttpError(400, err.message, err.code)


CB_THROTTLED_MESSAGE = "EC2 is throttling Capacity Block requests; try again in a few seconds"


def _cb_search(*args, **kwargs):
    """cb.search for the Capacity Block routes: an EC2 rejection is a 400 with its cb-* code, and EC2
    throttling (after botocore's own retries, no more here) is a 429 "throttled" the page retries.
    Every other ClientError goes on to the generic 502 "aws-error" answer."""
    try:
        return cb.search(*args, **kwargs)
    except ClientError as err:
        if cb.is_throttle(err):
            raise HttpError(429, CB_THROTTLED_MESSAGE, cb.THROTTLED)
        raise


def get_capacity_blocks(q, _claims):
    region = need_region(q)
    itype = need_cb_type(q, region)
    count = int_param(q, "count", 1, 1, cb.MAX_COUNT)
    days = need_days(q)
    now = datetime.now(timezone.utc)
    # No window by default: EC2 then searches its own range. startDate is a UTC calendar day, at
    # most START_DATE_MAX_DAYS days after today (UTC); a start date in the past starts now.
    start = date_param(q, "startDate", now)
    if start is not None:
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if start > today + timedelta(days=START_DATE_MAX_DAYS):
            raise HttpError(400, f"startDate must be a UTC day at most {START_DATE_MAX_DAYS} days from today",
                            "bad-parameter", {"name": "startDate"})
        start = max(start, now)
    end = date_param(q, "endDate", now)
    if end is not None:
        end += timedelta(days=1)  # the end of that day
        if end <= (start or now):
            raise HttpError(400, "endDate must be after startDate and after today", "bad-date", {"name": "endDate"})
    try:
        offerings, notes = _cb_search(client("ec2", region), itype, count, days * 24,
                                      start=start, end=end, max_items=CB_MAX_ITEMS)
    except cb.OfferingError as err:
        raise _offering_error(err)
    body = {"region": region, "instanceType": itype, "offerings": offerings}
    if notes:
        # `note` is the English text; `notes` carries a code and placeholders per note for the page.
        body["note"] = " ".join(n["message"] for n in notes)
        body["notes"] = notes
    return body


COUNT_RE = re.compile(r"0|[1-9][0-9]?")


def bracket_params(q):
    """The optional lo and hi of a max capacity follow-up request: both or neither, plain decimal
    integers with 0 <= lo < hi <= 65 (cb.MAX_COUNT + 1). Anything else is a 400 "bad-parameter"
    before any EC2 call, so a client cannot make a request do more work."""
    raw = {name: q.get(name) for name in ("lo", "hi")}
    given = {name for name, value in raw.items() if value not in (None, "")}
    if not given:
        return None, None
    if given != {"lo", "hi"}:
        name = ({"lo", "hi"} - given).pop()
        raise HttpError(400, "lo and hi go together: send both or neither", "bad-parameter", {"name": name})
    values = {}
    for name in ("lo", "hi"):
        text = raw[name]
        if not isinstance(text, str) or not COUNT_RE.fullmatch(text) or int(text) > cb.MAX_COUNT + 1:
            raise HttpError(400, f"{name} must be an integer from 0 to {cb.MAX_COUNT + 1}", "bad-parameter", {"name": name})
        values[name] = int(text)
    if values["lo"] >= values["hi"]:
        raise HttpError(400, "lo must be less than hi", "bad-parameter", {"name": "lo"})
    return values["lo"], values["hi"]


def get_capacity_block_max(q, _claims):
    """Largest instance count (1 to 64) with an offering, split into short stateless requests
    (cb.search_stage): without lo and hi, stage 1 asks 7 counts in one parallel round; with the lo
    and hi of the previous answer, a follow-up runs at most 2 rounds. Each answer is either the
    result (done: true) or the narrowed bracket (done: false, lo, hi) to send next. For any answer
    the search takes at most 3 requests and 16 EC2 calls; one request makes at most 14."""
    region = need_region(q)
    itype = need_cb_type(q, region)
    days = need_days(q)
    lo, hi = bracket_params(q)
    ec2 = client("ec2", region)
    calls = []
    lock = threading.Lock()

    def available(count):
        with lock:
            calls.append(count)
        try:
            return _cb_search(ec2, itype, count, days * 24, max_items=1000, paginate=False)[0]
        except cb.OfferingError as err:
            # A count EC2 does not accept for this type has no offering.
            if err.code == cb.INVALID_COUNT:
                return []
            raise _offering_error(err)

    done, lo, hi, found = cb.search_stage(available, lo, hi)
    body = {"region": region, "instanceType": itype, "days": days, "done": done}
    if not done:
        body.update({"lo": lo, "hi": hi, "probed": len(found), "callsUsed": len(calls)})
        return body
    body["maxCount"] = lo
    if lo:
        body["earliest"] = cb.summary(min(found[lo], key=lambda o: o["StartDate"]))
    return body


# ------------------------------------------------------------ forced refresh
def _refresh_lock_key(item):
    return f"{REFRESH_LOCK_PREFIX}{item}"


def _claim_refresh(now, item):
    """Take the forced-refresh slot of one cached item with a conditional write. Returns 0 when this
    call took it, else the whole seconds until the running window ends (1 to REFRESH_WINDOW_SECONDS).
    `item` names the cached item, for example "quota#us-east-1"; the lock is "#lock#" + item."""
    if _take_lock(now, item):
        return 0
    try:
        held = offering_cache_table().get_item(Key={"cacheKey": _refresh_lock_key(item)}, ConsistentRead=True).get("Item")
    except (ClientError, BotoCoreError):
        held = None
    return _lock_wait(held, now)


def _take_lock(now, item):
    """The conditional write of one forced-refresh lock: True when this call took it. A write that
    fails for another reason (throttled, timed out) is logged and counts as a held lock, so the
    item is served from the cache instead of failing the whole request."""
    try:
        offering_cache_table().put_item(
            Item={"cacheKey": _refresh_lock_key(item), "lockedUntil": now + REFRESH_WINDOW_SECONDS,
                  "expires": now + REFRESH_WINDOW_SECONDS + 86400},
            ConditionExpression="attribute_not_exists(cacheKey) OR lockedUntil <= :now",
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as err:
        if not _conditional_failed(err):
            logger.warning("refresh lock write failed for %s (%s); serving the cached answer", item,
                           err.response.get("Error", {}).get("Code", ""))
    except BotoCoreError as err:
        logger.warning("refresh lock write failed for %s (%s); serving the cached answer", item, type(err).__name__)
    return False


def _lock_wait(held, now):
    """Seconds until a held lock's window ends (1 to REFRESH_WINDOW_SECONDS). The slot frees at
    lockedUntil; without a readable lock the whole window is the safe answer."""
    try:
        until = int((held or {}).get("lockedUntil", 0))
    except (TypeError, ValueError):
        until = 0
    left = until - now
    return left if 1 <= left <= REFRESH_WINDOW_SECONDS else REFRESH_WINDOW_SECONDS


def _claim_refreshes(now, items):
    """[_claim_refresh(now, item) for item in items]: the conditional writes on the cache pool, then
    one strongly consistent batched read of the locks this call did not take."""
    items = list(items)
    taken = _cache_map(lambda i: _take_lock(now, i), items)
    held = [i for i, ok in zip(items, taken) if not ok]
    locks = cache_reads.get_items(offering_cache_table(), [_refresh_lock_key(i) for i in held], consistent=True) if held else {}
    return [0 if ok else _lock_wait(locks.get(_refresh_lock_key(i)), now) for i, ok in zip(items, taken)]


def _refresh_info(asked, retry, from_cache):
    """The refresh fields of one cached item: none unless refresh=true was asked. refreshed is true
    when the answer came from AWS in this request; a blocked refresh served from the cache says
    how long until its window ends."""
    if not asked:
        return {}
    info = {"refreshed": not from_cache}
    if from_cache and retry:
        info["retryAfterSeconds"] = retry
    return info


# -------------------------------------------------------------- spot scores
# The score updater (score_updater.py) builds the score groups, grants configurations and keeps
# their answers in the cache every 15 minutes. These routes read those items. A configuration
# without a current answer is repeated here only when AWS accepted it in the last day (a free
# repeat); a Refresh (refresh=true) repeats a group's granted configurations, once per group per
# REFRESH_WINDOW_SECONDS. Nothing here grants a configuration or changes a group.
SPS_MESSAGES = {
    "sps-limited": "AWS restricts how many different Spot placement score requests an account can make; this score "
                   "updates after the next daily update",
    "throttled": "EC2 is throttling Spot placement score requests; try again in a few seconds",
}
SPS_TIMEOUT = {"code": "sps-timeout", "message": "Spot placement scores did not arrive in time; try again"}
# A Refresh of a group none of whose configurations may be repeated now (planned or limited).
SPS_NOT_REPEATED = {"code": "sps-limited", "message": SPS_MESSAGES["sps-limited"]}
GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}/\d{1,3}$")
# The score updater is started at most once per this many seconds while the groups are being built.
BOOTSTRAP_LOCK_SECONDS = 600
BOOTSTRAP_LOCK = "#lock#score-bootstrap"
_STATE_MEM_KEY = "#sps-groups"


def sps_error(err):
    """{code, message} for a failed GetSpotPlacementScores call: sps-limited (refused by AWS for the
    configuration restriction), throttled (retry with backoff), sps-unsupported, or sps-error."""
    if isinstance(err, SpsTimeout):
        return dict(SPS_TIMEOUT)
    if isinstance(err, ClientError):
        aws_code = err.response["Error"].get("Code", "")
        text = err.response["Error"].get("Message", "")
        if spot_cache.is_limit_error(err):
            return {"code": "sps-limited", "message": SPS_MESSAGES["sps-limited"]}
        if aws_code in SPS_THROTTLE_ERRORS or SPS_THROTTLE_RE.search(text):
            return {"code": "throttled", "message": SPS_MESSAGES["throttled"]}
        if aws_code in SPS_UNSUPPORTED_ERRORS:
            return {"code": "sps-unsupported",
                    "message": f"EC2 does not return Spot placement scores for this instance type ({aws_code})"}
        return {"code": "sps-error", "message": f"AWS call failed ({aws_code})"}
    return {"code": "sps-error", "message": f"AWS call failed ({type(err).__name__})"}


class SpsTimeout(Exception):
    """A GetSpotPlacementScores call did not finish within the request's budget."""


def _sps_lock_item(key):
    """The Refresh lock name of one score group."""
    return f"sps#{key}"


def _groups_state():
    """The stored score groups (score_groups.read_state), kept MEMORY_TTL_SECONDS in the
    container; None when the item cannot be read."""
    hit = _sps_mem.get(_STATE_MEM_KEY)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS:
        return hit[1]
    state = score_groups.read_state(offering_cache_table())
    if state is not None:
        _sps_mem[_STATE_MEM_KEY] = (_now(), state, float("inf"))
    return state


def _groups_state_or_none():
    try:
        return _groups_state()
    except Exception:  # noqa: BLE001 - /api/config never fails over the groups
        logger.warning("Spot score groups unavailable", exc_info=True)
        return None


def _scored_groups(state):
    """The stored groups whose sizes the deployment still scores (a deploy that dropped a family
    shows its groups no more, even before the updater rebuilds)."""
    groups = (state or {}).get("groups") or {}
    return {k: g for k, g in groups.items() if any(scored(t) for t in g["types"])}


def _score_state(state):
    """ready, building (the groups are being built: first deploy or a new spotScoreFamilies), or
    unavailable (the stored groups cannot be read)."""
    if state is None:
        return "unavailable"
    return "building" if score_groups.needs_bootstrap(state, SPOT_SCORE_FAMILIES) else "ready"


def _request_bootstrap(now):
    """Start the score updater once (asynchronous invoke) while the groups must be built; at most
    once per BOOTSTRAP_LOCK_SECONDS across all containers. Never fails the request."""
    if not SCORE_UPDATER_FUNCTION:
        return False
    try:
        offering_cache_table().put_item(
            Item={"cacheKey": BOOTSTRAP_LOCK, "lockedUntil": now + BOOTSTRAP_LOCK_SECONDS,
                  "expires": now + BOOTSTRAP_LOCK_SECONDS + 86400},
            ConditionExpression="attribute_not_exists(cacheKey) OR lockedUntil <= :now",
            ExpressionAttributeValues={":now": now})
    except ClientError as err:
        if not _conditional_failed(err):
            logger.warning("score bootstrap lock failed (%s)", err.response["Error"]["Code"])
        return False
    try:
        client("lambda", HOME_REGION).invoke(FunctionName=SCORE_UPDATER_FUNCTION, InvocationType="Event",
                                             Payload=json.dumps({"mode": "bootstrap"}).encode())
        return True
    except (ClientError, BotoCoreError) as err:
        logger.warning("could not start the score updater (%s)", type(err).__name__)
        return False


def _sps_cache_key(key, chunk):
    return spot_cache.cache_key(key, chunk)


def _sps_memory(key, now):
    """The container's copy of one configuration's item, or None when absent, older than
    MEMORY_TTL_SECONDS, or past the item's own expiry."""
    hit = _sps_mem.get(key)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS and hit[2] > now:
        return {"scores": dict(hit[1]["scores"]), "fetchedAt": hit[1]["fetchedAt"]}
    return None


def _read_sps_entries(wanted, now):
    """{pair: entry} for the pairs of `wanted` ({pair: (group key, chunk)}) whose item is current:
    the container's copies first, then one batched read of the rest (cache_reads.get_items)."""
    found, keys = {}, {}
    for pair, (key, chunk) in wanted.items():
        name = _sps_cache_key(key, chunk)
        hit = _sps_memory(name, now)
        if hit is not None:
            _count_cache(True)
            found[pair] = hit
        else:
            keys[pair] = name
    items = cache_reads.get_items(offering_cache_table(), keys.values()) if keys else {}
    for pair, name in keys.items():
        entry, expires = spot_cache.parse_item(items.get(name), wanted[pair][1], now)
        _count_cache(entry is not None)
        if entry is not None:
            _remember_sps(name, entry, expires)
            found[pair] = entry
    return found


def _remember_sps(key, entry, expires):
    if len(_sps_mem) > MEMORY_CACHE_MAX:
        _sps_mem.clear()
    _sps_mem[key] = (_now(), {"scores": dict(entry["scores"]), "fetchedAt": entry["fetchedAt"]}, expires)


def _forget_sps(key):
    """A Refresh that took the group's lock drops the container's copies before asking EC2."""
    for name in [k for k in _sps_mem if k.startswith(f"sps#{key}#")]:
        _sps_mem.pop(name, None)


def _write_sps(key, chunk, scores, stamp, now):
    minutes = SPOT_SCORE_CACHE_MINUTES + SPS_GRACE_MINUTES
    _remember_sps(_sps_cache_key(key, chunk), {"scores": scores, "fetchedAt": stamp}, now + minutes * 60)
    spot_cache.write(offering_cache_table(), key, chunk, scores, stamp, now, minutes)


# The cache items outlive spotScoreCacheMinutes by this much, as the score updater writes them.
SPS_GRACE_MINUTES = 10


def _region_spot_scores(cfg):
    """{region: 1..10} of one configuration, in one request to its own endpoint."""
    return spot_configs.fetch(client("ec2", cfg.endpoint), cfg)


def _ledger(now):
    """The registry and governor view of one request (spot_configs.Ledger)."""
    return spot_configs.Ledger(offering_cache_table(), now, score_groups.ENDPOINT, SPOT_SCORE_BUDGET)


def _run_sps(jobs, deadline):
    """{fp: scores or the exception} for jobs {fp: Config}, on the heatmap pool; calls past
    `deadline` count as SpsTimeout and their results are dropped."""
    if not jobs:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(HEATMAP_WORKERS, len(jobs)))
    futures = {pool.submit(_region_spot_scores, cfg): fp for fp, cfg in jobs.items()}
    timeout = None if deadline is None else max(0.0, deadline - _now())
    done, _ = wait(futures, timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    out = {}
    for future, fp in futures.items():
        cfg = jobs[fp]
        if future not in done:
            logger.warning("spot placement scores timed out for %s", cfg.itype)
            out[fp] = SpsTimeout()
            continue
        err = future.exception()
        if err is not None:
            code = err.response["Error"].get("Code") if isinstance(err, ClientError) else type(err).__name__
            logger.warning("spot placement scores failed for %s (%s)", cfg.itype, code)
            out[fp] = err
            continue
        out[fp] = future.result()
    return out


def _pending_regions(gpus, shown, members, now):
    """{group key: [Region]} for the groups of `members` ({key: set of member Regions}, the selected
    groups only): Regions that sell a size of the group (the API's cached offering items, no lookup;
    a missing item adds nothing) but are not in its stored list yet. They join at the next daily
    update. A size of a group that is not selected (a subset request) is skipped."""
    try:
        items = cache_reads.get_items(offering_cache_table(), [_cache_key(r) for r in shown])
    except Exception:  # noqa: BLE001 - the pending Regions never fail a score answer
        logger.warning("offering items unavailable for the pending Regions", exc_info=True)
        items = {}
    entries = {r: e for r in shown for e in [_parse_offering_item(items.get(_cache_key(r)), now)] if e is not None}
    out = {}
    for region in shown:
        entry = entries.get(region)
        if entry is None:
            continue
        for itype in _entry_types(entry):
            if not scored(itype):
                continue
            key = score_groups.group_key(itype, gpus)
            if key in members and region not in members[key]:
                out.setdefault(key, set()).add(region)
    return {k: sorted(v) for k, v in out.items()}


def _price_items(shown, now):
    """{region: {type: {zone: price}}} from the cached Spot price items (no lookup)."""
    keys = {r: spot_prices.cache_key(r) for r in shown}
    try:
        items = cache_reads.get_items(offering_cache_table(), keys.values())
    except Exception:  # noqa: BLE001 - prices never fail a score answer
        logger.warning("Spot price items unavailable", exc_info=True)
        return {}
    out = {}
    for region, key in keys.items():
        entry = spot_prices.parse_item(items.get(key), now)
        if entry is not None:
            out[region] = entry["data"]
    return out


def _group_histories(keys, fresh, now):
    """{group key: last readings}: the ones this request just stored, else one batched read."""
    out = {k: fresh[k] for k in keys if k in fresh}
    names = {k: spot_cache.history_key(k, spot_cache.HISTORY_REGIONS) for k in keys if k not in out}
    try:
        items = cache_reads.get_items(offering_cache_table(), names.values()) if names else {}
    except Exception as err:  # noqa: BLE001 - the history never fails the answer
        logger.warning("spot score history read failed (%s)", type(err).__name__)
        items = {}
    for k, name in names.items():
        out[k] = spot_cache.parse_history(items.get(name), now)
    return {k: spot_cache.shown(out[k]) for k in keys}


def _score_groups_answer(keys, now, asked=False, deadline=None):
    """The Spot score answer for the score groups `keys` (None: every group)."""
    state = _groups_state()
    if state is None:
        raise HttpError(502, "the Spot score groups cannot be read; try again", "sps-state-unavailable")
    status = _score_state(state)
    if status == "building":
        _request_bootstrap(now)
    groups = _scored_groups(state)
    selected = [k for k in score_groups.sort_keys(groups) if keys is None or k in keys]
    shown = read_regions()
    scope = set(shown)
    members = {k: score_groups.members(groups[k]) for k in selected}
    cfgs = {k: [spot_configs.build(k, c, score_groups.ENDPOINT, types=groups[k]["types"]) for c in groups[k]["chunks"]]
            for k in selected}
    every = [c for k in selected for c in cfgs[k]]
    retries = dict(zip(selected, _claim_refreshes(now, [_sps_lock_item(k) for k in selected]))) if asked else {}
    live = {k for k, v in retries.items() if v == 0}
    for k in live:
        _forget_sps(k)
    # Every group's stored answers, also of a group whose Refresh took its lock (read from the table,
    # not the container's copy): when its repeat fails or is not allowed, the stored answer stands.
    cached = _read_sps_entries({c.fp: (c.itype, list(c.regions)) for c in every}, now)
    ledger = _ledger(now)
    ledger.load([c.fp for c in every])
    # Repeats only: configurations AWS accepted in the last day, with no current answer or in a
    # group whose Refresh took its lock. REPEAT_ONLY never creates one.
    wanted = [c for c in every if (c.itype in live or c.fp not in cached) and ledger.warm(c)]
    allowed = ledger.plan(wanted, spot_configs.REPEAT_ONLY) if wanted else set()
    outcomes = _run_sps({c.fp: c for c in wanted if c.fp in allowed}, deadline)
    by_fp = {c.fp: c for c in every}
    ledger.record_all([(by_fp[fp], o) for fp, o in outcomes.items() if not isinstance(o, SpsTimeout)],
                      mapper=_cache_map)
    stamp = iso(datetime.fromtimestamp(now, timezone.utc))
    fetched = {fp: o for fp, o in outcomes.items() if not isinstance(o, BaseException)}
    errors = {fp: o for fp, o in outcomes.items() if isinstance(o, BaseException)}
    _cache_map(lambda fp: _write_sps(by_fp[fp].itype, list(by_fp[fp].regions), fetched[fp], stamp, now), list(fetched))
    # One history reading per group that got an answer from EC2 in this request.
    fresh_history = {}
    answered = {}
    for k in selected:
        if any(c.fp in fetched for c in cfgs[k]):
            part = {}
            for c in cfgs[k]:
                part.update(fetched.get(c.fp) or (cached.get(c.fp) or {}).get("scores") or {})
            answered[k] = part
    fresh_history = dict(zip(answered, _cache_map(
        lambda k: spot_cache.append_history(offering_cache_table(), spot_cache.history_key(k, spot_cache.HISTORY_REGIONS),
                                            answered[k], stamp, now), list(answered))))
    histories = _group_histories(selected, fresh_history, now)
    pending = _pending_regions(state["gpus"], shown, {k: set(members[k]) for k in selected}, now)
    prices = _price_items(shown, now)
    limited_until = int((ledger.state or {}).get("limitedUntil") or 0)
    out = []
    for k in selected:
        g = groups[k]
        scores, states, configurations, stamps, failures = {}, {}, [], [], []
        for c in cfgs[k]:
            entry = {"scores": fetched[c.fp], "fetchedAt": stamp} if c.fp in fetched else cached.get(c.fp)
            cstate = score_groups.config_state(ledger.entries.get(c.fp), now, limited_until)
            configurations.append({"regions": [r for r in c.regions if r in scope], "state": cstate,
                                   "fetchedAt": entry["fetchedAt"] if entry else None})
            if c.fp in errors:
                failures.append(sps_error(errors[c.fp]))
            if entry is not None:
                stamps.append(entry["fetchedAt"] or stamp)
            for r in c.regions:
                if r not in scope:
                    continue
                if entry is not None and r in entry["scores"]:
                    scores[r] = entry["scores"][r]
                    states[r] = "scored"
                elif entry is not None:
                    states[r] = "no-score"
                else:
                    states[r] = "waiting" if cstate == "active" else cstate
        for r in pending.get(k, []):
            states[r] = "after-daily-update"
        family, gpus = score_groups.parse_key(k)
        view = {
            "key": k, "family": family, "gpus": gpus, "label": score_groups.label(k), "sizes": sort_types(g["types"]),
            "regions": [r for r in members[k] if r in scope],
            "pendingRegions": pending.get(k, []),
            "scores": scores, "regionStates": states,
            "fetchedAt": min(stamps) if stamps else None,
            "fromCache": not any(c.fp in fetched for c in cfgs[k]),
            "configurations": configurations,
            "prices": {r: p for r in shown if r in prices
                       for p in [spot_prices.group_prices(prices[r], g["types"])] if p is not None},
            "history": histories.get(k, []),
        }
        if k in live and view["fromCache"] and not failures:
            # The Refresh took the lock but no configuration of the group may be repeated
            # (planned or limited): the stored answer stands, with the reason.
            failures.append(dict(SPS_NOT_REPEATED))
        if failures:
            view["errors"] = failures
        if asked:
            view["refreshed"] = not view["fromCache"]
            # Only a Refresh the group's lock blocked says when to try again.
            if retries.get(k):
                view["retryAfterSeconds"] = retries[k]
        out.append(view)
    return {
        "generatedAt": stamp, "state": status, "endpoint": score_groups.ENDPOINT, "regions": shown, "groups": out,
        "spotScoreLimitedUntil": score_groups._iso(limited_until) if limited_until > now else None,
        **score_groups.schedule_view(state, now),
    }


def get_spot_scores(q, _claims):
    """Every score group's Region-level Spot placement scores (or the groups of ?group=, or the
    group of ?instanceType=). ?refresh=true repeats the selected groups' granted configurations."""
    if q.get("scope") == "az":
        raise HttpError(400, "Availability Zone Spot placement scores were removed; use a test launch to check a zone",
                        "sps-zone-removed")
    deadline = _now() + HEATMAP_BUDGET_SECONDS
    asked = bool_param(q, "refresh")
    now = _epoch()
    extra = {}
    keys = None
    if q.get("group"):
        keys = list(dict.fromkeys(k.strip() for k in q["group"].split(",") if k.strip()))
        if not keys or len(keys) > HEATMAP_MAX_TYPES or not all(GROUP_KEY_RE.match(k) for k in keys):
            raise HttpError(400, "group must list score group keys such as g6e/4", "bad-parameter", {"name": "group"})
    elif q.get("instanceType"):
        itype = check_family(q.get("instanceType", ""))
        state = _groups_state()
        key = score_groups.group_key(itype, (state or {}).get("gpus")) if scored(itype) else None
        extra = {"instanceType": itype, "group": key,
                 "scoreState": "off" if not scored(itype) else "pending" if key is None else "grouped"}
        keys = [key] if key else []
    answer = _score_groups_answer(keys, now, asked=asked, deadline=deadline)
    return {**answer, **extra}


def _heatmap_types(q):
    """The `types` list: 1 to 30 distinct names, each in an enabled family, in natural order."""
    raw = q.get("types", "")
    types = list(dict.fromkeys(t.strip() for t in raw.split(",") if t.strip()))
    if not types:
        raise HttpError(400, "types must list at least one instance type", "bad-parameter", {"name": "types"})
    if len(types) > HEATMAP_MAX_TYPES:
        raise HttpError(400, f"types accepts at most {HEATMAP_MAX_TYPES} entries", "list-too-long", {"max": HEATMAP_MAX_TYPES})
    for itype in types:
        check_family(itype)
    return sort_types(types)


def get_spot_heatmap(q, _claims):
    """The score of each listed instance type: its score group's (the same items and repeats as
    /api/spot-scores). scoreState per type: scored, none (in a group, no score yet), pending (a
    scored size that joins a group at the next daily update), building, or off (not scored here)."""
    deadline = _now() + HEATMAP_BUDGET_SECONDS
    types = _heatmap_types(q)
    asked = bool_param(q, "refresh")
    now = _epoch()
    state = _groups_state()
    gpus = (state or {}).get("gpus") or {}
    key_of = {t: score_groups.group_key(t, gpus) if scored(t) else None for t in types}
    keys = sorted({k for k in key_of.values() if k})
    answer = _score_groups_answer(keys, now, asked=asked, deadline=deadline)
    by_key = {g["key"]: g for g in answer["groups"]}
    body = {"generatedAt": answer["generatedAt"], "state": answer["state"], "regions": answer["regions"],
            "scores": {}, "fromCache": {}, "fetchedAt": {}, "scoreState": {}, "scoreGroups": {}, "groupLabels": {},
            "history": {}, "errors": {}, "scoreLevel": "family+gpus"}
    for t in types:
        key = key_of[t]
        group = by_key.get(key)
        body["scoreGroups"][t] = key
        if key:
            body["groupLabels"][t] = score_groups.label(key)
        if not scored(t):
            body["scoreState"][t] = "off"
            continue
        if group is None:
            body["scoreState"][t] = "building" if answer["state"] == "building" else "pending"
            continue
        body["scores"][t] = dict(group["scores"])
        body["fromCache"][t] = group["fromCache"]
        body["fetchedAt"][t] = group["fetchedAt"]
        body["history"][t] = group["history"]
        body["scoreState"][t] = "scored" if group["scores"] else "none"
        if group.get("errors"):
            body["errors"][t] = group["errors"][0]
        if asked:
            body.setdefault("refreshed", {})[t] = group.get("refreshed", False)
            if "retryAfterSeconds" in group:
                body.setdefault("retryAfterSeconds", {})[t] = group["retryAfterSeconds"]
    for name in ("nextDailyUpdateAt", "lastDailyUpdateAt", "nextWarmAt", "warmMinutes"):
        body[name] = answer[name]
    return body


# ------------------------------------------------------------------- prices
def _read_shared(key, now):
    """{data, fetchedAt, expires} of a JSON cache item: the container's copy (MEMORY_TTL_SECONDS), then
    the DynamoDB item. None when missing, expired, or unreadable; a failed read only counts as a miss."""
    hit = _price_cache.get(key)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS and hit[1]["expires"] > now:
        _count_cache(True)
        return hit[1]
    entry = _load_shared(key, now)
    _count_cache(entry is not None)
    if entry is not None:
        _remember_shared(key, entry)
    return entry


def _load_shared(key, now):
    try:
        item = offering_cache_table().get_item(Key={"cacheKey": key}).get("Item")
    except ClientError as err:
        logger.warning("cache read failed for %s (%s)", key, err.response["Error"]["Code"])
        return None
    if not item or int(item.get("expires", 0)) <= now:
        return None
    try:
        data = json.loads(item["data"])
    except (KeyError, TypeError, ValueError):
        return None
    expires = int(item["expires"])
    return {"data": data, "fetchedAt": item.get("fetchedAt"), "expires": expires,
            "freshUntil": int(item.get("freshUntil", expires))}


def _remember_shared(key, entry):
    if len(_price_cache) > MEMORY_CACHE_MAX:
        _price_cache.clear()
    _price_cache[key] = (_now(), entry)


def _write_shared(key, data, stamp, now, ttl):
    entry = {"data": data, "fetchedAt": stamp, "expires": now + ttl}
    _remember_shared(key, entry)
    try:
        offering_cache_table().put_item(Item={
            "cacheKey": key, "data": json.dumps(data, sort_keys=True), "fetchedAt": stamp, "expires": now + ttl,
        })
    except ClientError as err:
        logger.warning("cache write failed for %s (%s)", key, err.response["Error"]["Code"])


def _spot_price_key(itype, region):
    return f"price-spot#{itype}#{region}"


def _od_price_key(itype, region):
    return f"price-od#{itype}#{region}"


def _region_spot_prices(region):
    """({type: {zone: price}}, fetchedAt, fromCache) of every scored type the Region sells: the
    shared item (spot_prices, also written by the score updater every warm run), else one
    DescribeSpotPriceHistory call for all of them, fresh for SPOT_PRICE_TTL_SECONDS. The item lives
    spot_prices.GRACE_SECONDS longer for the score groups (_price_items), but past its freshness
    this route looks the prices up again."""
    key = spot_prices.cache_key(region)
    now = _epoch()
    hit = _read_shared(key, now)
    if hit is not None and isinstance(hit["data"], dict) and spot_prices.is_fresh(hit, now):
        return hit["data"], hit["fetchedAt"], True
    types = [t for t in region_offerings(region) if scored(t)]
    stamp = iso(datetime.now(timezone.utc))
    prices = spot_prices.fetch(client("ec2", region), types)
    spot_prices.write(offering_cache_table(), region, prices, stamp, now)
    _remember_shared(key, spot_prices.entry(prices, stamp, now))
    return prices, stamp, False


def get_spot_prices(q, _claims):
    """The latest Spot prices of a Region. Without instanceType: every scored type the Region sells
    ({type: {zone: price}}) and each score group's "from" price. With instanceType: that type's
    prices per zone (a type outside spotScoreFamilies gets one call of its own)."""
    region = need_region(q)
    if q.get("instanceType"):
        itype = need_instance_type(q, region)
        if scored(itype):
            prices, stamp, from_cache = _region_spot_prices(region)
            return {"region": region, "instanceType": itype, "prices": dict(prices.get(itype, {})),
                    "fetchedAt": stamp, "fromCache": from_cache}
        key = _spot_price_key(itype, region)
        now = _epoch()
        hit = _read_shared(key, now)
        if hit is not None and isinstance(hit["data"], dict):
            return {"region": region, "instanceType": itype, "prices": hit["data"],
                    "fetchedAt": hit["fetchedAt"], "fromCache": True}
        stamp = iso(datetime.now(timezone.utc))
        prices = spot_prices.fetch(client("ec2", region), [itype]).get(itype, {})
        _write_shared(key, prices, stamp, now, SPOT_PRICE_TTL_SECONDS)
        return {"region": region, "instanceType": itype, "prices": prices, "fetchedAt": stamp, "fromCache": False}
    prices, stamp, from_cache = _region_spot_prices(region)
    groups = _scored_groups(_groups_state_or_none())
    return {"region": region, "prices": prices, "groups": spot_prices.groups_view(prices, groups),
            "fetchedAt": stamp, "fromCache": from_cache}


def _decimal_text(value):
    """A Decimal as plain text without trailing zeros ("55.0400000000" -> "55.04")."""
    return format(value.normalize(), "f")


def _hourly_usd(product):
    """The lowest positive USD per-hour price of one Price List product's OnDemand terms, or None."""
    if isinstance(product, str):
        try:
            product = json.loads(product)
        except ValueError:
            return None
    found = []
    terms = (product.get("terms") or {}).get("OnDemand") or {}
    for term in terms.values():
        for dim in (term.get("priceDimensions") or {}).values():
            if dim.get("unit") != "Hrs":
                continue
            try:
                value = Decimal(str((dim.get("pricePerUnit") or {})["USD"]))
            except (KeyError, InvalidOperation):
                continue
            if value.is_finite() and value > 0:
                found.append(value)
    return min(found) if found else None


def _fetch_on_demand_price(region, itype):
    """The On-Demand list price per hour (Decimal, USD) for Linux on shared tenancy, or None when the
    Price List has no matching product. Every filter must match (TERM_MATCH filters are ANDed)."""
    filters = [("instanceType", itype), ("regionCode", region), *OD_PRICE_FILTERS]
    pages = client("pricing", PRICING_REGION).get_paginator("get_products").paginate(
        ServiceCode="AmazonEC2",
        Filters=[{"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in filters],
        FormatVersion="aws_v1",
        PaginationConfig={"MaxItems": OD_PRICE_MAX_PRODUCTS},
    )
    prices = [p for page in pages for p in map(_hourly_usd, page.get("PriceList", [])) if p is not None]
    if len(prices) > 1:
        logger.warning("%d On-Demand list prices match %s in %s; using the lowest", len(prices), itype, region)
    return min(prices) if prices else None


def get_on_demand_price(q, _claims):
    """The On-Demand list price (not a quote, no discounts) for Linux on shared tenancy."""
    region = need_region(q)
    itype = need_instance_type(q, region)
    key = _od_price_key(itype, region)
    now = _epoch()
    hit = _read_shared(key, now)
    if hit is not None and isinstance(hit["data"], dict) and isinstance(hit["data"].get("usdPerHour"), (str, type(None))):
        value, stamp, from_cache = hit["data"]["usdPerHour"], hit["fetchedAt"], True
    else:
        stamp = iso(datetime.now(timezone.utc))
        # A failed lookup raises before anything is stored, so errors are never cached.
        price = _fetch_on_demand_price(region, itype)
        value = _decimal_text(price) if price is not None else None
        _write_shared(key, {"usdPerHour": value}, stamp, now,
                      OD_PRICE_TTL_SECONDS if value is not None else OD_PRICE_NOT_FOUND_TTL_SECONDS)
        from_cache = False
    body = {"region": region, "instanceType": itype, "usdPerHour": value, "currency": "USD", "kind": "list-price",
            "fetchedAt": stamp, "fromCache": from_cache}
    if value is None:
        body["reason"] = "not-found"
    return body


def quota_category(name):
    """on-demand, spot, or capacity-block for the quotas the page shows; None for the rest."""
    if CAPACITY_BLOCK_QUOTA in name:
        return "capacity-block"
    if not QUOTA_NAME_RE.search(name):
        return None
    if name.startswith("Running On-Demand"):
        return "on-demand"
    if "Spot Instance Requests" in name:
        return "spot"
    return None


def quota_family(name):
    """The family a GPU quota covers, so the page does not guess from the name: "p", "g", "trn",
    or "inf" for On-Demand and Spot quotas, "cb:<family>" (for example "cb:p5en") for a
    Capacity Block quota. None when the name names no family."""
    if CAPACITY_BLOCK_QUOTA in name:
        found = CAPACITY_BLOCK_QUOTA_RE.search(name)
        return f"cb:{found.group(1).lower()}" if found else None
    found = QUOTA_NAME_RE.search(name)
    if not found:
        return None
    return QUOTA_FAMILIES.get(re.sub(r"\d+$", "", found.group(1)))


def percent(used, value):
    """Share of the quota in use, one decimal. None without usage or with a zero quota."""
    if used is None or not value:
        return None
    return round(used / value * 100, 1)


def _metric_query(qid, metric):
    return {
        "Id": qid,
        "MetricStat": {
            "Metric": {
                "Namespace": metric["MetricNamespace"],
                "MetricName": metric["MetricName"],
                "Dimensions": [{"Name": k, "Value": v} for k, v in sorted(metric.get("MetricDimensions", {}).items())],
            },
            "Period": USAGE_PERIOD_SECONDS,
            "Stat": "Maximum",
        },
        "ReturnData": True,
    }


def _usage(region, rows):
    """Maximum usage over the last hour per row, from CloudWatch. Returns {row index: value}."""
    queries = [(i, row["_metric"]) for i, row in enumerate(rows) if row.get("_metric")]
    if not queries:
        return {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(seconds=USAGE_WINDOW_SECONDS)
    cw = client("cloudwatch", region)
    found = {}
    for offset in range(0, len(queries), METRIC_QUERIES_PER_CALL):
        batch = queries[offset:offset + METRIC_QUERIES_PER_CALL]
        ids = {f"q{i}": i for i, _ in batch}
        pages = cw.get_paginator("get_metric_data").paginate(
            MetricDataQueries=[_metric_query(f"q{i}", m) for i, m in batch], StartTime=start, EndTime=end
        )
        for page in pages:
            for result in page["MetricDataResults"]:
                i = ids.get(result["Id"])
                if i is not None:
                    found.setdefault(i, []).extend(result.get("Values", []))
    # A metric with no data point in the window means nothing was counted: report 0.
    return {i: max(found.get(i) or [0.0]) for i, _ in queries}


def _fetch_quota_rows(region):
    """Live GPU On-Demand and Spot quotas and every Capacity Block quota, with usage."""
    sq = client("service-quotas", region)
    found = {}
    # The two lists (about 18 pages each) are read at the same time; only the GPU quotas are kept.
    # Paginators are made here: a client is thread-safe for calls.
    lists = [(sq.get_paginator(op), applied)
             for op, applied in (("list_aws_default_service_quotas", False), ("list_service_quotas", True))]

    def kept(paginator):
        pages = paginator.paginate(ServiceCode="ec2", PaginationConfig={"PageSize": 100})
        return [(item, category) for page in pages for item in page["Quotas"]
                if (category := quota_category(item["QuotaName"]))]

    with ThreadPoolExecutor(max_workers=QUOTA_LIST_WORKERS) as pool:
        futures = [(pool.submit(kept, paginator), applied) for paginator, applied in lists]
    # Defaults first, then applied values overwrite them; a failure raises the defaults list's error first.
    for future, applied in futures:
        for item, category in future.result():
            old = found.get(item["QuotaCode"], {})
            found[item["QuotaCode"]] = {
                "code": item["QuotaCode"],
                "name": item["QuotaName"],
                "category": category,
                "family": quota_family(item["QuotaName"]),
                "value": item.get("Value"),
                "unit": item.get("Unit"),
                "adjustable": item.get("Adjustable"),
                "applied": applied,
                "_metric": item.get("UsageMetric") or old.get("_metric"),
            }
    rows = sorted(found.values(), key=lambda x: (QUOTA_CATEGORIES.index(x["category"]), x["name"]))
    usage_error = aws_code = None
    try:
        usage = _usage(region, rows)
    except (ClientError, BotoCoreError) as err:
        # A slow or unreachable CloudWatch endpoint (BotoCoreError) marks usage unavailable, like an AWS error.
        logger.exception("usage metrics failed in %s", region)
        aws_code = err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__
        usage, usage_error = {}, f"usage metrics unavailable ({aws_code})"
    for i, row in enumerate(rows):
        has_metric = bool(row.pop("_metric"))
        row["used"] = usage.get(i) if has_metric else None
        row["percent"] = percent(row["used"], row["value"])
    result = {"quotas": rows, "usageError": usage_error}
    if usage_error:
        result["usageErrorCode"] = "usage-unavailable"
        result["usageErrorParams"] = {"awsCode": aws_code}
    result["fetchedAt"] = iso(datetime.now(timezone.utc))
    result["fromCache"] = False
    return result


def _quota_cache_key(region):
    return f"quota#{region}"


def _read_quota_cache(region, now):
    """The Region's cached quota result, or None when missing, expired, or unreadable."""
    try:
        item = offering_cache_table().get_item(Key={"cacheKey": _quota_cache_key(region)}).get("Item")
    except ClientError as err:
        logger.warning("quota cache read failed for %s (%s)", region, err.response["Error"]["Code"])
        return None
    if not item or int(item.get("expires", 0)) <= now:
        return None
    try:
        rows = json.loads(item["rows"])
    except (KeyError, ValueError):
        return None
    if not isinstance(rows, dict) or not isinstance(rows.get("quotas"), list):
        return None
    kept = []
    for row in rows["quotas"]:
        if isinstance(row, dict) and _old_dl_row(row):
            continue
        # Items written before rows carried a family get it from the name.
        if isinstance(row, dict) and "family" not in row and isinstance(row.get("name"), str):
            row["family"] = quota_family(row["name"])
        kept.append(row)
    return {**rows, "quotas": kept, "fetchedAt": item.get("fetchedAt"), "fromCache": True}


def _old_dl_row(row):
    """A DL quota row from an item cached before the DL quotas were left out."""
    if "family" in row:
        return row["family"] == "dl"
    name = row.get("name")
    return isinstance(name, str) and CAPACITY_BLOCK_QUOTA not in name and bool(OLD_DL_QUOTA_RE.search(name))


def _write_quota_cache(region, result, now):
    rows = {k: v for k, v in result.items() if k not in ("fetchedAt", "fromCache")}
    try:
        offering_cache_table().put_item(Item={
            "cacheKey": _quota_cache_key(region),
            "rows": json.dumps(rows, sort_keys=True),
            "fetchedAt": result["fetchedAt"],
            "expires": now + QUOTA_CACHE_MINUTES * 60,
        })
    except ClientError as err:
        logger.warning("quota cache write failed for %s (%s)", region, err.response["Error"]["Code"])


def _cached_quota_rows(region, now):
    """The in-memory copy (60 seconds per container), then the DynamoDB item."""
    hit = _quota_cache.get(region)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS:
        _count_cache(True)
        return {**hit[1], "fromCache": True}
    cached = _read_quota_cache(region, now)
    _count_cache(cached is not None)
    if cached is not None:
        _remember_quota(region, cached)
    return cached


def _remember_quota(region, result):
    if len(_quota_cache) > MEMORY_CACHE_MAX:
        _quota_cache.clear()
    _quota_cache[region] = (_now(), result)


def _store_quota_rows(region, result, now):
    _remember_quota(region, result)
    # A result without usage stays only in memory, so a passing metrics error does not stick for minutes.
    if not result.get("usageError"):
        _write_quota_cache(region, result, now)


def _quota_rows(region, live=False):
    """One Region's quota result: cached unless `live`, else fetched and stored."""
    now = _epoch()
    cached = None if live else _cached_quota_rows(region, now)
    if cached is not None:
        return cached
    result = _fetch_quota_rows(region)
    _store_quota_rows(region, result, now)
    return result


def get_quotas(q, _claims):
    region = need_region(q)
    asked = bool_param(q, "refresh")
    retry = _claim_refresh(_epoch(), _quota_cache_key(region)) if asked else None
    result = _quota_rows(region, live=retry == 0)
    return {"region": region, **result, **_refresh_info(asked, retry, result["fromCache"])}


def region_error(err):
    """Short text for one Region's failure; Regions the account has not enabled say so."""
    if isinstance(err, ClientError):
        code = err.response["Error"]["Code"]
        if code in NOT_ENABLED_ERRORS:
            return NOT_ENABLED
        return f"AWS call failed ({code})"
    return "the Region did not answer"


def region_error_code(err):
    """The machine code and placeholders for region_error's text, so the page can show its own words."""
    if isinstance(err, ClientError):
        code = err.response["Error"]["Code"]
        if code in NOT_ENABLED_ERRORS:
            return NOT_ENABLED_CODE, None
        return "aws-error", {"awsCode": code}
    return "no-answer", None


def region_failure(region, err):
    """One Region's entry in a Region-wide answer: the English text, its code, and the code's placeholders."""
    code, params = region_error_code(err)
    entry = {"region": region, "error": region_error(err), "errorCode": code}
    if params:
        entry["errorParams"] = params
    return entry


# One unreachable Region (its endpoint does not answer) must not make the whole request wait: the
# fan-out has a time budget, and a Region that timed out is skipped for a few minutes. The marker
# lives in this container and in the cache table ("#down#<Region>", with the same time as its TTL),
# so one container's timeout spares the others instead of every container paying it once.
# Only a Region that gave no answer at all is marked: an endpoint that cannot be reached, or no
# answer within the fan-out's own budget. A Region that answered with an error, throttling included,
# is not marked, and a marker never means "this Region sells nothing".
EACH_REGION_BUDGET_SECONDS = 12
REGION_DOWN_SECONDS = 300
REGION_DOWN_PREFIX = "#down#"
_region_down = {}  # Region -> epoch seconds until which it is skipped
_UNREACHABLE = (ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)


def _region_down_key(region):
    return f"{REGION_DOWN_PREFIX}{region}"


def _mark_region_down(region):
    """Remember that the Region gave no answer, in this container and in the shared cache item. A
    failed write costs only the sharing: this container still skips the Region."""
    until = int(time.time() + REGION_DOWN_SECONDS)
    _region_down[region] = until
    try:
        offering_cache_table().put_item(
            Item={"cacheKey": _region_down_key(region), "downUntil": until, "expires": until})
    except Exception:  # noqa: BLE001 - the marker saves time; it is never part of an answer
        logger.warning("could not store the no-answer marker for %s", region, exc_info=True)


def _regions_down(regions, now):
    """The Regions to skip: this container's own markers, then one batched read of the shared items
    for the rest. A marker that cannot be read skips nothing, so an unknown Region is always tried."""
    down = {r for r in regions if _region_down.get(r, 0) > now}
    rest = [r for r in regions if r not in down]
    if not rest:
        return down
    try:
        items = cache_reads.get_items(offering_cache_table(), [_region_down_key(r) for r in rest])
    except Exception:  # noqa: BLE001 - without the markers every Region is tried
        logger.warning("no-answer markers unavailable", exc_info=True)
        return down
    for region in rest:
        try:
            until = int((items.get(_region_down_key(region)) or {}).get("downUntil", 0))
        except (TypeError, ValueError):
            continue
        if until > now:
            down.add(region)
            # Keep it here too, so the next request in this container needs no read.
            _region_down[region] = until
    return down


def each_region(fn, regions=None, budget=None, mark_pending_down=True):
    """Run fn(region) for each Region (default: every enabled Region) on a small thread pool. A
    failure stays in its Region. Regions that have not answered when the time budget ends, or whose
    endpoint is unreachable, come back as "the Region did not answer" and are skipped for a while.
    `mark_pending_down` is False for a short refresh of data the request already has: a Region that
    misses such a small budget is not a sign that its endpoint is down."""
    regions = list(read_regions() if regions is None else regions)
    budget = EACH_REGION_BUDGET_SECONDS if budget is None else budget

    def one(region):
        try:
            return {"region": region, **fn(region)}
        except Exception as err:  # one Region's failure must not fail the others
            if isinstance(err, _UNREACHABLE):
                _mark_region_down(region)
                logger.warning("Region %s did not answer: %s", region, type(err).__name__)
            else:
                logger.exception("call failed in %s", region)
            return region_failure(region, err)

    if not regions:
        return []
    results = {r: region_failure(r, TimeoutError()) for r in _regions_down(regions, time.time())}
    todo = [r for r in regions if r not in results]
    if todo:
        pool = ThreadPoolExecutor(max_workers=min(REGION_WORKERS, len(todo)))
        futures = {pool.submit(one, r): r for r in todo}
        done, pending = wait(futures, timeout=budget)
        for f in done:
            results[futures[f]] = f.result()
        for f in pending:
            region = futures[f]
            if mark_pending_down:
                _mark_region_down(region)
            logger.warning("Region %s did not answer within %s s", region, budget)
            results[region] = region_failure(region, TimeoutError())
        pool.shutdown(wait=False, cancel_futures=True)
    return [results[r] for r in regions]


# ---------------------------------------------------------- offering matrix
# One DynamoDB item per Region (offerings#<Region>) holds the Region-level types grouped by family,
# the Availability Zones per type, and the names of every accelerator family the Region offers. The matrix, the instance-type lists, and the validation of
# every route read it, so they all show the same answer with the same fetchedAt.
def family_of(itype):
    return families.family_of(itype)


# Every accelerator family a Region offers, by the AWS naming rule: g<digit>, p<digit>, trn<digit>,
# inf<digit> (gr6 and similar names are dropped). The quota tips list them per quota group, also for
# the Trn and Inf families the page leaves out, so these patterns are wider than families.WILDCARDS.
ACCELERATOR_PATTERNS = ("p*", "g*", "trn*", "inf*")
ACCELERATOR_FAMILY_RE = re.compile(r"^(g|p|trn|inf)\d")


def region_offering_patterns():
    """The Region-level call asks for the accelerator families and the tracked ones at once, so one
    answer (about 75 types, one page) serves both lists; matches_families then narrows it in code."""
    return list(dict.fromkeys([*ACCELERATOR_PATTERNS, *families.filter_values()]))


def tracked_tag():
    """Identifies the page set in a cached offering item: an item built for another set is fetched
    again instead of read (families.cache_tag)."""
    return families.cache_tag()


def accelerator_families(types):
    """The accelerator family names among the instance types, in natural order."""
    return sort_families({family_of(t) for t in types if ACCELERATOR_FAMILY_RE.match(family_of(t))})


def _fetch_region_offerings(region):
    """Live DescribeInstanceTypeOfferings for the Region: the Region-level types grouped by family
    and the zone names per type, both for the tracked families, plus every accelerator family the
    Region offers (accelerators). Two paginated calls; the Region-level one asks for the wider
    accelerator wildcards too, and matches_families narrows the answer."""
    filters = [{"Name": "instance-type", "Values": families.filter_values()}]
    pager = client("ec2", region).get_paginator("describe_instance_type_offerings")
    wide = [{"Name": "instance-type", "Values": region_offering_patterns()}]
    types = {o["InstanceType"] for page in pager.paginate(LocationType="region", Filters=wide)
             for o in page["InstanceTypeOfferings"]}
    zones = {}
    for page in pager.paginate(LocationType="availability-zone", Filters=filters):
        for o in page["InstanceTypeOfferings"]:
            if matches_families(o["InstanceType"]):
                zones.setdefault(o["InstanceType"], set()).add(o["Location"])
    grouped = {}
    for itype in sort_types(types):
        if matches_families(itype):
            grouped.setdefault(family_of(itype), []).append(itype)
    return {"families": grouped, "zones": {t: sorted(zones[t]) for t in sort_types(zones)},
            "accelerators": accelerator_families(types),
            # The offered types of the Capacity Block fallback families, whatever the page tracks
            # lists (the Capacity Blocks tab lists them when the Region's detection has no answer).
            "blockTypes": sort_types(t for t in types if cb.is_family(t))}


def _ordered_families(grouped):
    """Families in natural order, each with its sizes in natural order (cached items may predate it)."""
    return {f: sort_types(grouped[f]) for f in sort_families(grouped)}


def _entry_types(entry):
    """Every tracked instance type of an offering entry, in natural order."""
    return sort_types(t for sizes in entry["families"].values() for t in sizes if matches_families(t))


def _cache_key(region):
    return f"offerings#{region}"


def _read_cached(region, now):
    """The Region's cached offerings, or None when missing, past its max-stale bound, made for
    another tracked set, or written before the item held zones (such an item is fetched again and
    replaced)."""
    try:
        item = offering_cache_table().get_item(Key={"cacheKey": _cache_key(region)}).get("Item")
    except ClientError:
        logger.exception("offering cache read failed for %s", region)
        return None
    return _parse_offering_item(item, now)


def _parse_offering_item(item, now):
    """_read_cached's answer for a stored item (None for a missing one); the batched reads share it.
    An item past its freshness window ("refreshAfter") but inside its max-stale bound ("expires")
    carries stale: True, so the caller answers from it and asks AWS again."""
    if not item or int(item.get("expires", 0)) <= now or item.get("patterns") != tracked_tag():
        return None
    try:
        grouped = json.loads(item["data"])
        zones = json.loads(item["zones"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(grouped, dict) or not isinstance(zones, dict):
        return None
    entry = {"families": grouped, "zones": zones, "accelerators": _parse_accelerators(item.get("accelerators")),
             "blockTypes": _parse_accelerators(item.get("blockTypes")), "fetchedAt": item.get("fetchedAt"), "fromCache": True}
    if _past_freshness(item, now):
        entry["stale"] = True
    return entry


def _past_freshness(item, now):
    """True when the item's freshness window has ended. An item written before the window existed
    has no "refreshAfter" and counts as fresh until it expires."""
    try:
        after = item.get("refreshAfter")
        return after is not None and int(after) <= now
    except (TypeError, ValueError):
        return False


def _stale_info(entry):
    """The stale field of an answer: present only for data served past its freshness window, so the
    page can tell that the age it shows is being worked on."""
    return {"stale": True} if entry.get("stale") else {}


def _parse_accelerators(raw):
    """The stored accelerator family names, or None (unknown) for an item written before the field
    existed or one that does not parse; such an item gets the field at its normal refresh."""
    try:
        names = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    return names


def _write_cached(region, entry, now):
    try:
        offering_cache_table().put_item(Item={
            "cacheKey": _cache_key(region),
            "data": json.dumps(entry["families"], sort_keys=True),
            "zones": json.dumps(entry["zones"], sort_keys=True),
            **({"accelerators": json.dumps(entry["accelerators"])} if entry.get("accelerators") is not None else {}),
            **({"blockTypes": json.dumps(entry["blockTypes"])} if entry.get("blockTypes") is not None else {}),
            "fetchedAt": entry["fetchedAt"],
            "patterns": tracked_tag(),
            # The freshness window, then the max-stale bound (also the table's TTL).
            "refreshAfter": now + OFFERING_CACHE_HOURS * 3600,
            "expires": now + OFFERING_KEEP_SECONDS,
        })
    except ClientError:
        logger.exception("offering cache write failed for %s", region)


def _cached_offerings(region, now):
    """The container's copy (MEMORY_TTL_SECONDS), then the DynamoDB item; None on a miss."""
    hit = _offering_memory(region)
    if hit is not None:
        _count_cache(True)
        return hit
    cached = _read_cached(region, now)
    _count_cache(cached is not None)
    if cached is not None:
        _remember_offerings(region, cached)
    return cached


def _offering_memory(region):
    hit = _offering_mem.get(region)
    if hit and _now() - hit[0] < MEMORY_TTL_SECONDS:
        return {**hit[1], "fromCache": True}
    return None


def _cached_offerings_many(regions, now):
    """{region: entry} for the Regions with a usable cached entry: the container's copies first,
    then one batched read of the other Regions' items (cache_reads.get_items). Each Region counts
    as one cache hit or miss, as in _cached_offerings."""
    found, missing = {}, []
    for region in regions:
        hit = _offering_memory(region)
        if hit is not None:
            _count_cache(True)
            found[region] = hit
        else:
            missing.append(region)
    items = cache_reads.get_items(offering_cache_table(), [_cache_key(r) for r in missing]) if missing else {}
    for region in missing:
        cached = _parse_offering_item(items.get(_cache_key(region)), now)
        _count_cache(cached is not None)
        if cached is not None:
            _remember_offerings(region, cached)
            found[region] = cached
    return {r: found[r] for r in regions if r in found}


def _remember_offerings(region, entry):
    if len(_offering_mem) > MEMORY_CACHE_MAX:
        _offering_mem.clear()
    _offering_mem[region] = (_now(), {k: v for k, v in entry.items() if k != "fromCache"})


def _store_offerings(region, entry, now):
    _remember_offerings(region, entry)
    _write_cached(region, entry, now)


def _offering_entry(region):
    """One Region's offerings {families, zones, fetchedAt, fromCache}: cached, else fetched live
    and stored. A cached item past its freshness window is the answer, and the request asks AWS
    again within a small budget (_revalidate_offerings). A failed lookup raises (the route answers
    with an error) and is not cached."""
    now = _epoch()
    cached = _cached_offerings(region, now)
    if cached is not None and not cached.get("stale"):
        return cached
    stamp = iso(datetime.now(timezone.utc))
    if cached is not None:
        return _revalidate_offerings([region], now, stamp).get(region, cached)
    entry = {**_fetch_region_offerings(region), "fetchedAt": stamp, "fromCache": False}
    _store_offerings(region, entry, now)
    return entry


def _lock_taken(now, item):
    """_take_lock without raising: a lock claim that fails only means this caller does not refresh."""
    try:
        return _take_lock(now, item)
    except Exception:  # noqa: BLE001 - the refresh is optional; the stale answer stands
        logger.warning("refresh lock failed for %s", item, exc_info=True)
        return False


def _revalidate_offerings(regions, now, stamp):
    """{region: fresh entry} for the stale Regions this request asked AWS about again. One caller
    per item and refresh window does the work (the item's own forced-refresh lock), within
    _revalidate_budget() seconds, so the others answer from the stale item at once. A Region that
    does not answer in time keeps its stale item, which the answer already carries, and is not
    marked as a Region that gave no answer: a failed refresh never becomes an empty offering list."""
    budget = _revalidate_budget()
    if not budget:
        return {}
    taken = [r for r, ok in zip(regions, _cache_map(lambda r: _lock_taken(now, _cache_key(r)), regions)) if ok]
    if not taken:
        return {}
    found = {}
    for e in each_region(lambda r: {**_fetch_region_offerings(r), "fetchedAt": stamp, "fromCache": False},
                         taken, budget=budget, mark_pending_down=False):
        region = e.pop("region")
        if "error" not in e:
            _store_offerings(region, e, now)
            found[region] = e
    return found


def _offering_entries(regions, live=()):
    """{region: entry} for several Regions; Regions in `live` skip both cache layers. The cache
    items are read in one batch (_cached_offerings_many) and the EC2 calls of the misses run on
    the Region pool. An item past its freshness window is the answer right away and is asked about
    again in the same request (_revalidate_offerings). A Region whose lookup fails gets a
    region_failure entry and is not cached."""
    now = _epoch()
    found = _cached_offerings_many([r for r in regions if r not in live], now)
    stamp = iso(datetime.now(timezone.utc))
    for e in each_region(lambda r: {**_fetch_region_offerings(r), "fetchedAt": stamp, "fromCache": False},
                         [r for r in regions if r not in found]):
        region = e.pop("region")
        if "error" not in e:
            _store_offerings(region, e, now)
        found[region] = e
    stale = [r for r in regions if found.get(r, {}).get("stale")]
    if stale:
        found.update(_revalidate_offerings(stale, now, stamp))
    return found


def get_offering_matrix(q, _claims):
    asked = bool_param(q, "refresh")
    now = _epoch()
    names = read_regions()
    # One forced-refresh lock per Region item; a Region whose window still runs is served from the cache.
    retries = dict(zip(names, _claim_refreshes(now, [_cache_key(r) for r in names]))) if asked else {}
    entries = _offering_entries(names, live={r for r, v in retries.items() if v == 0})
    regions = []
    for region in names:
        entry = entries[region]
        if "error" in entry:
            regions.append({"region": region, **entry})
            continue
        regions.append({"region": region, "families": _ordered_families(entry["families"]),
                        "fetchedAt": entry["fetchedAt"], "fromCache": entry["fromCache"], **_stale_info(entry),
                        **_refresh_info(asked, retries.get(region), entry["fromCache"])})
    ok = [e for e in regions if "error" not in e]
    return {
        "generatedAt": iso(datetime.now(timezone.utc)),
        "fromCache": bool(ok) and all(e["fromCache"] for e in ok),
        "cacheHours": OFFERING_CACHE_HOURS,
        "regions": regions,
        "families": sort_families({f for e in ok for f in e["families"]}),
        # Every accelerator family the Regions offer (also the ones not tracked), for the quota tips;
        # Regions whose item predates the field add none until their normal refresh.
        "acceleratorFamilies": sort_families({f for r in names if "error" not in entries[r]
                                              for f in entries[r].get("accelerators") or []}),
    }


PROBE_QUERY_MAX_PAGES = 20


def _recent_probes(region, since):
    """Probe records for the Region since `since`, oldest first.

    The query reads newest first and follows LastEvaluatedKey for up to PROBE_QUERY_MAX_PAGES pages
    (1 MB each). If the window holds more than that, the oldest records are the ones left out.
    """
    items = []
    kwargs = {
        "KeyConditionExpression": "#r = :r AND sk >= :s",
        "ExpressionAttributeNames": {"#r": "region"},
        "ExpressionAttributeValues": {":r": region, ":s": iso(since)},
        "ScanIndexForward": False,
    }
    for _ in range(PROBE_QUERY_MAX_PAGES):
        resp = table().query(**kwargs)
        items += resp["Items"]
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    items.reverse()
    return items


OBSERVED_HOURS = 24
# A Region's default subnets are asked again after this many seconds (a Region without one: sooner,
# so a default VPC created meanwhile is found quickly).
DEFAULT_SUBNETS_TTL_SECONDS = 600
NO_DEFAULT_VPC_TTL_SECONDS = 60
NO_DEFAULT_VPC_MESSAGE = ("No default VPC in this Region: test launches run in the default VPC "
                          "(create one with `aws ec2 create-default-vpc`)")


def _score_fields(item):
    """The Spot placement score stored with a probe record: score (1-10 or None), scoreFetchedAt
    (the fetch time of the score data used), scoreSource (live, cache or none). New records store
    none (zone scores were removed); older records keep theirs."""
    score = (item or {}).get("score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    source = (item or {}).get("scoreSource")
    return {"score": score, "scoreFetchedAt": (item or {}).get("scoreFetchedAt"),
            "scoreSource": source if source in ("live", "cache") else "none"}


def _record_group(item, gpus):
    """The score group of a probe record: the stored one, else (a record of an earlier version, one
    instance type per launch) the group of its type by the stored GPU counts, else None."""
    if item.get("group"):
        return item["group"]
    itype = item.get("type")
    return score_groups.group_key(itype, gpus) if itype else None


def _observed(items, since, gpus):
    """The launch results of the records since `since`, across every user, one row per score group
    (or instance type, for a type without a group) and market: counts per result and where the
    launched instances landed (zone and size). No user ids. Each record is one launch request EC2
    answered (cooldown answers and running launches write none)."""
    rows = {}
    start = iso(since)
    for item in items:
        ts = item["sk"].split("#", 1)[0]
        if ts < start:
            continue
        group = _record_group(item, gpus)
        target = group or item.get("target") or item.get("type")
        mode = item.get("mode")
        row = rows.setdefault((target, mode), {
            "group": group, "label": score_groups.label(group) if group else None,
            "instanceType": None if group else target, "market": mode,
            "launched": 0, "noCapacity": 0, "quota": 0, "other": 0, "total": 0, "landed": {}, "last": None})
        result = item.get("result")
        row[{"launched": "launched", "no-capacity": "noCapacity", "quota": "quota"}.get(result, "other")] += 1
        row["total"] += 1
        if result == "launched" and item.get("az") and item.get("type"):
            spot = (item["az"], item["type"])
            row["landed"][spot] = row["landed"].get(spot, 0) + 1
        if row["last"] is None or ts > row["last"]["ts"]:
            row["last"] = {"ts": ts, "result": result, "errorCode": item.get("errorCode")}
    out = []
    for (target, mode), row in sorted(rows.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        row["landed"] = [{"az": az, "instanceType": t, "count": n} for (az, t), n in sorted(row["landed"].items())]
        out.append(row)
    return out


def get_probes(q, claims):
    """Today's test launch counters (the caller's and all users') with their caps, the Region's
    default VPC state, and the observed launch results of the last 24 hours per score group and
    market in the Region (all users, no user ids)."""
    region = need_region(q)
    now = _utcnow()
    since = now - timedelta(hours=OBSERVED_HOURS)
    state = _groups_state_or_none()
    items = _recent_probes(region, since)
    zones = _default_vpc_view(region)
    return {"region": region, "generatedAt": iso(now), "observedHours": OBSERVED_HOURS,
            "observed": _observed(items, since, (state or {}).get("gpus") or {}),
            "today": _today(claims.get("sub"), now), "cooldownMinutes": COOLDOWN_MINUTES, **zones}


def _type_info(region, types):
    """{type: {"vcpus": n, "arch": "x86_64" | "arm64"}} from DescribeInstanceTypes (one call),
    cached per type for the container's life. A failed lookup returns what is cached (a type left
    out has unknown vCPUs and the x86_64 image) and is not cached, so the next launch asks again."""
    missing = [t for t in types if t not in _arch_cache]
    if missing:
        try:
            resp = client("ec2", region).describe_instance_types(InstanceTypes=missing)
            if len(_arch_cache) > MEMORY_CACHE_MAX:
                _arch_cache.clear()
            for t in resp.get("InstanceTypes") or []:
                archs = (t.get("ProcessorInfo") or {}).get("SupportedArchitectures") or []
                vcpus = (t.get("VCpuInfo") or {}).get("DefaultVCpus")
                _arch_cache[t["InstanceType"]] = {
                    "arch": next((a for a in ("x86_64", "arm64") if a in archs), DEFAULT_ARCHITECTURE),
                    "vcpus": int(vcpus) if vcpus is not None else None}
        except (ClientError, BotoCoreError, KeyError, TypeError, ValueError) as err:
            logger.warning("instance type lookup failed for %s (%s)", missing, type(err).__name__)
    return {t: _arch_cache[t] for t in types if t in _arch_cache}


def _ami_id(region, arch=DEFAULT_ARCHITECTURE):
    """The latest Amazon Linux 2023 image for the architecture in the Region (SSM public parameter)."""
    key = (region, arch)
    if key not in _ami_cache:
        resp = client("ssm", region).get_parameter(Name=AMI_PARAMETERS[arch])
        _ami_cache[key] = resp["Parameter"]["Value"]
    return _ami_cache[key]


_subnet_cache = {}


def _default_subnets(region):
    """({zone: subnet id}, VPC id) of the Region's default subnets, kept in the container for a few
    minutes. Raises the 400 no-default-vpc when the Region has none."""
    hit = _subnet_cache.get(region)
    if hit and _now() < hit[0]:
        found = hit[1]
    else:
        try:
            found = fleet_launch.default_subnets(client("ec2", region))
            ttl = DEFAULT_SUBNETS_TTL_SECONDS
        except fleet_launch.NoDefaultVpc:
            found, ttl = None, NO_DEFAULT_VPC_TTL_SECONDS
        if len(_subnet_cache) > MEMORY_CACHE_MAX:
            _subnet_cache.clear()
        _subnet_cache[region] = (_now() + ttl, found)
    if found is None:
        raise HttpError(400, NO_DEFAULT_VPC_MESSAGE, "no-default-vpc", {"region": region})
    return found


def _default_vpc_view(region):
    """defaultVpc (true, false, or null when the lookup failed) and the zones of its default subnets,
    so the page can turn the test launch off in a Region without a default VPC."""
    try:
        subnets, _vpc = _default_subnets(region)
        return {"defaultVpc": True, "defaultSubnetZones": sorted(subnets)}
    except HttpError:
        return {"defaultVpc": False, "defaultSubnetZones": []}
    except (ClientError, BotoCoreError) as err:
        logger.warning("default subnet lookup failed in %s (%s)", region, type(err).__name__)
        return {"defaultVpc": None, "defaultSubnetZones": []}


_sg_cache = {}


def _closed_group(groups, vpc_id):
    """The first tagged group with no inbound rules. A tagged group that allows inbound traffic
    (for example a group someone tagged by hand) is never used for a probe."""
    for group in groups:
        if not group.get("IpPermissions"):
            return group["GroupId"]
    raise HttpError(
        409,
        f"the security group tagged {PROBE_SG_TAG}=true in {vpc_id} has inbound rules; remove the rules "
        f"or delete the group, then try again",
        "probe-sg-open", {"group": groups[0]["GroupId"], "vpc": vpc_id, "tag": PROBE_SG_TAG},
    )


def _probe_security_group(region, vpc_id):
    """Dedicated security group with no inbound rules, so probes never use the VPC default group."""
    key = (region, vpc_id)
    if key in _sg_cache:
        return _sg_cache[key]
    ec2 = client("ec2", region)
    filters = [
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": f"tag:{PROBE_SG_TAG}", "Values": ["true"]},
    ]
    groups = ec2.describe_security_groups(Filters=filters)["SecurityGroups"]
    if groups:
        _sg_cache[key] = _closed_group(groups, vpc_id)
        return _sg_cache[key]
    try:
        group_id = ec2.create_security_group(
            GroupName=PROBE_SG_NAME,
            Description="No inbound rules. Used by short-lived capacity probes.",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [{"Key": PROBE_SG_TAG, "Value": "true"}]}],
        )["GroupId"]
    except ClientError as err:
        if err.response["Error"]["Code"] != "InvalidGroup.Duplicate":
            raise
        # Either a concurrent probe just created the tagged group, or a group with this name
        # exists without the tag (for example made by hand); that one is never used.
        groups = ec2.describe_security_groups(Filters=filters)["SecurityGroups"]
        if not groups:
            raise HttpError(
                409,
                f"a security group named {PROBE_SG_NAME} already exists in {vpc_id} without the "
                f"{PROBE_SG_TAG}=true tag; delete it, then try again",
                "probe-sg-conflict", {"name": PROBE_SG_NAME, "vpc": vpc_id, "tag": PROBE_SG_TAG},
            )
        group_id = _closed_group(groups, vpc_id)
    _sg_cache[key] = group_id
    return group_id


def _quota_token(name):
    found = QUOTA_NAME_RE.search(name or "")
    return found.group(1) if found else None


def _quota_for(rows, family, market):
    """The vCPU quota row that limits `family` in `market` (on-demand or spot), from the Region's
    quota rows (the Quotas tab's data): a per-family row (for example "All P5 Spot Instance
    Requests" for p5, p5e, p5en) when one names the family, else the letter group's row ("Running
    On-Demand P instances"). None when no row applies."""
    letter = (re.match(r"[a-z]+", family) or [""])[0]
    want = {"p": "p", "g": "g"}.get(letter)
    rows = [r for r in rows or [] if isinstance(r, dict) and r.get("category") == market and r.get("family") == want
            and isinstance(r.get("value"), (int, float, Decimal))]
    specific = [r for r in rows if re.search(r"\d", _quota_token(r.get("name")) or "")
                and family.startswith(_quota_token(r["name"]).lower())]
    if specific:
        return max(specific, key=lambda r: len(_quota_token(r["name"])))
    return next((r for r in rows if not re.search(r"\d", _quota_token(r.get("name")) or "x1")), None)


def _available_vcpus(region, family, market):
    """(free vCPUs of the quota that limits the family, the quota row) or (None, None) when unknown:
    no matching row, or the quota lookup failed (then EC2 decides)."""
    try:
        rows = _quota_rows(region)["quotas"]
    except (ClientError, BotoCoreError, KeyError, TypeError) as err:
        logger.warning("quota lookup failed in %s (%s); the test launch tries every size", region, type(err).__name__)
        return None, None
    row = _quota_for(rows, family, market)
    if row is None:
        return None, None
    used = row.get("used") or 0
    return max(0, int(float(row["value"]) - float(used))), row


def run_probe(region, family, sizes, market):
    """One test launch: the sizes that fit the vCPU quota x the default subnets, one instance, then
    terminate. Returns fleet_launch.launch's dict plus `sizes` (tried) and `skipped` (left out by
    the quota). Raises the 400 quota-too-low (no launch request) or no-default-vpc, and the 503
    no-time-left when too little of the invocation is left before an AWS call ahead of CreateFleet
    (nothing launched; the caller gives the counts back)."""
    try:
        return _run_probe(region, family, sizes, market)
    except fleet_launch.NoTimeLeft:
        raise HttpError(503, "not enough time was left to start the test launch safely; nothing was launched, "
                        "try again", "internal", {"reason": "no-time-left"}) from None


def _need_fleet_time(step):
    """Raise fleet_launch.NoTimeLeft before `step` (an AWS call ahead of CreateFleet) when less than
    MIN_SECONDS_FOR_FLEET of the invocation is left: CreateFleet would be refused anyway, and a slow
    endpoint must not run the invocation past its timeout before the counts are given back."""
    left = _time_left()
    if left is not None and left < fleet_launch.MIN_SECONDS_FOR_FLEET:
        logger.warning("test launch stopped before %s: %.1f seconds left of the invocation", step, left)
        raise fleet_launch.NoTimeLeft()


def _run_probe(region, family, sizes, market):
    _need_fleet_time("the instance type lookup")
    info = _type_info(region, sizes)
    vcpus = {t: v["vcpus"] for t, v in info.items()}
    _need_fleet_time("the quota lookup")
    available, row = _available_vcpus(region, family, market)
    fit, skipped = fleet_launch.fitting(sizes, vcpus, available)
    if not fit:
        needed = min(s["vcpus"] for s in skipped)
        raise HttpError(400, f"the {market} vCPU quota for {family} has {available} vCPUs free; the smallest size "
                        f"needs {needed}", "quota-too-low",
                        {"needed": needed, "available": available, "quota": row.get("name"), "quotaCode": row.get("code"),
                         "market": market})
    _need_fleet_time("the default subnet lookup")
    subnets, vpc_id = _default_subnets(region)
    arch = next((info[t]["arch"] for t in fit if t in info), DEFAULT_ARCHITECTURE)
    _need_fleet_time("the image lookup")
    image_id = _ami_id(region, arch)

    def attempt(last):
        try:
            _need_fleet_time("the security group lookup")
            security_group = _probe_security_group(region, vpc_id)
            _need_fleet_time("the launch template")
            return fleet_launch.launch(client("ec2", region), image_id=image_id,
                                       security_group=security_group, subnets=subnets,
                                       sizes=fit, market=market, time_left=_time_left,
                                       single_try=lambda connect, read: _single_try_ec2(region, connect, read))
        except ClientError as err:
            # CreateLaunchTemplate refused the group: nothing was sent to CreateFleet.
            if last or err.response["Error"]["Code"] != SG_GONE:
                raise
            return None
    out = attempt(False)
    if out is None or _group_gone(out):
        # The kept security group id is stale (the group was deleted, for example by another
        # deployment's teardown): look it up or create it again, and try once more.
        logger.warning("probe security group of %s in %s is gone; trying once more", vpc_id, region)
        _sg_cache.pop((region, vpc_id), None)
        try:
            retry = attempt(True)
        except Exception as err:
            if out is None:
                raise
            # The first request reached EC2 (it counts); its answer stands.
            logger.warning("the security group retry in %s failed (%s); the first answer stands", region,
                           type(err).__name__)
            retry = None
        # A retry that sent nothing does not replace a first answer that reached EC2.
        if retry is not None and (out is None or not retry.get("refund")):
            out = retry
    return {**out, "sizes": fit, "skipped": skipped}


SG_GONE = "InvalidGroup.NotFound"


def _group_gone(out):
    """True when EC2 launched nothing because the security group does not exist."""
    return not out["instanceIds"] and (out["errorCode"] == SG_GONE or any(e["code"] == SG_GONE for e in out["errors"]))


def _single_try_ec2(region, connect_timeout, read_timeout):
    """An EC2 client with ONE attempt (no retry) and the given timeouts, for CreateFleet and the
    calls after an unknown outcome (fleet_launch.launch): a retry or a slow answer must not run
    past the invocation's deadline. Kept per Region and timeouts (whole seconds)."""
    key = ("ec2-single-try", region, int(connect_timeout), int(read_timeout))
    with _clients_lock:
        if key not in _clients:
            _clients[key] = boto3.client("ec2", region_name=region, config=Config(
                retries={"mode": "standard", "total_max_attempts": 1}, connect_timeout=int(connect_timeout),
                read_timeout=int(read_timeout)))
        return _clients[key]


def classify_error(code):
    """Map an EC2 error code to a coarse probe result."""
    return fleet_launch.classify(code)


def _probe_target(body, region):
    """(group key or None, single size or None, sizes to try, family) of a test launch. `group`
    ("g6e/4") tries every size of a stored score group that the Region sells; `instanceType` tries
    that one size (any tracked type the Region sells; its group, when it has one, is recorded). The
    GPU naming rule is checked first, before any AWS call."""
    group, itype = body.get("group"), body.get("instanceType")
    if group not in (None, ""):
        if not isinstance(group, str) or not GROUP_KEY_RE.match(group):
            raise HttpError(400, "group must be a score group key such as g6e/4", "bad-parameter", {"name": "group"})
        family = score_groups.parse_key(group)[0]
        check_family(f"{family}.x")
        state = _groups_state()
        stored = ((state or {}).get("groups") or {}).get(group)
        if not stored:
            raise HttpError(400, f"{group} is not a score group of this deployment", "unknown-group", {"group": group})
        offered = set(region_offerings(region))
        sizes = [t for t in sort_types(stored["types"]) if t in offered and matches_families(t)]
        if not sizes:
            raise HttpError(400, f"no size of {score_groups.label(group)} is offered in {region}", "group-not-offered",
                            {"group": group, "region": region})
        return group, None, sizes, family
    if itype in (None, ""):
        raise HttpError(400, "give group or instanceType", "bad-parameter", {"name": "group"})
    itype = need_instance_type(body, region)
    state = _groups_state_or_none()
    return score_groups.group_key(itype, (state or {}).get("gpus") or {}), itype, [itype], family_of(itype)


def need_market(body):
    """market (or the older name mode): on-demand or spot."""
    market = body.get("market", body.get("mode", ""))
    if market not in fleet_launch.MARKETS:
        raise HttpError(400, "market must be on-demand or spot", "bad-parameter", {"name": "market"})
    if market == "spot" and not ENABLE_SPOT_PROBE:
        raise HttpError(403, "Spot launch probes are disabled in this deployment", "spot-probes-disabled")
    return market


def _target_key(group, itype):
    """What the cooldown and the history key on: the score group, or the size when one size of a
    group is chosen (a size test does not hide the group's result, nor the other way round)."""
    return f"{group}:{itype}" if group and itype else (group or itype)


def _record_view(item):
    """A stored record as a test launch answer (cooldown hit)."""
    obtained = {"az": item["az"], "instanceType": item.get("type")} if item.get("result") == "launched" and item.get("az") else None
    return {"cached": True, "result": item.get("result"), "errorCode": item.get("errorCode"),
            "ts": item["sk"].split("#", 1)[0], "obtained": obtained, "sizes": list(item.get("sizes") or []),
            **_score_fields(item)}


def _cached_probe(region, target, market, now):
    for item in reversed(_recent_probes(region, now - timedelta(minutes=COOLDOWN_MINUTES))):
        if (item.get("target"), item.get("mode")) == (target, market):
            return _record_view(item)
    return None


def _lock_key(region, target, market):
    # "#" sorts before the timestamps, so history queries never read lock items.
    return {"region": region, "sk": f"#lock#{target}#{market}"}


def _claim_probe(region, target, market):
    """Take the cooldown slot with a conditional write. Only one request per slot launches."""
    now = _epoch()
    until = now + COOLDOWN_MINUTES * 60
    try:
        table().put_item(
            Item={**_lock_key(region, target, market), "lockedUntil": until, "expires": until + 86400},
            ConditionExpression="attribute_not_exists(sk) OR lockedUntil < :now",
            ExpressionAttributeValues={":now": now},
        )
        return True
    except ClientError as err:
        if not _conditional_failed(err):
            raise
        return False


def _release_probe(region, target, market):
    """Free the slot when the probe failed before it launched, so a retry can run."""
    try:
        table().put_item(Item={**_lock_key(region, target, market), "lockedUntil": 0, "expires": _epoch() + 86400})
    except ClientError:
        logger.exception("could not release the probe slot")


def _utcnow():
    return datetime.now(timezone.utc)


def _probe_count_key(user, day):
    return {"region": f"{PROBE_COUNT_PREFIX}{user}", "sk": day}


def _all_count_key(day):
    """The deployment-wide counter of one UTC day (every user's launches together)."""
    return {"region": f"{ALL_COUNT_PREFIX}{day}", "sk": "count"}


def _probe_day(now):
    """The UTC calendar day of `now` (YYYY-MM-DD) and the counter's expiry: the end of that day plus two days."""
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%d"), int((start + timedelta(days=3)).timestamp())


def _daily_reset(now):
    """When the counts start again: the next 00:00 UTC (ISO 8601, to the second)."""
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (start + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _daily_limit_error(now):
    n = MAX_PROBES_PER_USER_PER_DAY
    return HttpError(
        429,
        # The page shows err.probeDailyLimit with the reset time in local time; this English text is the fallback.
        f"You reached your limit of {n} test launches for today: every test launch you start counts, in any "
        "Region or market and also when it finds no capacity, because a test launch can start a billed GPU "
        "instance for a short time and this limit protects against unexpected charges. The count resets at "
        "00:00 UTC; an administrator can change the limit with the maxTestLaunchesPerUserPerDay setting at deploy "
        "time (0 turns the limit off).",
        "probe-daily-limit", {"limit": n, "resetAt": _daily_reset(now)})


def _daily_cap_all_error(now):
    n = MAX_TEST_LAUNCHES_PER_DAY
    return HttpError(
        429,
        f"This deployment reached its limit of {n} test launches for today (all users together). The count "
        "resets at 00:00 UTC; an administrator can change the limit with the maxTestLaunchesPerDay setting at "
        "deploy time (0 turns the limit off).",
        "daily-cap-all", {"limit": n, "resetAt": _daily_reset(now)})


def _take_count(key, limit, expires):
    """ADD 1 to a counter item while it is below `limit` (one conditional update). False when the
    limit is reached."""
    try:
        table().update_item(
            Key=key,
            UpdateExpression="ADD #c :one SET expires = :exp",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":one": 1, ":exp": expires, ":limit": limit},
        )
    except ClientError as err:
        if not _conditional_failed(err):
            raise
        return False
    return True


def _refund_count(key):
    """Give one count back when the launch failed before its request reached EC2."""
    try:
        table().update_item(
            Key=key,
            UpdateExpression="ADD #c :neg",
            ConditionExpression="#c > :zero",
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={":neg": -1, ":zero": 0},
        )
    except ClientError as err:
        logger.warning("could not refund a daily test launch count (%s)", err.response["Error"]["Code"])


def _take_daily_probe(user, now):
    """Count one launch for the user's UTC day with one conditional update. Returns the day key
    to refund, or None when the cap is off. Raises the 429 when the cap is reached."""
    if MAX_PROBES_PER_USER_PER_DAY <= 0:
        return None
    day, expires = _probe_day(now)
    if not _take_count(_probe_count_key(user, day), MAX_PROBES_PER_USER_PER_DAY, expires):
        raise _daily_limit_error(now)
    return day


def _refund_daily_probe(user, day):
    if day is not None:
        _refund_count(_probe_count_key(user, day))


def _take_daily_all(now):
    """Count one launch on the deployment-wide counter of the UTC day (maxTestLaunchesPerDay), taken
    before any EC2 call. Returns the day key to refund, or None when the cap is off. Raises the 429
    daily-cap-all when the cap is reached (also when two requests race for the last slot: the
    conditional update lets only one through)."""
    if MAX_TEST_LAUNCHES_PER_DAY <= 0:
        return None
    day, expires = _probe_day(now)
    if not _take_count(_all_count_key(day), MAX_TEST_LAUNCHES_PER_DAY, expires):
        raise _daily_cap_all_error(now)
    return day


def _refund_daily_all(day):
    if day is not None:
        _refund_count(_all_count_key(day))


def _read_count(key):
    """A counter's value (0 when the item is missing), None when it cannot be read."""
    try:
        # A Query on the counter's key (the function may Query the table; UpdateItem stays the only
        # other action on the counter partitions).
        items = table().query(KeyConditionExpression="#r = :r AND sk = :s", ExpressionAttributeNames={"#r": "region"},
                              ExpressionAttributeValues={":r": key["region"], ":s": key["sk"]}, Limit=1)["Items"]
        return int((items[0] if items else {}).get("count") or 0)
    except (ClientError, BotoCoreError, TypeError, ValueError) as err:
        logger.warning("could not read a daily test launch count (%s)", type(err).__name__)
        return None


def _today(user, now):
    """Today's test launch counts and caps (a cap of 0 is off: its count is null)."""
    day, _ = _probe_day(now)
    mine = _read_count(_probe_count_key(user, day)) if user and MAX_PROBES_PER_USER_PER_DAY > 0 else None
    everyone = _read_count(_all_count_key(day)) if MAX_TEST_LAUNCHES_PER_DAY > 0 else None
    return {"day": day, "user": mine, "userLimit": MAX_PROBES_PER_USER_PER_DAY,
            "all": everyone, "allLimit": MAX_TEST_LAUNCHES_PER_DAY, "resetAt": _daily_reset(now)}


def post_probe(q, claims, body):
    """One test launch. Guards in order, each before any EC2 call: the feature switch, the Region,
    the market, the GPU naming rule and the target (group or size), the caller's identity, the
    cooldown (per target and market in the Region: a recent result is returned), the per-user daily
    cap, then the deployment-wide daily cap. Only then the EC2 calls: the quota filter, the default
    subnets, the security group, and the instant fleet. A failure before the fleet request, or a
    fleet request that was not sent or that EC2 throttled ("refund" in fleet_launch's result: the
    answer is returned but not stored), gives both counts and the cooldown slot back; an unknown
    fleet outcome (fleet_launch) keeps them."""
    if not ENABLE_PROBE:
        raise HttpError(403, "launch probes are disabled in this deployment", "probes-disabled")
    region = need_probe_region(body)
    market = need_market(body)
    group, itype, sizes, family = _probe_target(body, region)
    # The caller's identity: required for the per-user cap; without the cap it only shows the own count.
    user = need_user(claims) if MAX_PROBES_PER_USER_PER_DAY > 0 else claims.get("sub")
    target = _target_key(group, itype)
    view = {"region": region, "group": group, "label": score_groups.label(group) if group else None,
            "instanceType": itype, "market": market}

    now = _utcnow()
    cached = _cached_probe(region, target, market, now)
    if cached:
        return {**view, **cached, "today": _today(user, now)}
    if not _claim_probe(region, target, market):
        # Another request holds the cooldown slot: return its result, or say it is running.
        cached = _cached_probe(region, target, market, now) or {
            "cached": True, "result": "in-progress", "errorCode": None, "ts": None, "obtained": None, "sizes": [],
            **_score_fields(None)}
        return {**view, **cached, "today": _today(user, now)}
    # Only a request that will really launch counts toward the daily caps.
    day = all_day = None
    try:
        day = _take_daily_probe(user, now)
        all_day = _take_daily_all(now)
        out = run_probe(region, family, sizes, market)
    except Exception:
        _release_probe(region, target, market)
        _refund_daily_probe(user, day)
        _refund_daily_all(all_day)
        raise
    if out.get("refund"):
        # Nothing was launched and EC2 did not process the request: no record, so a retry can run.
        _release_probe(region, target, market)
        _refund_daily_probe(user, day)
        _refund_daily_all(all_day)
    else:
        _store_probe(region, target, market, group, itype, claims, now, out)
    return {**view, "cached": False, "result": out["result"], "errorCode": out["errorCode"],
            "errorMessage": out.get("errorMessage"), "ts": iso(now),
            "obtained": out["obtained"], "sizes": out["sizes"], "skipped": out["skipped"], "errors": out["errors"],
            **_score_fields(None), "today": _today(user, now)}


def _store_probe(region, target, market, group, itype, claims, now, out):
    """Store one test launch answer (the history and the cooldown read it)."""
    record = {
        "region": region,
        "sk": f"{iso(now)}#{target}#{market}",
        "target": target, "mode": market, "result": out["result"], "sizes": out["sizes"],
        "user": claims.get("cognito:username", "unknown"),
        "expires": int(time.time()) + RETENTION_DAYS * 86400,
        # Zone-level Spot placement scores were removed: a new record stores no score.
        "scoreSource": "none",
    }
    if group:
        record["group"] = group
    if out["obtained"]:
        record["az"] = out["obtained"]["az"]
        record["type"] = out["obtained"]["instanceType"]
    elif itype:
        record["type"] = itype
    if out["errorCode"]:
        record["errorCode"] = out["errorCode"]
    record = {k: v for k, v in record.items() if v is not None}
    table().put_item(Item=record)


# ------------------------------------------------------------ subscriptions
def need_notifications():
    if not ENABLE_NOTIFICATIONS:
        raise HttpError(403, "email alerts are disabled in this deployment", "alerts-disabled")


def need_email(claims):
    """The address comes only from the ID token, never from the request."""
    email = claims.get("email", "")
    if str(claims.get("email_verified", "")).lower() != "true":
        raise HttpError(403, "your email address is not verified", "email-not-verified")
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.match(email):
        raise HttpError(403, "the token has no usable email address", "email-missing")
    return email


def body_int(body, name, low, high):
    raw = body.get(name)
    if raw in (None, ""):
        raise HttpError(400, f"{name} is required", "bad-parameter", {"name": name})
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise HttpError(400, f"{name} must be an integer", "bad-parameter", {"name": name})
    return int_param({name: str(raw)}, name, None, low, high)


def _subscription_view(item):
    name = alerts.PARAMS[item["signal"]][0]
    state, reason = alerts.state_view(item)
    view = {
        "id": item["sk"],
        "region": item["region"],
        "instanceType": item["instanceType"],
        "signal": item["signal"],
        name: int(item[name]),
        "lastState": item.get("lastState", alerts.PENDING),
        # state is what the page shows; reason is EC2's machine code when the check was rejected.
        "state": state,
        "reason": reason,
        "lastCheckedAt": item.get("lastCheckedAt"),
        "createdAt": item.get("createdAt"),
    }
    if item["signal"] == alerts.CAPACITY_BLOCK:
        view["count"] = alerts.count_of(item)
    return view


def _user_items(user):
    """The caller's profile record (or None) and alert items. At most a few dozen items."""
    resp = subscriptions_table().query(
        KeyConditionExpression="userId = :u", ExpressionAttributeValues={":u": user}, ConsistentRead=True
    )
    items = resp["Items"]
    profile = next((i for i in items if i["sk"] == alerts.PROFILE_SK), None)
    subs = [i for i in items if alerts.is_subscription(i) and i.get("signal") in alerts.SIGNALS]
    return profile, subs


def get_subscriptions(_q, claims):
    need_notifications()
    profile, subs = _user_items(need_user(claims))
    return {
        "subscriptions": [_subscription_view(i) for i in subs],
        "max": MAX_SUBSCRIPTIONS_PER_USER,
        "intervalMinutes": NOTIFY_INTERVAL_MINUTES,
        "email": profile.get("email") if profile else None,
    }


def _subscribe_email(user, email):
    resp = client("sns", None).subscribe(
        TopicArn=TOPIC_ARN,
        Protocol="email",
        Endpoint=email,
        # Set server-side from the token: this address only gets messages for this user.
        Attributes={"FilterPolicy": json.dumps({"userId": [user]}), "FilterPolicyScope": "MessageAttributes"},
        ReturnSubscriptionArn=True,
    )
    return resp["SubscriptionArn"]


def _unsubscribe(arn):
    """Remove an SNS subscription. A pending or already removed one is not an error here."""
    if not arn or not arn.startswith(TOPIC_ARN + ":"):
        return
    try:
        client("sns", None).unsubscribe(SubscriptionArn=arn)
    except ClientError:
        logger.exception("unsubscribe failed for an old subscription")


def _transact(items):
    t = subscriptions_table()
    for entry in items:
        next(iter(entry.values()))["TableName"] = t.name
    t.meta.client.transact_write_items(TransactItems=items)


def _cancel_reasons(err):
    return [r.get("Code") for r in err.response.get("CancellationReasons", [])]


def _profile_key(user):
    return {"userId": user, "sk": alerts.PROFILE_SK}


def _save_profile(user, profile, email, arn, count, now):
    """Create the profile only when it is absent, or change only its email and subscription on an
    existing one; alertCount is changed by the transactions alone. False when the condition failed."""
    try:
        if profile is None:
            subscriptions_table().put_item(
                Item={**_profile_key(user), "email": email, "subscriptionArn": arn, "alertCount": count, "createdAt": now},
                ConditionExpression="attribute_not_exists(sk)",
            )
        else:
            subscriptions_table().update_item(
                Key=_profile_key(user),
                # if_not_exists only fills a count on an older profile that has none.
                UpdateExpression="SET email = :e, subscriptionArn = :a, alertCount = if_not_exists(alertCount, :n)",
                ConditionExpression="attribute_exists(sk)",
                ExpressionAttributeValues={":e": email, ":a": arn, ":n": count},
            )
        return True
    except ClientError as err:
        if not _conditional_failed(err):
            raise
        return False


def _spot_alert_size(region, itype):
    """(stored size, group key or None, GPU counts) of a Spot score alert. The alert checks its score
    group's score (any size), so it is stored under the group's canonical size: the first size, in
    natural order, of the group's sizes the Region sells (per the stored GPU counts), whatever size
    was sent. A type without a known group keeps its own name."""
    state = _groups_state_or_none()
    gpus = (state or {}).get("gpus") or {}
    group = score_groups.group_key(itype, gpus)
    if group is None:
        return itype, None, gpus
    sizes = {t for t in region_offerings(region) if scored(t) and score_groups.group_key(t, gpus) == group}
    return sort_types(sizes | {itype})[0], group, gpus


def _same_spot_alert(item, region, group, threshold, gpus):
    """True when a stored alert is a Spot score alert of the same group, Region and threshold (any
    size of the group: older alerts may name another size)."""
    if group is None or item.get("signal") != alerts.SPOT_SCORE or item.get("region") != region:
        return False
    try:
        same = int(item.get("threshold")) == threshold
    except (TypeError, ValueError):
        return False
    return same and score_groups.group_key(item.get("instanceType"), gpus) == group


def post_subscription(_q, claims, body):
    need_notifications()
    user = need_user(claims)
    email = need_email(claims)
    region = need_region(body)
    signal = body.get("signal", "")
    if signal not in alerts.SIGNALS:
        raise HttpError(400, f"signal must be one of {', '.join(alerts.SIGNALS)}", "bad-parameter", {"name": "signal"})
    # A Capacity Block alert takes any type with Capacity Blocks in the Region; a Spot score alert
    # takes a type the Region offers whose family gets Spot scores (spotScoreFamilies), since any
    # other type never has a score to compare.
    itype = need_cb_type(body, region) if signal == alerts.CAPACITY_BLOCK else need_scored(need_instance_type(body, region))
    name, low, high = alerts.PARAMS[signal]
    value = body_int(body, name, low, high)
    if not alerts.valid_value(signal, value):
        raise HttpError(400, BAD_DAYS_MESSAGE, "bad-days")
    extra = {}
    count = alerts.DEFAULT_COUNT
    if signal == alerts.CAPACITY_BLOCK:
        if body.get("count") not in (None, ""):
            count = body_int(body, "count", 1, cb.MAX_COUNT)
        extra["count"] = count

    group, gpus = None, {}
    if signal == alerts.SPOT_SCORE:
        itype, group, gpus = _spot_alert_size(region, itype)
    sk = alerts.sort_key(region, itype, signal, value, count)
    confirmation_sent = False
    for _ in range(2):
        profile, subs = _user_items(user)
        if any(i["sk"] == sk or _same_spot_alert(i, region, group, value, gpus) for i in subs):
            raise HttpError(409, "you already have this alert", "alert-exists")
        if len(subs) >= MAX_SUBSCRIPTIONS_PER_USER:
            raise HttpError(409, f"you have {len(subs)} alert(s) and the limit is {MAX_SUBSCRIPTIONS_PER_USER}; delete one first",
                            "alert-limit", {"max": MAX_SUBSCRIPTIONS_PER_USER})

        now = iso(datetime.now(timezone.utc))
        # Subscribe when there is no profile, the token's email changed, or the subscription is gone
        # (the notifier clears subscriptionArn when SNS no longer knows it).
        if not profile or profile.get("email") != email or not profile.get("subscriptionArn"):
            arn = _subscribe_email(user, email)
            confirmation_sent = True
            if not _save_profile(user, profile, email, arn, len(subs), now):
                # A concurrent request created or removed the profile: read again.
                continue
            if profile and profile.get("subscriptionArn") not in (None, arn):
                # The old address would keep getting this user's alerts.
                _unsubscribe(profile.get("subscriptionArn"))
        item = {"userId": user, "sk": sk, "region": region, "instanceType": itype, "signal": signal,
                name: value, **extra, "lastState": alerts.PENDING, "createdAt": now}
        try:
            # The count on the profile and the alert change together, so concurrent
            # requests cannot pass the limit, and the profile must still exist.
            _transact([
                {"Update": {"Key": _profile_key(user), "UpdateExpression": "ADD alertCount :one",
                            "ConditionExpression": "attribute_exists(sk) AND (attribute_not_exists(alertCount) OR alertCount < :max)",
                            "ExpressionAttributeValues": {":one": 1, ":max": MAX_SUBSCRIPTIONS_PER_USER}}},
                {"Put": {"Item": item, "ConditionExpression": "attribute_not_exists(sk)"}},
            ])
            return {"subscription": _subscription_view(item), "confirmationSent": confirmation_sent, "email": email}
        except ClientError as err:
            if err.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = _cancel_reasons(err)
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                raise HttpError(409, "you already have this alert", "alert-exists")
            if not reasons or reasons[0] != "ConditionalCheckFailed":
                raise
            # The profile was removed or the count reached the limit: read again.
    raise HttpError(409, f"the alert limit is {MAX_SUBSCRIPTIONS_PER_USER}, or your alerts changed at the same time; try again",
                    "alert-conflict", {"max": MAX_SUBSCRIPTIONS_PER_USER})


def delete_subscription(q, claims):
    need_notifications()
    user = need_user(claims)
    sk = q.get("id", "")
    if not alerts.SK_RE.match(sk):
        raise HttpError(400, "id must be an alert ID from GET /api/subscriptions", "bad-parameter", {"name": "id"})
    # The key includes the caller's sub, so this reaches only the caller's items.
    key = {"userId": user, "sk": sk}
    try:
        _transact([
            {"Delete": {"Key": key, "ConditionExpression": "attribute_exists(sk)"}},
            {"Update": {"Key": _profile_key(user), "UpdateExpression": "ADD alertCount :neg",
                        "ConditionExpression": "attribute_exists(sk)", "ExpressionAttributeValues": {":neg": -1}}},
        ])
    except ClientError as err:
        if err.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = _cancel_reasons(err)
        if reasons and reasons[0] == "ConditionalCheckFailed":
            raise HttpError(404, "no alert with that ID belongs to you", "alert-not-found")
        if not (len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed"):
            raise
        # No profile: delete the alert on its own.
        try:
            subscriptions_table().delete_item(Key=key, ConditionExpression="attribute_exists(sk)")
        except ClientError as inner:
            if not _conditional_failed(inner):
                raise
            raise HttpError(404, "no alert with that ID belongs to you", "alert-not-found")
    profile, subs = _user_items(user)
    if profile and not subs:
        # No alerts left: remove the profile and its SNS subscription. The condition keeps
        # the profile when a concurrent request just added an alert.
        try:
            old = subscriptions_table().delete_item(
                Key=_profile_key(user),
                ConditionExpression="attribute_not_exists(alertCount) OR alertCount <= :zero",
                ExpressionAttributeValues={":zero": 0},
                ReturnValues="ALL_OLD",
            ).get("Attributes", {})
            _unsubscribe(old.get("subscriptionArn"))
        except ClientError as err:
            if not _conditional_failed(err):
                raise
    return {"id": sk, "deleted": True}


ROUTES = {
    ("GET", "/api/config"): get_config,
    ("GET", "/api/instance-types"): get_instance_types,
    ("GET", "/api/instance-type-zones"): get_instance_type_zones,
    ("GET", "/api/capacity-blocks"): get_capacity_blocks,
    ("GET", "/api/capacity-blocks/max"): get_capacity_block_max,
    ("GET", "/api/spot-scores"): get_spot_scores,
    ("GET", "/api/spot-heatmap"): get_spot_heatmap,
    ("GET", "/api/spot-prices"): get_spot_prices,
    ("GET", "/api/on-demand-price"): get_on_demand_price,
    ("GET", "/api/quotas"): get_quotas,
    ("GET", "/api/offering-matrix"): get_offering_matrix,
    ("GET", "/api/probes"): get_probes,
    ("GET", "/api/subscriptions"): get_subscriptions,
    ("DELETE", "/api/subscriptions"): delete_subscription,
}


BODY_ROUTES = {
    ("POST", "/api/probe"): post_probe,
    ("POST", "/api/subscriptions"): post_subscription,
}


def _dispatch(method, path, event, q, claims):
    try:
        # The JWT authorizer accepts any token of the app client, an access token too (it matches
        # client_id as well as aud). Only an ID token carries the email and user name the routes
        # read. Cognito sets token_use on every token it issues; a token without it is refused.
        if claims.get("token_use") != "id":
            raise HttpError(401, "an ID token of the user pool is required", "no-identity")
        body_route = BODY_ROUTES.get((method, path))
        if body_route:
            try:
                body = json.loads(event.get("body") or "{}")
            except ValueError:
                raise HttpError(400, "body must be JSON", "bad-body")
            if not isinstance(body, dict):
                raise HttpError(400, "body must be a JSON object", "bad-body")
            return respond(200, body_route(q, claims, body)), None
        route = ROUTES.get((method, path))
        if route is None:
            raise HttpError(404, "not found", "not-found")
        return respond(200, route(q, claims)), None
    except HttpError as err:
        return respond(err.status, error_body(err.message, err.code, err.params)), err.code
    except ClientError as err:
        logger.exception("AWS call failed")
        code = err.response["Error"]["Code"]
        return respond(502, error_body(f"AWS call failed ({code})", "aws-error", {"awsCode": code})), f"aws-error:{code}"
    except Exception:
        logger.exception("unhandled error")
        return respond(500, error_body("internal error", "internal")), "internal"


def _request_line(method, path, q, status, started, error_code, cold):
    """One INFO line per request. Only known routes, configured or enabled Regions, and well-formed
    instance type names are logged: no token, claim, email, or other query value."""
    known = (method, path) in ROUTES or (method, path) in BODY_ROUTES
    line = {"msg": "request", "method": method if known else "-", "route": path if known else "-",
            "status": status, "durationMs": round((time.monotonic() - started) * 1000),
            "cacheHits": _cache_counts["hits"], "cacheMisses": _cache_counts["misses"], "coldStart": cold}
    known_regions = set(REGIONS) | set(_regions_mem[1]["regions"] if _regions_mem else ())
    if q.get("region") in known_regions:
        line["region"] = q["region"]
    itype = q.get("instanceType")
    if isinstance(itype, str) and TYPE_RE.match(itype):
        line["instanceType"] = itype
    if error_code:
        line["errorCode"] = error_code
    return json.dumps(line, sort_keys=True)


def handler(event, context):
    global _cold
    started = time.monotonic()
    cold, _cold = _cold, False
    _set_deadline(context)
    with _cache_counts_lock:
        _cache_counts.update(hits=0, misses=0)
    http = event["requestContext"]["http"]
    method, path = http["method"], event["rawPath"]
    q = event.get("queryStringParameters") or {}
    claims = event["requestContext"].get("authorizer", {}).get("jwt", {}).get("claims", {})
    response, error_code = _dispatch(method, path, event, q, claims)
    try:
        logger.info(_request_line(method, path, q, response["statusCode"], started, error_code, cold))
    except Exception:  # noqa: BLE001 - the log line never changes the answer
        logger.warning("request log line failed", exc_info=True)
    return response


# Lambda sets AWS_LAMBDA_FUNCTION_NAME; tests and local imports create nothing at import time.
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    _prewarm()
