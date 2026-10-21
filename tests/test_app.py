import os
import sys
from unittest.mock import MagicMock, patch

import pytest

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    from app import (
        build_device_list,
        host_of,
        lambda_handler,
        normalise_device,
        normalise_room_id,
        resolve_room_gateways,
        to_port,
        valid_hostname,
    )


SESSION_MANAGER = {
    "id": "dev.matrix.1",
    "name": "Matrix 1",
    "brand": "EvertzAV",
    "hostname": "nuc-av-b1.curtin.edu.au",
    "ip_address": "10.75.219.45",
    "gateway_capable": True,
}

TRANSCEIVER = {
    "id": "dev.transceiver.1",
    "name": "Transceiver 1",
    "brand": "EvertzAV",
    "ip_address": "10.75.219.101",
    "serial_number": "7261010045",
    "gateway_device": "dev.matrix.1",
}


class TestLambdaHandler:
    def test_warmup_event(self):
        event = {"source": "innomesh-aws-lambdas-warmup"}
        assert lambda_handler(event, {}) == {}

    @patch("app.dispatch")
    def test_normal_event(self, mock_dispatch):
        mock_dispatch.return_value = {"error": False, "message": "Success"}
        event = {"room": "test-room", "tenant": "test-tenant"}
        assert lambda_handler(event, {}) == {"error": False, "message": "Success"}
        mock_dispatch.assert_called_once_with(event)


class TestNormaliseRoomId:
    """pushAssetMetadata strips these before keying the table; so must this."""

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("monitoring-b1g12", "b1g12"),
            ("cloud-b1g12", "b1g12"),
            ("b1g12", "b1g12"),
            ("  monitoring-x  ", "x"),
            ("", ""),
            (None, ""),
            ("monitoringb1g12", "monitoringb1g12"),
            ("cloud-monitoring-x", "x"),
            ("monitoring-cloud-x", "cloud-x"),
        ],
    )
    def test_prefixes(self, given, expected):
        assert normalise_room_id(given) == expected


class TestHostSelection:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("nuc-av-b1.curtin.edu.au", True),
            ("host.example.com.", True),
            ("nuc-av-b1", False),
            ("localhost", False),
            ("10.75.219.45", False),
            ("::1", False),
            ("[2001:db8::1]", False),
            ("", False),
            (None, False),
        ],
    )
    def test_valid_hostname(self, value, expected):
        assert valid_hostname(value) is expected

    @pytest.mark.parametrize(
        "hostname,ip,expected",
        [
            ("nuc.example.com", "10.0.0.1", "nuc.example.com"),
            ("", "10.0.0.1", "10.0.0.1"),
            ("nuc", "10.0.0.1", "10.0.0.1"),
            ("10.0.0.9", "10.0.0.1", "10.0.0.1"),
            ("", "", ""),
        ],
    )
    def test_host_of(self, hostname, ip, expected):
        assert host_of(hostname, ip) == expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            (8080, 8080),
            ("8080", 8080),
            (" 8080 ", 8080),
            (None, None),
            ("", None),
            ("http", None),
            (0, 0),
        ],
    )
    def test_to_port(self, value, expected):
        assert to_port(value) == expected


