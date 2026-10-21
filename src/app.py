"""
ResolveGatewayAddress - a task in the innomesh-innomeshui-deployMonitoringRoom-v1
state machine.

Runs immediately after "Save asset metadata" (innomesh-cloudcontrol-pushAssetMetadata-v1),
which is what creates the assets.{namespace} items. For every device in the room it
resolves the Gateway Device association an operator authored in Room Manager and writes
exactly five attributes onto that device's asset item:

    gateway_address, gateway_port, gateway_username, gateway_password, gateway_encrypted

Nothing else on the item is ever touched.

Implements WS5 of "RFC: Gateway Device Association for Innomesh Sight" (section 3.2.2).
"""

import base64
import gzip
import ipaddress
import json
import logging
import traceback

import requests
from Crypto.Cipher import PKCS1_OAEP
from Crypto.PublicKey import RSA

from config import Category, get_kms_public_key
from dynamo_db_api import get_asset_credentials, update_gateway_attributes

log = logging.getLogger("CloudControlAPI")

ARTEFACT_TIMEOUT = 30

WARMUP_SOURCE = "innomesh-aws-lambdas-warmup"


# --------------------------------------------------------------------------
# Encryption - identical to pushAssetMetadata's build_rsa_cipher /
# get_asset_encrypted_credentials, so the worker's existing decryption
# handles a gateway password with no change.
# --------------------------------------------------------------------------


def build_rsa_cipher(public_key_str):
    pub_key_decoded = base64.b64decode(public_key_str)
    pub_key = RSA.import_key(pub_key_decoded)
    return PKCS1_OAEP.new(key=pub_key)


def encrypt_password(cipher, password):
    """
    base64url(RSA-OAEP-SHA1(utf8(password))), the form the asset item stores.

    Returns (stored_value, is_encrypted). An empty password is valid and
    common - EvertzAV NUCLEUS has no auth - and is stored as an empty string
    with is_encrypted False.
    """
    if not password:
        return "", False
    encrypted_password = cipher.encrypt(password.encode("utf-8"))
    return base64.urlsafe_b64encode(encrypted_password).decode("ASCII"), True


# --------------------------------------------------------------------------
# Artefact handling
# --------------------------------------------------------------------------


def base64_decode_and_gunzip(base64_data):
    try:
        gzipped_data = base64.b64decode(base64_data)
        json_bytes = gzip.decompress(gzipped_data)
        return json.loads(json_bytes.decode("utf-8"))
    except Exception as e:
        print("Error occurred while decompressing data:", str(e))
        return None


def fetch_artefact(presigned_url):
    """
    Download and decode a room artefact.

    The URL is a bearer credential for the object, so it is never printed and
    never put in an exception message.
    """
    response = requests.get(presigned_url, timeout=ARTEFACT_TIMEOUT)
    response.raise_for_status()
    return base64_decode_and_gunzip(response.text)


def normalise_room_id(room_id):
    """
    Strip the prefixes pushAssetMetadata strips before using a room id as the
    assets.{namespace} partition key. Without stripping them identically,
    every update_item here would address a key that does not exist.
    """
    room_id = (room_id or "").strip()
    return room_id.removeprefix("cloud-").removeprefix("monitoring-")


# --------------------------------------------------------------------------
# Device list
# --------------------------------------------------------------------------


def normalise_device(device):
    """
    One device entry from devices[] or asset_metadata[], in one shape.

    The two arrays spell identity differently - devices[] uses id/name,
    asset_metadata[] uses asset_id/asset_name - and some rooms carry the
    address in DevicePulse's "host" field instead of hostname/ip_address.
    """
    out = dict(device)
    out["id"] = str(device.get("id") or device.get("asset_id") or "").strip()
    out["name"] = device.get("name") or device.get("asset_name") or ""

    hostname = str(device.get("hostname") or "").strip()
    ip_address = str(device.get("ip_address") or "").strip()
    host = str(device.get("host") or "").strip()
    if not hostname and not ip_address and host:
        if is_ip_literal(host):
            ip_address = host
        else:
            hostname = host
    out["hostname"] = hostname
    out["ip_address"] = ip_address

    # room.json device entries carry "port"; some asset-side projections
    # carry "ip_port".
    port = device.get("port")
    if port in (None, ""):
        port = device.get("ip_port")
    out["port"] = to_port(port)
    return out


