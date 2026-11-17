"""Batched reads of the cache table (DynamoDB BatchGetItem), shared by the API and the notifier.

One BatchGetItem request reads up to MAX_KEYS_PER_CALL items. DynamoDB bills each item read the
same way as with GetItem, so batching saves requests and latency, not read capacity. Keys that
DynamoDB leaves unprocessed are asked again, at most MAX_ATTEMPTS requests per chunk in all. A key
still unprocessed after that, or every key of a chunk whose request fails, is read with GetItem on a
small thread pool instead, so a passing batch error costs latency only. An item whose GetItem fails
too is left out of the answer: the caller treats it as a cache miss, as it treats a missing item. A
caller that must tell the two apart passes `unread` (a list) and gets those keys in it."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()

KEY = "cacheKey"
# The BatchGetItem limit: at most 100 keys per request.
MAX_KEYS_PER_CALL = 100
# Requests per chunk, the first one included; UnprocessedKeys are asked again with a short backoff.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.05
# The GetItem fallback runs on this many threads.
FALLBACK_WORKERS = 8


def _code(err):
    if isinstance(err, ClientError):
        return err.response.get("Error", {}).get("Code", "")
    return type(err).__name__


def _batch_client(table):
    """The low-level client behind a boto3 Table resource, or None for a handle without one. The
    resource's client takes and returns plain Python values, as Table.get_item does."""
    return getattr(getattr(table, "meta", None), "client", None)


# What _read_one returns for a read that failed (not the same as a missing item: None).
_UNREAD = object()


def _read_one(table, key, consistent):
    try:
        kwargs = {"ConsistentRead": True} if consistent else {}
        return table.get_item(Key={KEY: key}, **kwargs).get("Item")
    except (ClientError, BotoCoreError) as err:
        logger.warning("cache read failed for %s (%s)", key, _code(err))
        return _UNREAD


def get_one(table, key, consistent=False):
    """The item with the key, or None when it is missing or its read fails (logged)."""
    item = _read_one(table, key, consistent)
    return None if item is _UNREAD else item


def _get_each(table, keys, consistent, unread=None):
    """{key: item} from one GetItem per key, on up to FALLBACK_WORKERS threads. The keys whose
    read failed are added to `unread` (when given)."""
    if len(keys) == 1:
        items = [_read_one(table, keys[0], consistent)]
    else:
        with ThreadPoolExecutor(max_workers=min(FALLBACK_WORKERS, len(keys))) as pool:
            items = list(pool.map(lambda k: _read_one(table, k, consistent), keys))
    if unread is not None:
        unread.extend(k for k, item in zip(keys, items) if item is _UNREAD)
    return {k: item for k, item in zip(keys, items) if item and item is not _UNREAD}


def _batch_chunk(client, name, chunk, consistent, sleep):
    """(found, left): the items of one chunk of at most MAX_KEYS_PER_CALL keys, and the keys still
    unread after MAX_ATTEMPTS requests or a failed request."""
    keys = [{KEY: k} for k in chunk]
    found = {}
    for attempt in range(MAX_ATTEMPTS):
        request = {name: {"Keys": keys, **({"ConsistentRead": True} if consistent else {})}}
        try:
            resp = client.batch_get_item(RequestItems=request)
        except (ClientError, BotoCoreError) as err:
            logger.warning("cache batch read failed (%s); reading %d items one by one", _code(err), len(keys))
            break
        for item in resp.get("Responses", {}).get(name, []):
            found[item[KEY]] = item
        keys = ((resp.get("UnprocessedKeys") or {}).get(name) or {}).get("Keys") or []
        if not keys:
            break
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(RETRY_DELAY_SECONDS * 2 ** attempt)
    else:
        logger.warning("cache batch read left %d keys unprocessed; reading them one by one", len(keys))
    return found, [k[KEY] for k in keys]


def get_items(table, keys, consistent=False, sleep=time.sleep, unread=None):
    """{cacheKey: item} for the keys whose item exists; a missing or unreadable item is absent.
    Eventually consistent unless `consistent` (only the forced-refresh lock reads ask for it). One
    key is one GetItem; more keys are BatchGetItem requests of MAX_KEYS_PER_CALL keys each. The keys
    whose read failed (batch and GetItem fallback) are added to `unread` (when given)."""
    keys = list(dict.fromkeys(keys))
    if not keys:
        return {}
    client = _batch_client(table)
    if len(keys) == 1 or client is None:
        return _get_each(table, keys, consistent, unread)
    found, left = {}, []
    for i in range(0, len(keys), MAX_KEYS_PER_CALL):
        # Not named `unread`: that is the caller's list, which the GetItem fallback below fills.
        items, left_chunk = _batch_chunk(client, table.name, keys[i:i + MAX_KEYS_PER_CALL], consistent, sleep)
        found.update(items)
        left += left_chunk
    if left:
        found.update(_get_each(table, left, consistent, unread))
    return found
