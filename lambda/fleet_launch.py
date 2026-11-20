"""One bounded test launch with an EC2 Fleet of type instant.

A test launch asks EC2 for ONE instance of any size of a score group (or of one chosen size) in any
Availability Zone of a Region, and terminates it at once:

* Overrides: every default subnet of the Region (one per Availability Zone) x every size that fits
  the account's vCPU quota. On-Demand uses the lowest-price allocation strategy, Spot uses
  price-capacity-optimized. EC2 answers synchronously: the instance it launched (with its zone and
  size) or one error per override it tried (Errors[]).
* A launch template is created for the test (tagged with the probe tag) and deleted right after
  the fleet answer, also when the call fails. The scheduled cleanup deletes probe-tagged templates
  older than an hour that a failed delete left behind.
* The instance, its volume and network interface, and the fleet carry the probe tag. The instance
  also carries LAUNCH_TAG with the request's ClientToken, so the function can find it when the
  CreateFleet answer is lost. The instance powers itself off 5 minutes after boot
  (PROBE_USER_DATA) and terminates on shutdown.
* Deadline: CreateFleet is sent only while at least MIN_SECONDS_FOR_FLEET of the invocation are
  left (else NoTimeLeft, nothing sent), with one attempt and a read timeout that leaves
  CLEANUP_RESERVE_SECONDS to terminate what it launched.
* Not sent: when CreateFleet fails before EC2 processed it (a connect timeout, an unreachable
  endpoint, a parameter or credential error of botocore, or a throttling answer such as
  RequestLimitExceeded), nothing was launched. The result is "error" with EC2's or botocore's
  code and "refund": True, so the caller gives the daily counts and the cooldown slot back.
* Unknown outcome: when CreateFleet loses its answer (a read timeout, a closed connection, another
  HTTP client error) or EC2 answers with a server error (5xx) without a known code, EC2 may still
  have launched an instance. When time allows, the function sends the SAME request again (same
  ClientToken, same parameters: CreateFleet is idempotent with a client token, so EC2 answers the
  first request's outcome without a second launch) and uses that answer. Otherwise, or when the
  resend fails too, the result is "error" with errorCode "unknown-outcome"; the function looks the
  instance up by LAUNCH_TAG and terminates whatever it finds (one bounded call each). The caller
  keeps the daily counts and the cooldown slot: the request may have reached EC2.
* After CreateFleet, every call (terminate, lookup, template delete) uses a one-attempt client
  with short timeouts. A found instance is terminated in any case; the template delete is skipped
  when less than TEMPLATE_DELETE_MIN_SECONDS are left, so the caller keeps ANSWER_RESERVE_SECONDS
  to store the record and answer (the scheduled cleanup deletes the template after an hour).

An instant fleet needs no DeleteFleets call: EC2 deletes the fleet request itself some time after
all its instances are terminated, or when it launched none (Amazon EC2 User Guide, "Configure an
EC2 Fleet of type instant"). The function terminates the instance itself, as before.

Only the functions here touch EC2; the API function (api.py) decides whether a launch may run
(guards, daily caps, cooldown) and records the result.
"""
import base64
import logging
import uuid

from botocore.exceptions import (BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError,
                                 NoCredentialsError, NoRegionError, ParamValidationError, PartialCredentialsError,
                                 ProxyConnectionError)

logger = logging.getLogger()