def build_device_list(monitoring_config):
    """
    The room's devices, with devices[] authoritative and asset_metadata[]
    filling any gateway field it left empty.

    RFC assumption A-01 is that the pipeline builds asset items from
    asset_metadata[] rather than devices[]; reading both makes this correct
    whichever array Room Manager wrote the gateway fields to.
    """
    devices = []
    index = {}

    for entry in monitoring_config.get("devices") or []:
        device = normalise_device(entry)
        devices.append(device)
        if device["id"] and device["id"] not in index:
            index[device["id"]] = len(devices) - 1

    for entry in monitoring_config.get("asset_metadata") or []:
        device = normalise_device(entry)
        if not device["id"]:
            continue
        position = index.get(device["id"])
        if position is None:
            devices.append(device)
            index[device["id"]] = len(devices) - 1
            continue
        devices[position] = merge_gateway_fields(devices[position], device)

    return devices


def merge_gateway_fields(primary, secondary):
    """devices[] wins where it carries a value; asset_metadata[] fills gaps."""
    for field in ("gateway_device", "gateway_address", "gateway_username", "gateway_password"):
        if not str(primary.get(field) or "").strip():
            primary[field] = secondary.get(field)
    if primary.get("gateway_port") in (None, ""):
        primary["gateway_port"] = secondary.get("gateway_port")
    if not primary.get("clear"):
        primary["clear"] = secondary.get("clear")
    for field in ("hostname", "ip_address"):
        if not str(primary.get(field) or "").strip():
            primary[field] = secondary.get(field)
    if primary.get("port") is None:
        primary["port"] = secondary.get("port")
    return primary


# --------------------------------------------------------------------------
# Address and port helpers
# --------------------------------------------------------------------------


def is_ip_literal(value):
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def valid_hostname(value):
    """
    Whether to treat a value as a DNS name rather than an IP literal.

    Matches the Sight worker's transport.ValidHostname - non-empty and
    containing a dot - plus the qualification the RFC makes explicit, that an
    IP literal is not a hostname. Without that, a gateway device's
    ip_address would be copied into the hostname slot.
    """
    value = str(value or "").strip()
    if not value or value.startswith("["):
        return False
    if is_ip_literal(value):
        return False
    return "." in value


def host_of(hostname, ip_address):
    """The address a driver dials: hostname when usable, else ip_address."""
    if valid_hostname(hostname):
        return str(hostname).strip()
    return str(ip_address or "").strip()


def to_port(value):
    """
    Normalise a port to an int, or None meaning "driver default".

    gateway_port is specified as "int or null"; a plain 0 cannot express
    that, so an absent or blank port stays None.
    """
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

CLEARED = {
    "gateway_address": "",
    "gateway_port": None,
    "gateway_username": "",
    "gateway_password": "",
    "gateway_encrypted": False,
}


def resolve_via_gateway_device(device, gateway, credentials):
    """
    Resolve from another device in the same room.

    Address, port and the hostname-or-IP choice come from the gateway's
    room.json entry, which is the authored source of truth for addresses.
    Credentials come from that device's own asset item, copied verbatim so
    the password stays in exactly the form the gateway asset stores it.
    """
    address = host_of(gateway.get("hostname"), gateway.get("ip_address"))
    if not address:
        return None

    return {
        "gateway_address": address,
        "gateway_port": gateway.get("port"),
        "gateway_username": credentials.get("username", "") or "",
        "gateway_password": credentials.get("password", "") or "",
        "gateway_encrypted": bool(credentials.get("encrypted", False)),
    }


