# innomesh-cloudcontrol-ResolveGatewayAddress-v1

A task in the **`innomesh-innomeshui-deployMonitoringRoom-v1`** step function.

It runs immediately after `Save asset metadata`
(`innomesh-cloudcontrol-pushAssetMetadata-v1`), which is what creates the
`assets.{namespace}` items. For every device in the room it resolves the
Gateway Device association an operator authored in Room Manager and writes
exactly five attributes onto that device's asset item:

| Attribute | Type | Meaning |
|---|---|---|
| `gateway_address` | string | Host the Sight worker dials for this device's status |
| `gateway_port` | int or null | `null` means "driver default" |
| `gateway_username` | string | Empty is valid — EvertzAV NUCLEUS has no auth |
| `gateway_password` | string | Encrypted when `gateway_encrypted` is true |
| `gateway_encrypted` | bool | Whether `gateway_password` is ciphertext |

Nothing else on the item is ever touched.

Implements **WS5** of *RFC: Gateway Device Association for Innomesh Sight*
(§3.2.2).

## Why it exists

Sight polls each device at its own address. That breaks for devices with no
usable API of their own — EvertzAV NUCLEUS UXP units get all their state from
the session manager; Barco NGS encoders and decoders get inventory and health
from the CTRL server. The worker used to paper over this with a hardcoded
controller IP, which allowed exactly one controller per worker build.

This lambda is what replaces that constant: the controller's address and
credentials now travel through the pipeline as data, per device, so a tenant
can run more than one.

## Resolution rules

Per device, in order:

1. **`gateway_device` is set and resolves** — the gateway is another device in
   the same room. `gateway_address` is that device's `hostname` when it is a
   usable DNS name, otherwise its `ip_address`; `gateway_port` is its port; the
   credentials and `encrypted` flag are copied from **its own asset item**,
   because room.json does not carry stored credentials.
2. **A manual `gateway_address` is set** — the four authored fields are used as
   given. The password arrives in plaintext from room.json and is encrypted
   here with the same routine `pushAssetMetadata` uses for the device's own
   password.
3. **Neither** — all five attributes are written **empty**. This is what makes
   *clearing* a Gateway Device in Room Manager take effect on redeploy rather
   than leaving a stale address behind.

Only **one hop** is resolved. A gateway device may itself carry
`gateway_device`; the chain is never followed.

A `gateway_device` that cannot be used — naming a device outside the room,
naming itself, or naming a device with no address — is treated as unset and
logged as a WARN with the room, device and dangling reference.

## Step function contract

Invoked with the same Payload shape as its neighbour `Save asset metadata`:

```json
{
  "code": "...", "correlation_id": "...", "metadata": { },
  "namespace": "curtin.uat.innomesh.com.au", "payload": { },
  "retry": false, "room": "monitoring-b1g12", "tenant": "curtin"
}
```

and returns the shape that state's `Assign` block reads:

```json
{ "error": false, "payload": { }, "message": "Gateway addresses resolved: ..." }
```

`payload` is echoed back **verbatim** — the state machine assigns
`subtask_payload` from it, so dropping a field would break a later task.

Failure is signalled with `error: true`, not a raised exception: the existing
`Encountered Error?` choice routes that to `Log deployment fail`. A raised
exception would instead hit the state's `Retry` block and re-run the writes.

See [docs/state-machine-wiring.md](docs/state-machine-wiring.md) for where the
state goes and the IAM it needs.

## Encryption (RFC A-07)

A device password on an asset item is:

```
base64url( RSA-OAEP-SHA1( utf8(password), tenant RSA public key ) )
```

written by `pushAssetMetadata` with pycryptodome `PKCS1_OAEP`, and read by
`innosight-worker-v2` with `rsa.DecryptOAEP(sha1.New(), …)`. The tenant public
key is the SSM SecureString
`/cap-innomesh-v1/{tenant}/aws/kms/{env}/public-key`, holding base64 of a DER
key. KMS protects the key material, never an individual password.

This lambda reuses that routine verbatim, and `tests/test_encryption.py`
encrypts with this code and decrypts with the worker's routine written out
longhand — so a change to either side fails the build rather than silently
orphaning every stored credential.

## Storage form: "value|version"

`pushAssetMetadata` has an explicit payload dict for the fields it knows
about, and a catch-all for everything else that stores them as
`"value|version"`. The five `gateway_*` fields are not in that dict, so when
Room Manager mirrors them into `asset_metadata[]` they go through the
catch-all and land in that form.

This lambda runs moments later against the same item, so it stores them the
same way rather than overwriting the convention on every deploy. The branches
are reproduced exactly:

| State of the attribute | Stored as |
|---|---|
| item does not exist yet | `"value\|0"` |
| existing `"v\|N"` | `"value\|N"` — the version is preserved |
| existing `"v"` | `"value\|0"` |
| existing non-string, or absent | the raw value |

The last row is the common first-deploy case: asset persistence has just
created the item and no gateway attribute is on it yet, so the values are
written raw.

> **Downstream note.** The RFC's A.4 example shows these fields clean
> (`"gateway_address": "nuc-av-b1.curtin.edu.au"`), and the Sight worker and
> coordination engine read them as a plain string, int and bool. Where the
> pipe form applies, those consumers need to strip the `|N` suffix the same
> way the Portal asset views already do for `ip_port`. Raised with the RFC
> author.

## Behaviour worth knowing

- **Room id normalisation.** `pushAssetMetadata` strips `cloud-` and
  `monitoring-` before using a room id as the partition key. This lambda
  strips them identically; without that, every `update_item` would address a
  key that does not exist.
- **Writes are conditional** on the item already existing. `update_item`
  creates an item when the key is absent, which here would invent an asset out
  of a room.json device the asset-persist state chose not to store. A missing
  item is counted and logged, not treated as a failure.
- **Both device arrays are read.** RFC assumption A-01 is that the pipeline
  builds asset items from `asset_metadata[]` rather than `devices[]`.
  `devices[]` wins where it carries a value and `asset_metadata[]` fills the
  gaps, so this is correct whichever array Room Manager wrote to.
- **The tenant key is read only when needed.** A room whose gateways are all
  other devices, or which authors no gateway password, never touches SSM — so
  a tenant without a key is not blocked.
- **Passwords are never logged**, in plaintext or ciphertext, and neither are
  presigned artefact URLs — both are asserted by tests.
- **Idempotent.** It runs on every deploy, which is what propagates a changed
  gateway address. Note the RSA padding is randomised, so a redeploy rewrites
  `gateway_password` with fresh bytes: semantically idempotent, not
  byte-idempotent.

## Commands

```bash
pip install -r src/requirements.txt -r requirements-dev.txt
python -m pytest          # 87 tests
python -m ruff check .
python -m ruff format --check .
```

## Layout

```
src/app.py             entry point, artefact decode, resolution rules
src/config.py          tenant RSA public key from SSM
src/dynamo_db_api.py   the only reader/writer of assets.{namespace}
tests/                 unit tests incl. encryption parity with the worker
```