# Fixed names: the cleanup, the teardown and the IAM conditions find the probe resources by them,
# so one account holds one deployment (a second one would clean up the first one's probes).
PROBE_TAG = "gpu-capacity-finder-probe"
PROBE_SG_TAG = "gpu-capacity-finder-probe-sg"
PROBE_SG_NAME = "gpu-capacity-finder-probe"
TAG = {"Key": PROBE_TAG, "Value": "true"}
# The per-launch tag on the instance: its value is the CreateFleet ClientToken of that launch.
LAUNCH_TAG = "gpu-capacity-finder-launch"
UNKNOWN_OUTCOME = "unknown-outcome"
# CreateFleet is sent only while this much of the invocation is left.
MIN_SECONDS_FOR_FLEET = 12
FLEET_CONNECT_TIMEOUT = 2
# The CreateFleet read timeout when the invocation's time left is unknown, and its upper bound.
FLEET_READ_TIMEOUT = 15
AFTER_CONNECT_TIMEOUT = 1
AFTER_READ_TIMEOUT = 2
# The unknown-outcome lookup is skipped when less than this is left.
AFTER_STEP_SECONDS = AFTER_CONNECT_TIMEOUT + AFTER_READ_TIMEOUT
# Kept for the caller after the template delete: storing the record, reading today's counts and
# the answer.
ANSWER_RESERVE_SECONDS = 2
# The template delete is skipped (left for the scheduled cleanup) when less than this is left.
TEMPLATE_DELETE_MIN_SECONDS = AFTER_STEP_SECONDS + ANSWER_RESERVE_SECONDS
# Kept free after the CreateFleet call: the lookup and terminate of an unknown outcome (each one
# attempt of at most AFTER_STEP_SECONDS) plus ANSWER_RESERVE_SECONDS for the caller, the same
# reserve the resend path keeps; or the terminate of a launched instance. The template delete fits
# only when it leaves ANSWER_RESERVE_SECONDS.
CLEANUP_RESERVE_SECONDS = 2 * AFTER_STEP_SECONDS + ANSWER_RESERVE_SECONDS
# The read timeout of the terminate call after a launch, at most (less when less time is left).
TERMINATE_READ_TIMEOUT = 4
# The resend of an unknown outcome needs at least this read timeout, and keeps two steps (lookup
# and terminate) and ANSWER_RESERVE_SECONDS free after it.
RESEND_MIN_READ_TIMEOUT = 2
# Launch template names of test launches: this prefix and a random part.
TEMPLATE_PREFIX = "gpu-capacity-finder-probe-"
# Last line of defense for a probe that nobody terminates. The function terminates each probe right
# after the fleet answer, and the scheduled cleanup function terminates tagged probes older than 10
# minutes. If both fail (for example an API error and then a broken schedule), a large GPU instance
# would keep running and billing. Every probe therefore starts with this user data: cloud-init on
# Amazon Linux 2023 (x86_64 and arm64) runs a "#!" script once at first boot, and the script asks the
# operating system to power off 5 minutes later. Because the launch template sets
# InstanceInitiatedShutdownBehavior to "terminate", an OS power-off terminates the instance (a
# one-time Spot Instance cannot be stopped at all, so it terminates too). The script is static: no
# secrets, no account data, no network calls. A launch template takes the user data base64-encoded.
PROBE_USER_DATA = "#!/bin/bash\nshutdown -h +5\n"
MARKETS = ("on-demand", "spot")
# Allocation strategy per market.
STRATEGIES = {"on-demand": "lowest-price", "spot": "price-capacity-optimized"}
# Overrides of one launch template configuration that one request sends at most.
MAX_OVERRIDES = 300
# Errors listed in a test launch answer at most (one per override EC2 tried).
MAX_ERRORS_SHOWN = 20

CAPACITY_ERRORS = {
    "InsufficientInstanceCapacity",
    "InsufficientHostCapacity",
    "InsufficientCapacity",
    # EC2 Fleet's answer when no Spot (or On-Demand) capacity matches the request.
    "UnfulfillableCapacity",
}
QUOTA_ERRORS = {"VcpuLimitExceeded", "InstanceLimitExceeded", "MaxSpotInstanceCountExceeded"}
UNSUPPORTED_ERRORS = {"Unsupported"}
# Throttling answers: EC2 refused the request before processing it, so nothing was launched.
THROTTLE_ERRORS = {"RequestLimitExceeded", "Throttling", "ThrottlingException"}
# botocore errors raised before the request was sent (no connection, bad parameters, no
# credentials): nothing reached EC2. Every other botocore error (a read timeout, a closed
# connection, a TLS error) may come after EC2 got the request, so its outcome is unknown.
NOT_SENT_ERRORS = (ConnectTimeoutError, EndpointConnectionError, ProxyConnectionError, ParamValidationError,
                   NoCredentialsError, PartialCredentialsError, NoRegionError)