def resolve_manual(device, cipher):
    """
    Resolve from the four manually authored fields.

    The password arrives in plaintext from room.json and is encrypted here
    with the same routine the asset-persist state uses for the device's own
    password. A device flagged "clear" opts out of encryption, exactly as its
    own password does.
    """
    address = str(device.get("gateway_address") or "").strip()
    if not address:
        return None

    password = device.get("gateway_password") or ""
    if not password or device.get("clear"):
        # An empty password is valid and common - EvertzAV NUCLEUS has no
        # auth - and "clear" opts out of encryption. Neither needs a cipher,
        # so a room of manual gateways without credentials resolves even
        # when the tenant has no RSA key.
        stored, is_encrypted = password, False
    else:
        if not cipher:
            raise ValueError("a manual gateway password was authored but the tenant has no RSA public key")
        stored, is_encrypted = encrypt_password(cipher, password)

    return {
        "gateway_address": address,
        "gateway_port": to_port(device.get("gateway_port")),
        "gateway_username": str(device.get("gateway_username") or "").strip(),
        "gateway_password": stored,
        "gateway_encrypted": is_encrypted,
    }


def resolve_room_gateways(room_id, devices, table_name, cipher):
    """
    Apply the resolution rules across a room and write the result.

    Every device with an id gets exactly one update, including devices with
    no gateway: those are written cleared, which is what makes removing a
    Gateway Device in Room Manager take effect on redeploy rather than
    leaving a stale address behind.

    Only one hop is resolved. A gateway device may itself carry
    gateway_device; the chain is never followed.
    """
    by_id = {}
    for device in devices:
        if device["id"] and device["id"] not in by_id:
            by_id[device["id"]] = device

    counts = {"resolved": 0, "manual": 0, "cleared": 0, "dangling": 0, "missing": 0, "skipped": 0}
    credentials_cache = {}
    seen = set()

    for device in devices:
        asset_id = device["id"]
        if not asset_id:
            print(f"<warn> room {room_id}: a device entry has no id and cannot be keyed")
            counts["skipped"] += 1
            continue
        if asset_id in seen:
            print(f"<warn> room {room_id}: device {asset_id} is repeated; only the first entry is resolved")
            counts["skipped"] += 1
            continue
        seen.add(asset_id)

        resolved, source = resolve_device(room_id, device, by_id, table_name, cipher, credentials_cache, counts)

        item_id = {"room_id": normalise_room_id(device.get("room_id")) or room_id, "asset_id": asset_id}
        if update_gateway_attributes(table_name, item_id, resolved):
            counts[source] += 1
        else:
            counts["missing"] += 1
            print(
                f"<warn> room {item_id['room_id']}: asset {asset_id} has no asset item; gateway attributes not written"
            )

    return counts


def resolve_device(room_id, device, by_id, table_name, cipher, credentials_cache, counts):
    """
    The three-way rule for one device. Returns (attributes, counter name).

    A gateway_device that cannot be used - dangling, self-referential, or
    pointing at a device with no address - is treated as unset per the RFC,
    which lets a manual address on the same device still apply and otherwise
    clears the gateway.
    """
    asset_id = device["id"]
    reference = str(device.get("gateway_device") or "").strip()

    if reference:
        problem = None
        if reference == asset_id:
            problem = "a device cannot be its own gateway"
        elif reference not in by_id:
            problem = "gateway_device names a device that is not in this room"

        if problem is None:
            gateway = by_id[reference]
            credentials = lookup_credentials(room_id, reference, by_id, table_name, credentials_cache)
            resolved = resolve_via_gateway_device(device, gateway, credentials)
            if resolved is None:
                problem = "gateway device has neither a usable hostname nor an ip_address"
            else:
                return resolved, "resolved"

        counts["dangling"] += 1
        print(
            f"<warn> room {room_id}: device {asset_id} gateway_device "
            f"{reference!r} unusable - {problem}; treated as unset"
        )

    manual = resolve_manual(device, cipher)
    if manual is not None:
        return manual, "manual"

    return dict(CLEARED), "cleared"


