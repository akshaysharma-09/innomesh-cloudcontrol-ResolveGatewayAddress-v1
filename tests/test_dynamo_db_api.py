import os
import sys
from unittest.mock import MagicMock, patch

import pytest

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    import dynamo_db_api
    from dynamo_db_api import (
        GATEWAY_ATTRIBUTES,
        get_asset_credentials,
        update_gateway_attributes,
    )


RESOLVED = {
    "gateway_address": "nuc-av-b1.curtin.edu.au",
    "gateway_port": 443,
    "gateway_username": "admin",
    "gateway_password": "ciphertext",
    "gateway_encrypted": True,
}

ITEM_ID = {"room_id": "b1g12", "asset_id": "dev.transceiver.1"}


@pytest.fixture
def table():
    mock_table = MagicMock()
    with patch.object(dynamo_db_api, "dynamodb") as mock_dynamo:
        mock_dynamo.Table.return_value = mock_table
        mock_dynamo.meta.client.exceptions.ConditionalCheckFailedException = ConditionalCheckFailed
        yield mock_table


class ConditionalCheckFailed(Exception):
    pass


class TestUpdateGatewayAttributes:
    def test_writes_exactly_the_five_attributes(self, table):
        assert update_gateway_attributes("assets.ns", ITEM_ID, RESOLVED) is True

        kwargs = table.update_item.call_args.kwargs
        assert kwargs["Key"] == ITEM_ID
        assert kwargs["UpdateExpression"].startswith("SET ")
        assert kwargs["UpdateExpression"].count("=") == 5
        assert set(kwargs["ExpressionAttributeNames"].values()) == set(GATEWAY_ATTRIBUTES)
        assert kwargs["ExpressionAttributeValues"][":gateway_address"] == RESOLVED["gateway_address"]
        assert kwargs["ReturnValues"] == "NONE"

    def test_is_conditional_on_the_item_existing(self, table):
        # update_item creates an item when the key is absent, which here
        # would invent an asset the asset-persist state chose not to store.
        update_gateway_attributes("assets.ns", ITEM_ID, RESOLVED)
        assert "ConditionExpression" in table.update_item.call_args.kwargs

    def test_missing_item_returns_false(self, table):
        table.update_item.side_effect = ConditionalCheckFailed()
        assert update_gateway_attributes("assets.ns", ITEM_ID, RESOLVED) is False

    def test_other_errors_propagate_without_the_password(self, table):
        table.update_item.side_effect = Exception("ProvisionedThroughputExceeded")
        with pytest.raises(Exception) as excinfo:
            update_gateway_attributes("assets.ns", ITEM_ID, dict(RESOLVED, gateway_password="s3cr3t"))
        assert "s3cr3t" not in str(excinfo.value)

    def test_refuses_to_write_anything_else(self, table):
        with pytest.raises(ValueError, match="non-gateway"):
            update_gateway_attributes("assets.ns", ITEM_ID, dict(RESOLVED, password="oops"))
        table.update_item.assert_not_called()

    def test_cleared_values_are_written_not_omitted(self, table):
        cleared = {
            "gateway_address": "",
            "gateway_port": None,
            "gateway_username": "",
            "gateway_password": "",
            "gateway_encrypted": False,
        }
        update_gateway_attributes("assets.ns", ITEM_ID, cleared)
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":gateway_port"] is None
        assert values[":gateway_encrypted"] is False


class TestGetAssetCredentials:
    def test_projects_only_the_credential_attributes(self, table):
        table.get_item.return_value = {"Item": {"username": "admin", "password": "ct", "encrypted": True}}
        assert get_asset_credentials("assets.ns", ITEM_ID) == {"username": "admin", "password": "ct", "encrypted": True}
        kwargs = table.get_item.call_args.kwargs
        assert kwargs["ProjectionExpression"] == "#u, #p, #e"
        assert set(kwargs["ExpressionAttributeNames"].values()) == {"username", "password", "encrypted"}

    def test_missing_item_is_empty_not_fatal(self, table):
        table.get_item.return_value = {}
        assert get_asset_credentials("assets.ns", ITEM_ID) == {}