class NoDefaultVpc(Exception):
    """The Region has no default subnet: test launches run in the default VPC only."""


class NoTimeLeft(Exception):
    """Too little of the invocation is left to send CreateFleet and still terminate what it
    launches. Nothing was sent to EC2."""


def fleet_read_timeout(left):
    """The CreateFleet read timeout for `left` seconds of the invocation (None: unknown): what is
    left after the connect timeout and CLEANUP_RESERVE_SECONDS, between 1 and FLEET_READ_TIMEOUT."""
    if left is None:
        return FLEET_READ_TIMEOUT
    return max(1, min(FLEET_READ_TIMEOUT, int(left - CLEANUP_RESERVE_SECONDS - FLEET_CONNECT_TIMEOUT)))


def classify(code):
    """Map an EC2 error code to a coarse result: no-capacity, quota, unsupported or error."""
    if code in CAPACITY_ERRORS:
        return "no-capacity"
    if code in QUOTA_ERRORS:
        return "quota"
    if code in UNSUPPORTED_ERRORS:
        return "unsupported"
    return "error"


def default_subnets(ec2):
    """({zone: subnet id}, VPC id) of the Region's default subnets (one per Availability Zone).
    Raises NoDefaultVpc when there is none."""
    resp = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])
    subnets, vpcs = {}, []
    for s in resp.get("Subnets") or []:
        if s.get("State", "available") != "available" or not s.get("AvailabilityZone"):
            continue
        subnets.setdefault(s["AvailabilityZone"], s["SubnetId"])
        vpcs.append(s["VpcId"])
    if not subnets:
        raise NoDefaultVpc()
    return dict(sorted(subnets.items())), vpcs[0]


def fitting(sizes, vcpus, available):
    """(sizes that fit `available` vCPUs, [{instanceType, vcpus}] that do not). A size of unknown
    vCPU count, or an unknown quota (None), is kept: EC2 then decides."""
    if available is None:
        return list(sizes), []
    fit, skipped = [], []
    for t in sizes:
        need = vcpus.get(t)
        if need is None or need <= available:
            fit.append(t)
        else:
            skipped.append({"instanceType": t, "vcpus": need})
    return fit, skipped


def overrides(subnets, sizes):
    """One override per size and default subnet: sizes in the given order, zones in name order."""
    out = [{"InstanceType": t, "SubnetId": subnets[az]} for t in sizes for az in sorted(subnets)]
    return out[:MAX_OVERRIDES]


def template_data(image_id, security_group, market):
    """The launch template of one test: no public address, the dedicated security group (no inbound
    rules), IMDSv2 only, and the self-shutdown script. On-Demand never runs in an open Capacity
    Reservation (for example another team's): the test measures free On-Demand capacity."""
    data = {
        "ImageId": image_id,
        "NetworkInterfaces": [{"DeviceIndex": 0, "AssociatePublicIpAddress": False, "Groups": [security_group],
                               "DeleteOnTermination": True}],
        "MetadataOptions": {"HttpTokens": "required"},
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": base64.b64encode(PROBE_USER_DATA.encode()).decode(),
    }
    if market == "on-demand":
        data["CapacityReservationSpecification"] = {"CapacityReservationPreference": "none"}
    return data