class TestDeviceList:
    def test_host_field_fallback(self):
        assert normalise_device({"id": "d", "host": "nuc.example.com"})["hostname"] == "nuc.example.com"
        assert normalise_device({"id": "d", "host": "10.0.0.1"})["ip_address"] == "10.0.0.1"

    def test_asset_metadata_spellings(self):
        config = {
            "devices": [SESSION_MANAGER, {"id": "dev.transceiver.1", "ip_address": "10.0.0.1"}],
            "asset_metadata": [
                {"asset_id": "dev.transceiver.1", "gateway_device": "dev.matrix.1"},
                {"asset_id": "dev.encoder.9", "asset_name": "Encoder 9", "gateway_address": "ctrl.example.com"},
            ],
        }
        devices = {d["id"]: d for d in build_device_list(config)}
        assert devices["dev.transceiver.1"]["gateway_device"] == "dev.matrix.1"
        assert devices["dev.encoder.9"]["gateway_address"] == "ctrl.example.com"

    def test_devices_array_wins(self):
        config = {
            "devices": [{"id": "dev.a", "gateway_address": "authored.example.com"}],
            "asset_metadata": [
                {"asset_id": "dev.a", "gateway_address": "mirrored.example.com", "gateway_username": "from-mirror"}
            ],
        }
        device = build_device_list(config)[0]
        assert device["gateway_address"] == "authored.example.com"
        assert device["gateway_username"] == "from-mirror"


