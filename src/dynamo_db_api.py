import boto3
from boto3.dynamodb.conditions import Attr

dynamodb = boto3.resource("dynamodb")

# The exact, closed set of attributes this lambda is allowed to write. The
# asset item is shared with the asset-persist state and the live status
# writers, so touching anything else here would clobber their work.
GATEWAY_ATTRIBUTES = (
    "gateway_address",
    "gateway_port",
    "gateway_username",
    "gateway_password",
    "gateway_encrypted",
)

# Credential attributes read back off a gateway device's own asset item.
# room.json does not carry stored credentials, which is why they come from
# here rather than from the monitoring config.
CREDENTIAL_ATTRIBUTES = ("username", "password", "encrypted")


def get_asset_credentials(table_name, item_id):
    """
    Read username, password and encrypted off one asset item.

    Returns {} when the item does not exist - a gateway device that has not
    been persisted yet is not fatal, the address still resolves and the
    caller logs a warning.

    Only the three credential attributes are projected, so this is the only
    place the lambda ever reads a stored secret.
    """
    try:
        table = dynamodb.Table(table_name)
        response = table.get_item(
            Key=item_id,
            ProjectionExpression="#u, #p, #e",
            ExpressionAttributeNames={"#u": "username", "#p": "password", "#e": "encrypted"},
        )
        return response.get("Item") or {}
    except Exception as e:
        print(f"Error occurred while reading gateway credentials for {item_id}: {str(e)}")
        raise


def get_asset_gateway_attributes(table_name, item_id):
    """
    The five gateway_* attributes as they currently stand on an asset item,
    and whether the item exists at all.

    Both are needed to reproduce pushAssetMetadata's "value|version"
    convention: it keys off whether the item exists, and off the type and
    shape of the value already there.

    Returns (exists, attributes).
    """
    try:
        table = dynamodb.Table(table_name)
        names = {f"#{k}": k for k in GATEWAY_ATTRIBUTES}
        response = table.get_item(
            Key=item_id,
            ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names,
        )
    except Exception as e:
        print(f"Error occurred while reading gateway attributes for {item_id}: {str(e)}")
        raise

    if "Item" not in response:
        return False, {}
    return True, response["Item"]


def update_gateway_attributes(table_name, item_id, payload):
    """
    Set exactly the five gateway_* attributes on one asset item.

    Conditional on the item already existing. update_item creates an item
    when the key is absent, which here would invent an asset out of a
    room.json device the asset-persist state chose not to store. A missing
    item returns False so the caller can count and log it rather than fail
    the deployment.
    """
    unexpected = set(payload) - set(GATEWAY_ATTRIBUTES)
    if unexpected:
        raise ValueError(f"refusing to write non-gateway attributes: {sorted(unexpected)}")

    table = dynamodb.Table(table_name)
    update_expr = "SET " + ", ".join(f"#{k} = :{k}" for k in payload)
    expr_attr_names = {f"#{k}": k for k in payload}
    expr_attr_values = {f":{k}": v for k, v in payload.items()}
    try:
        table.update_item(
            Key=item_id,
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values,
            ConditionExpression=Attr("room_id").exists(),
            ReturnValues="NONE",
        )
        return True
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as e:
        # Never echo the payload: it carries gateway_password.
        print(f"Error occurred while updating gateway attributes for {item_id}: {str(e)}")
        raise