def fleet_params(template_id, version, items, market, token=None):
    """The CreateFleet request: type instant, one instance of the market, the market's allocation
    strategy, the probe tag on the instance, its volume and network interface, and the fleet, and
    LAUNCH_TAG (value: the ClientToken) on the instance."""
    token = token or uuid.uuid4().hex
    tags = {r: [dict(TAG)] for r in ("instance", "volume", "network-interface", "fleet")}
    tags["instance"].append({"Key": LAUNCH_TAG, "Value": token})
    params = {
        "Type": "instant",
        "ClientToken": token,
        "LaunchTemplateConfigs": [{
            "LaunchTemplateSpecification": {"LaunchTemplateId": template_id, "Version": str(version)},
            "Overrides": items,
        }],
        "TargetCapacitySpecification": {"TotalTargetCapacity": 1, "DefaultTargetCapacityType": market},
        "TagSpecifications": [{"ResourceType": r, "Tags": t} for r, t in tags.items()],
    }
    if market == "spot":
        params["SpotOptions"] = {"AllocationStrategy": STRATEGIES["spot"], "InstanceInterruptionBehavior": "terminate"}
    else:
        params["OnDemandOptions"] = {"AllocationStrategy": STRATEGIES["on-demand"]}
    return params


def _override_of(item):
    return (item.get("LaunchTemplateAndOverrides") or {}).get("Overrides") or {}


def _zone(override, zones):
    return override.get("AvailabilityZone") or zones.get(override.get("SubnetId"))


def parse_fleet(resp, zones):
    """The CreateFleet answer as {result, errorCode, obtained, instanceIds, errors}. `zones` maps a
    subnet id to its Availability Zone. launched: EC2 started an instance (obtained = its zone and
    size). Otherwise Errors[] decide: no-capacity when any override found no capacity, else quota
    when any hit a quota, else unsupported when every one was unsupported, else error."""
    errors = []
    for e in resp.get("Errors") or []:
        o = _override_of(e)
        errors.append({"code": e.get("ErrorCode") or "Unknown", "message": str(e.get("ErrorMessage") or "")[:300],
                       "az": _zone(o, zones), "instanceType": o.get("InstanceType")})
    # Every instance of every Instances[] entry is terminated; obtained names the first one.
    ids, obtained = [], None
    for item in resp.get("Instances") or []:
        found = [i for i in item.get("InstanceIds") or [] if i not in ids]
        if found and obtained is None:
            o = _override_of(item)
            obtained = {"az": _zone(o, zones), "instanceType": item.get("InstanceType") or o.get("InstanceType")}
        ids += found
    if ids:
        return {"result": "launched", "errorCode": None, "instanceIds": ids, "errors": errors[:MAX_ERRORS_SHOWN],
                "obtained": obtained}
    codes = [e["code"] for e in errors]
    result, code = "error", (codes[0] if codes else "NoInstanceLaunched")
    for wanted, pool in (("no-capacity", CAPACITY_ERRORS), ("quota", QUOTA_ERRORS)):
        hit = next((c for c in codes if c in pool), None)
        if hit:
            result, code = wanted, hit
            break
    else:
        if codes and all(c in UNSUPPORTED_ERRORS for c in codes):
            result = "unsupported"
    message = next((e["message"] for e in errors if e["code"] == code and e["message"]), "")
    return {"result": result, "errorCode": code, "errorMessage": message or None, "obtained": None, "instanceIds": [],
            "errors": errors[:MAX_ERRORS_SHOWN]}


def _code(err):
    return err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__


def delete_template(ec2, template_id):
    """Delete one test launch template; a failure is logged (the scheduled cleanup deletes it later)."""
    try:
        ec2.delete_launch_template(LaunchTemplateId=template_id)
        return True
    except (ClientError, BotoCoreError) as err:
        logger.warning("could not delete the test launch template %s (%s)", template_id, _code(err))
        return False


def not_sent(err):
    """True when the failed CreateFleet call launched nothing for sure: botocore never sent it
    (NOT_SENT_ERRORS), or EC2 throttled it (THROTTLE_ERRORS)."""
    if isinstance(err, NOT_SENT_ERRORS):
        return True
    return isinstance(err, ClientError) and _code(err) in THROTTLE_ERRORS


