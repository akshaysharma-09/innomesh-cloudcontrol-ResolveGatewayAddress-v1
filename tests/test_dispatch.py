"""
The contract with innomesh-innomeshui-deployMonitoringRoom-v1.

The state machine invokes this lambda with the same Payload shape as the
neighbouring "Save asset metadata" state, and its Assign block reads
error_flag, message and subtask_payload back off the result. Breaking either
half breaks the deployment loop, so both are pinned here.
"""

import base64
import gzip
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    from app import base64_decode_and_gunzip, dispatch


# The payload the state machine passes through for the
# generic-save-asset-metadata task: subtask_payload is the request_payload
# the Room Manager create/update lambda put on the execution.
REQUEST_PAYLOAD = {
    "tenant": "curtin",
    "ddi_enabled": False,
    "namespace": "curtin.uat.innomesh.com.au",
    "room_id": "monitoring-b1g12",
    "room_name": "B1 G12",
    "cluster": "zone-1",
    "correlation_id": "0f5d9c1e",
    "monitoring": "https://s3.example/monitoring-config.json.gz.b64?X-Amz-Signature=SECRET",
    "asset_metadata": "https://s3.example/asset_metadata.json.gz.b64?X-Amz-Signature=SECRET",
    "operation": "update",
    "room_type": "monitoring",
}


def event(payload=None, **overrides):
    base = {
        "code": "generic-resolve-gateway-address",
        "correlation_id": "0f5d9c1e",
        "metadata": {"room_name": "B1 G12"},
        "namespace": "curtin.uat.innomesh.com.au",
        "retry": False,
        "room": "monitoring-b1g12",
        "tenant": "curtin",
        "payload": REQUEST_PAYLOAD if payload is None else payload,
    }
    base.update(overrides)
    return base


def pipeline_encode(document):
    """json.dumps -> gzip.compress -> base64, as the Room Manager lambdas do."""
    return base64.b64encode(gzip.compress(json.dumps(document).encode("utf-8"))).decode("utf-8")


ROOM = {
    "room_id": "b1g12",
    "devices": [
        {"id": "dev.matrix.1", "hostname": "nuc-av-b1.curtin.edu.au", "gateway_capable": True},
        {"id": "dev.transceiver.1", "ip_address": "10.75.219.101", "gateway_device": "dev.matrix.1"},
    ],
}


class TestArtefactDecoding:
    def test_round_trips_the_pipeline_encoding(self):
        assert base64_decode_and_gunzip(pipeline_encode(ROOM)) == ROOM

    def test_bad_payload_returns_none(self):
        assert base64_decode_and_gunzip("not base64 at all !!!") is None


class TestDispatchContract:
    def _run(self, ev, room=ROOM, writes=None):
        response = MagicMock()
        response.text = pipeline_encode(room)
        response.raise_for_status = MagicMock()
        recorded = writes if writes is not None else {}

        def fake_update(table_name, item_id, payload):
            recorded[item_id["asset_id"]] = (table_name, item_id, payload)
            return True

        with (
            patch("app.requests.get", return_value=response),
            patch("app.update_gateway_attributes", side_effect=fake_update),
            patch("app.get_asset_gateway_attributes", return_value=(True, {})),
            patch("app.get_asset_credentials", return_value={}),
        ):
            return dispatch(ev), recorded

    def test_success_shape(self):
        result, _ = self._run(event())
        assert result["error"] is False
        assert "resolved_gateways=1" in result["message"]
        assert "manual=0" in result["message"]
        assert "cleared=1" in result["message"]
        assert "dangling=0" in result["message"]

    def test_payload_is_echoed_verbatim(self):
        # The state machine assigns subtask_payload from this, so dropping a
        # field would break a later task.
        result, _ = self._run(event())
        assert result["payload"] == REQUEST_PAYLOAD

    def test_writes_use_the_namespaced_table_and_normalised_room(self):
        _, writes = self._run(event())
        table_name, item_id, _ = writes["dev.transceiver.1"]
        assert table_name == "assets.curtin.uat.innomesh.com.au"
        assert item_id == {"room_id": "b1g12", "asset_id": "dev.transceiver.1"}

    @pytest.mark.parametrize("missing", ["payload", "room", "tenant", "namespace"])
    def test_missing_inputs_fail_the_deployment(self, missing):
        ev = event()
        ev[missing] = None
        result = dispatch(ev)
        assert result["error"] is True
        assert result["payload"] == ev["payload"]

    def test_no_artefact_fails_the_deployment(self):
        result = dispatch(event(payload={"tenant": "curtin"}))
        assert result["error"] is True
        assert "artefact" in result["message"]

    def test_artefact_failure_fails_the_deployment_without_leaking_the_url(self):
        with patch("app.requests.get", side_effect=Exception("connection refused")):
            result = dispatch(event())
        assert result["error"] is True
        assert "SECRET" not in result["message"]
        assert result["payload"] == REQUEST_PAYLOAD

    def test_a_write_failure_fails_the_deployment(self):
        response = MagicMock()
        response.text = pipeline_encode(ROOM)
        with (
            patch("app.requests.get", return_value=response),
            patch("app.get_asset_credentials", return_value={}),
            patch("app.get_asset_gateway_attributes", return_value=(True, {})),
            patch("app.update_gateway_attributes", side_effect=Exception("throttled")),
        ):
            result = dispatch(event())
        assert result["error"] is True

    def test_manual_password_without_a_tenant_key_fails(self):
        room = {"devices": [{"id": "dev.a", "gateway_address": "ctrl.example.com", "gateway_password": "s3cr3t"}]}
        response = MagicMock()
        response.text = pipeline_encode(room)
        with (
            patch("app.requests.get", return_value=response),
            patch("app.get_kms_public_key", return_value=None),
            patch("app.get_asset_gateway_attributes", return_value=(True, {})),
            patch("app.update_gateway_attributes", return_value=True),
        ):
            result = dispatch(event())
        assert result["error"] is True
        assert "KMS public key" in result["message"]
        assert "s3cr3t" not in result["message"]

    def test_tenant_key_is_not_read_when_no_password_is_authored(self):
        with patch("app.get_kms_public_key") as mock_key:
            self._run(event())
        mock_key.assert_not_called()

    def test_falls_back_to_asset_metadata_artefact(self):
        response = MagicMock()
        response.text = pipeline_encode([{"asset_id": "dev.a", "gateway_address": "ctrl.example.com"}])
        payload = dict(REQUEST_PAYLOAD, monitoring=None)
        writes = {}

        def fake_update(table_name, item_id, payload_):
            writes[item_id["asset_id"]] = payload_
            return True

        with (
            patch("app.requests.get", return_value=response),
            patch("app.update_gateway_attributes", side_effect=fake_update),
            patch("app.get_asset_gateway_attributes", return_value=(True, {})),
            patch("app.get_asset_credentials", return_value={}),
        ):
            result = dispatch(event(payload=payload))

        assert result["error"] is False
        assert writes["dev.a"]["gateway_address"] == "ctrl.example.com"