class TestResolveRoomGateways:
    """The three-way rule, with the writer and credential read stubbed."""

    def run(self, devices, credentials=None, cipher=None):
        writes = {}
        creds = credentials or {}

        def fake_update(table_name, item_id, payload):
            writes[item_id["asset_id"]] = (item_id, payload)
            return True

        def fake_get(table_name, item_id):
            return creds.get(item_id["asset_id"], {})

        with (
            patch("app.update_gateway_attributes", side_effect=fake_update),
            patch("app.get_asset_credentials", side_effect=fake_get),
        ):
            counts = resolve_room_gateways("b1g12", build_device_list({"devices": devices}), "assets.ns", cipher)
        return counts, writes

    def test_gateway_device(self):
        counts, writes = self.run(
            [SESSION_MANAGER, TRANSCEIVER],
            credentials={"dev.matrix.1": {"username": "admin", "password": "ct", "encrypted": True}},
        )
        _, resolved = writes["dev.transceiver.1"]
        assert resolved["gateway_address"] == "nuc-av-b1.curtin.edu.au"
        assert resolved["gateway_port"] is None
        assert resolved["gateway_username"] == "admin"
        assert resolved["gateway_password"] == "ct"
        assert resolved["gateway_encrypted"] is True
        # the gateway itself has no gateway
        assert writes["dev.matrix.1"][1]["gateway_address"] == ""
        assert counts["resolved"] == 1 and counts["cleared"] == 1

    def test_write_is_keyed_by_normalised_room(self):
        _, writes = self.run([SESSION_MANAGER])
        assert writes["dev.matrix.1"][0] == {"room_id": "b1g12", "asset_id": "dev.matrix.1"}

    def test_manual_gateway_without_password(self):
        device = {
            "id": "dev.a",
            "ip_address": "10.0.0.1",
            "gateway_address": "  ctrl.example.com  ",
            "gateway_port": 443,
            "gateway_username": "  svc  ",
        }
        counts, writes = self.run([device])
        _, resolved = writes["dev.a"]
        assert resolved["gateway_address"] == "ctrl.example.com"
        assert resolved["gateway_port"] == 443
        assert resolved["gateway_username"] == "svc"
        assert resolved["gateway_password"] == ""
        assert resolved["gateway_encrypted"] is False
        assert counts["manual"] == 1

    def test_manual_gateway_password_is_encrypted(self):
        cipher = MagicMock()
        cipher.encrypt.return_value = b"cipherbytes"
        device = {"id": "dev.a", "gateway_address": "ctrl.example.com", "gateway_password": "s3cr3t"}
        _, writes = self.run([device], cipher=cipher)
        _, resolved = writes["dev.a"]
        assert resolved["gateway_encrypted"] is True
        assert resolved["gateway_password"] and resolved["gateway_password"] != "s3cr3t"
        cipher.encrypt.assert_called_once_with(b"s3cr3t")

    def test_clear_opts_out_of_encryption(self):
        cipher = MagicMock()
        device = {"id": "dev.a", "clear": True, "gateway_address": "ctrl.example.com", "gateway_password": "s3cr3t"}
        _, writes = self.run([device], cipher=cipher)
        _, resolved = writes["dev.a"]
        assert resolved["gateway_password"] == "s3cr3t"
        assert resolved["gateway_encrypted"] is False
        cipher.encrypt.assert_not_called()

    def test_no_gateway_is_written_cleared(self):
        _, writes = self.run([{"id": "dev.a", "ip_address": "10.0.0.1"}])
        _, resolved = writes["dev.a"]
        assert resolved == {
            "gateway_address": "",
            "gateway_port": None,
            "gateway_username": "",
            "gateway_password": "",
            "gateway_encrypted": False,
        }

    @pytest.mark.parametrize(
        "gateway_device,extra_devices",
        [
            ("dev.matrix.99", []),
            ("dev.a", []),
            ("dev.noaddress", [{"id": "dev.noaddress", "hostname": "", "ip_address": ""}]),
        ],
    )
    def test_unusable_reference_is_cleared_and_counted(self, gateway_device, extra_devices):
        device = {"id": "dev.a", "ip_address": "10.0.0.1", "gateway_device": gateway_device}
        counts, writes = self.run([device] + extra_devices)
        assert writes["dev.a"][1]["gateway_address"] == ""
        assert counts["dangling"] == 1

    def test_unusable_reference_falls_back_to_manual(self):
        device = {"id": "dev.a", "gateway_device": "dev.missing", "gateway_address": "fallback.example.com"}
        counts, writes = self.run([device])
        assert writes["dev.a"][1]["gateway_address"] == "fallback.example.com"
        assert counts["manual"] == 1 and counts["dangling"] == 1

    def test_gateway_device_wins_over_manual(self):
        device = dict(TRANSCEIVER, gateway_address="ignored.example.com", gateway_port=9999)
        _, writes = self.run([SESSION_MANAGER, device], credentials={"dev.matrix.1": {"username": "admin"}})
        resolved = writes["dev.transceiver.1"][1]
        assert resolved["gateway_address"] == "nuc-av-b1.curtin.edu.au"
        assert resolved["gateway_port"] is None

    def test_missing_gateway_credentials_still_resolves_address(self):
        counts, writes = self.run([SESSION_MANAGER, TRANSCEIVER], credentials={})
        resolved = writes["dev.transceiver.1"][1]
        assert resolved["gateway_address"] == "nuc-av-b1.curtin.edu.au"
        assert resolved["gateway_username"] == "" and resolved["gateway_password"] == ""
        assert counts["dangling"] == 0

    def test_only_one_hop_is_followed(self):
        upstream = {"id": "dev.upstream", "hostname": "upstream.example.com"}
        middle = {"id": "dev.middle", "hostname": "middle.example.com", "gateway_device": "dev.upstream"}
        leaf = {"id": "dev.leaf", "gateway_device": "dev.middle"}
        _, writes = self.run([upstream, middle, leaf])
        assert writes["dev.leaf"][1]["gateway_address"] == "middle.example.com"
        assert writes["dev.middle"][1]["gateway_address"] == "upstream.example.com"

    def test_device_without_id_and_duplicates_are_skipped(self):
        counts, writes = self.run(
            [
                {"name": "Nameless"},
                {"id": "dev.dupe", "gateway_address": "first.example.com"},
                {"id": "dev.dupe", "gateway_address": "second.example.com"},
            ]
        )
        assert writes["dev.dupe"][1]["gateway_address"] == "first.example.com"
        assert counts["skipped"] == 2

    def test_missing_asset_item_is_counted_not_fatal(self):
        with (
            patch("app.update_gateway_attributes", return_value=False),
            patch("app.get_asset_credentials", return_value={}),
        ):
            counts = resolve_room_gateways(
                "b1g12", build_device_list({"devices": [SESSION_MANAGER]}), "assets.ns", None
            )
        assert counts["missing"] == 1 and counts["cleared"] == 0