def _unknown_outcome(err):
    """True when CreateFleet may have launched although the call failed: a botocore error after the
    request may have been sent (read timeout, connection lost), or a server error (5xx) of EC2
    whose code does not say that nothing launched (not a throttling, capacity, quota or
    unsupported code)."""
    if not_sent(err):
        return False
    if isinstance(err, BotoCoreError):
        return True
    if not isinstance(err, ClientError) or classify(_code(err)) != "error":
        return False
    return int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0) >= 500


def terminate_read_timeout(left):
    """The read timeout of the terminate call after a launch for `left` seconds (None: unknown):
    TERMINATE_READ_TIMEOUT at most, and short enough to answer before the deadline (1 at least:
    the instance is terminated in any case)."""
    if left is None:
        return TERMINATE_READ_TIMEOUT
    return max(1, min(TERMINATE_READ_TIMEOUT, int(left - AFTER_CONNECT_TIMEOUT - 1)))


def resend_read_timeout(left):
    """The read timeout of the resend after an unknown outcome, or None when too little is left
    for it plus the lookup and terminate steps after it and ANSWER_RESERVE_SECONDS for the caller
    (the terminate after the lookup runs without a time check)."""
    if left is None:
        return FLEET_READ_TIMEOUT
    read = int(left - AFTER_CONNECT_TIMEOUT - 2 * AFTER_STEP_SECONDS - ANSWER_RESERVE_SECONDS)
    return min(FLEET_READ_TIMEOUT, read) if read >= RESEND_MIN_READ_TIMEOUT else None


def _terminate(ec2, ids):
    """Terminate `ids` in one call; a failure is logged (the scheduled cleanup and the
    self-shutdown remain)."""
    try:
        ec2.terminate_instances(InstanceIds=ids)
        return True
    except (ClientError, BotoCoreError) as err:
        logger.warning("terminate failed for %s (%s)", ids, _code(err))
        return False


def resend(single_try, params, zones, time_left=None):
    """After an unknown outcome: the same CreateFleet request again (same ClientToken, same
    parameters), parsed, when time allows; None when it is skipped or fails too. CreateFleet is
    idempotent with a client token: EC2 answers the outcome of the first request and launches
    nothing more."""
    read = resend_read_timeout(time_left() if time_left else None)
    if read is None:
        logger.warning("no time left to resend the test launch %s", params["ClientToken"])
        return None
    try:
        return parse_fleet(single_try(AFTER_CONNECT_TIMEOUT, read).create_fleet(**params), zones)
    except (ClientError, BotoCoreError) as err:
        logger.warning("the resend of test launch %s failed too (%s)", params["ClientToken"], _code(err))
        return None


def find_and_terminate(ec2, token, time_left=None):
    """Best effort after an unknown outcome: the instances that carry LAUNCH_TAG=token (one
    DescribeInstances call, no paging), terminated in one call. Returns the ids found; a failed
    or skipped step is logged (the scheduled cleanup and the self-shutdown remain)."""
    def enough():
        left = time_left() if time_left else None
        return left is None or left >= AFTER_STEP_SECONDS
    if not enough():
        logger.warning("no time left to look up the instance of an unknown test launch outcome (%s)", token)
        return []
    try:
        resp = ec2.describe_instances(Filters=[
            {"Name": f"tag:{LAUNCH_TAG}", "Values": [token]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}], MaxResults=50)
        ids = [i["InstanceId"] for r in resp.get("Reservations") or [] for i in r.get("Instances") or []]
    except (ClientError, BotoCoreError, KeyError) as err:
        logger.warning("could not look up the instance of an unknown test launch outcome %s (%s)", token, _code(err))
        return []
    if not ids:
        logger.info("unknown test launch outcome %s: no instance found", token)
        return []
    # A found instance is terminated in any case (the template delete gives way instead).
    if _terminate(ec2, ids):
        logger.warning("unknown test launch outcome %s: terminated %s", token, ids)
    return ids