def lookup_credentials(room_id, reference, by_id, table_name, cache):
    """Credentials off the gateway device's own asset item, read once."""
    if reference in cache:
        return cache[reference]

    gateway = by_id[reference]
    item_id = {
        "room_id": normalise_room_id(gateway.get("room_id")) or room_id,
        "asset_id": reference,
    }
    credentials = get_asset_credentials(table_name, item_id)
    if not credentials:
        print(f"<warn> room {room_id}: gateway device {reference} has no asset item; gateway credentials written empty")
    cache[reference] = credentials
    return credentials


# --------------------------------------------------------------------------
# Step function entry point
# --------------------------------------------------------------------------


def load_monitoring_config(payload):
    """
    The room config the authored gateway fields live in.

    The monitoring artefact is the source of truth. asset_metadata is the
    fallback for a payload that carried only that, so the state still
    resolves rather than failing the deployment.
    """
    monitoring_url = payload.get("monitoring")
    if monitoring_url:
        config = fetch_artefact(monitoring_url)
        if config and (config.get("devices") or config.get("asset_metadata")):
            return config
        print("<warn> the monitoring config artefact carried no devices")

    asset_metadata_url = payload.get("asset_metadata")
    if asset_metadata_url:
        asset_metadata = fetch_artefact(asset_metadata_url)
        if asset_metadata:
            return {"asset_metadata": asset_metadata}

    return None


def dispatch(event):
    room_id = event.get("room")
    tenant = event.get("tenant")
    namespace = event.get("namespace")
    payload = event.get("payload")

    if not payload:
        return {"error": True, "payload": payload, "message": "Missing payload in event"}
    if not room_id:
        return {"error": True, "payload": payload, "message": "Missing room in event"}
    if not tenant:
        return {"error": True, "payload": payload, "message": "Missing tenant in event"}
    if not namespace:
        return {"error": True, "payload": payload, "message": "Missing namespace in event"}

    try:
        monitoring_config = load_monitoring_config(payload)
        if not monitoring_config:
            return {
                "error": True,
                "payload": payload,
                "message": "Failed to resolve gateway addresses: no monitoring config or asset metadata artefact",
            }

        devices = build_device_list(monitoring_config)

        # The tenant key is only needed when a device authors a manual
        # gateway password, so a tenant without one is not blocked.
        cipher = None
        if any(d.get("gateway_password") and not d.get("clear") for d in devices):
            public_key_str = get_kms_public_key(tenant)
            if not public_key_str:
                return {
                    "error": True,
                    "payload": payload,
                    "message": f"Failed to resolve gateway addresses: no KMS public key for tenant {tenant}",
                }
            cipher = build_rsa_cipher(public_key_str)

        table_name = Category.ASSETS + "." + namespace
        counts = resolve_room_gateways(normalise_room_id(room_id), devices, table_name, cipher)

        summary = (
            f"resolved_gateways={counts['resolved']} manual={counts['manual']} "
            f"cleared={counts['cleared']} dangling={counts['dangling']}"
        )
        print(f"Gateway resolution for room {normalise_room_id(room_id)}: {summary}")
        if counts["missing"] or counts["skipped"]:
            print(f"<warn> assets_missing={counts['missing']} skipped_devices={counts['skipped']}")

        return {"error": False, "payload": payload, "message": f"Gateway addresses resolved: {summary}"}
    except Exception as e:
        tb = traceback.format_exc()
        print(f"Error occurred while resolving gateway addresses: {str(e)}")
        print(tb)
        return {
            "error": True,
            "payload": payload,
            "message": f"Failed to resolve gateway addresses: {str(e)}",
        }


def lambda_handler(event, context):
    """lambda handler"""
    # early return call when the function is called by warmup plugin
    if event.get("source") == WARMUP_SOURCE:
        print("WarmUp - Lambda is warm!")
        return {}
    try:
        return dispatch(event)
    except Exception as exc:
        log.exception(exc)
        raise