def launch(ec2, *, image_id, security_group, subnets, sizes, market, time_left=None, single_try=None):
    """Create the template, ask the instant fleet for one instance, terminate what it launched, and
    delete the template (unless less than TEMPLATE_DELETE_MIN_SECONDS are left). Returns parse_fleet's dict; a CreateFleet
    error becomes a result too (EC2 answered; "refund": True when nothing was sent or EC2 throttled
    it), and an unknown outcome is the resend's answer or result "error" with errorCode
    "unknown-outcome". An error of CreateLaunchTemplate raises: nothing was launched. NoTimeLeft
    (raised before CreateFleet, nothing sent) when `time_left()` (seconds left of the invocation,
    or None) is below MIN_SECONDS_FOR_FLEET. `single_try(connect_timeout, read_timeout)` makes an
    EC2 client with one attempt: every call after CreateLaunchTemplate uses it (`ec2` when not
    given)."""
    single_try = single_try or (lambda _connect, _read: ec2)
    template = ec2.create_launch_template(
        LaunchTemplateName=f"{TEMPLATE_PREFIX}{uuid.uuid4().hex[:16]}",
        LaunchTemplateData=template_data(image_id, security_group, market),
        TagSpecifications=[{"ResourceType": "launch-template", "Tags": [dict(TAG)]}],
    )["LaunchTemplate"]
    try:
        zones = {s: az for az, s in subnets.items()}
        left = time_left() if time_left else None
        if left is not None and left < MIN_SECONDS_FOR_FLEET:
            logger.warning("test launch not sent: %.1f seconds left of the invocation", left)
            raise NoTimeLeft()
        token = uuid.uuid4().hex
        params = fleet_params(template["LaunchTemplateId"], template["LatestVersionNumber"],
                              overrides(subnets, sizes), market, token=token)
        try:
            resp = single_try(FLEET_CONNECT_TIMEOUT, fleet_read_timeout(left)).create_fleet(**params)
        except (ClientError, BotoCoreError) as err:
            code = _code(err)
            errors = [{"code": code, "az": None, "instanceType": None}]
            if not_sent(err):
                logger.warning("CreateFleet not sent or throttled (%s); nothing launched", code)
                return {"result": "error", "errorCode": code, "obtained": None, "instanceIds": [], "errors": errors,
                        "refund": True}
            if not _unknown_outcome(err):
                return {"result": classify(code), "errorCode": code, "obtained": None, "instanceIds": [],
                        "errors": errors}
            logger.warning("CreateFleet outcome unknown (%s); resending or looking for instance %s=%s",
                           code, LAUNCH_TAG, token)
            resp = None
            out = resend(single_try, params, zones, time_left)
            if out is None:
                ids = find_and_terminate(single_try(AFTER_CONNECT_TIMEOUT, AFTER_READ_TIMEOUT), token, time_left)
                return {"result": "error", "errorCode": UNKNOWN_OUTCOME, "obtained": None, "instanceIds": ids,
                        "errors": errors}
        if resp is not None:
            out = parse_fleet(resp, zones)
        if out["instanceIds"]:
            # The scheduled cleanup function terminates tagged leftovers when this call fails.
            left = time_left() if time_left else None
            _terminate(single_try(AFTER_CONNECT_TIMEOUT, terminate_read_timeout(left)), out["instanceIds"])
        return out
    finally:
        left = time_left() if time_left else None
        if left is not None and left < TEMPLATE_DELETE_MIN_SECONDS:
            logger.warning("test launch template %s left for the scheduled cleanup (no time left)", template["LaunchTemplateId"])
        else:
            delete_template(single_try(AFTER_CONNECT_TIMEOUT, AFTER_READ_TIMEOUT), template["LaunchTemplateId"])
