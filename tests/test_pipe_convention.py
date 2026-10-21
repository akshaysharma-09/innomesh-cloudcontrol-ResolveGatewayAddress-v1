"""
The "value|version" storage convention.

The gateway_* fields are not in pushAssetMetadata's explicit payload dict, so
when Room Manager mirrors them into asset_metadata[] that lambda writes them
through its catch-all, in the "value|version" form. This lambda runs moments
later against the same item, so it has to store them the same way or it
would overwrite the convention on every deploy.

These tests pin each branch of pushAssetMetadata's loop:

    for field in asset:
        if field not in payload:
            if existing_asset:
                existing_field_value = existing_asset.get(field)
                if isinstance(existing_field_value, str):
                    split_vals = existing_field_value.split("|")
                    if len(split_vals) == 2:
                        split_vals[0] = str(asset.get(field, ""))
                        payload[field] = "|".join(split_vals)
                    elif len(split_vals) == 1:
                        payload[field] = str(asset.get(field, "")) + "|0"
                else:
                    payload[field] = asset.get(field)
            else:
                field_val = asset.get(field)
                payload[field] = str(field_val if field_val is not None else "") + "|0"
"""

import os
import sys
from unittest.mock import patch

import pytest

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    from app import apply_pipe_convention, to_stored_attributes


RESOLVED = {
    "gateway_address": "nuc-av-b1.curtin.edu.au",
    "gateway_port": 443,
    "gateway_username": "admin",
    "gateway_password": "ciphertext",
    "gateway_encrypted": True,
}


class TestApplyPipeConvention:
    def test_no_item_yet_gets_version_zero(self):
        assert apply_pipe_convention("nuc.example.com", False, None) == ("nuc.example.com|0", True)

    def test_existing_version_is_preserved(self):
        # This is the "do not overwrite the current flow" case: whatever
        # version pushAssetMetadata or a later writer put there survives.
        assert apply_pipe_convention("new.example.com", True, "old.example.com|7") == (
            "new.example.com|7",
            True,
        )

    def test_existing_unversioned_string_gains_version_zero(self):
        assert apply_pipe_convention("new.example.com", True, "old.example.com") == (
            "new.example.com|0",
            True,
        )

    def test_existing_non_string_is_written_raw(self):
        # pushAssetMetadata's else branch. An item that exists but has no
        # gateway attribute yet lands here, which is the common first-deploy
        # case for this lambda.
        assert apply_pipe_convention(443, True, None) == (443, True)
        assert apply_pipe_convention(True, True, False) == (True, True)

    def test_more_than_two_parts_is_left_alone(self):
        # pushAssetMetadata matches neither branch and writes nothing, so
        # neither does this.
        assert apply_pipe_convention("x", True, "a|b|c") == (None, False)

    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, "|0"),
            ("", "|0"),
            (0, "0|0"),
            (False, "False|0"),
            (443, "443|0"),
        ],
    )
    def test_stringification_matches_the_reference(self, value, expected):
        assert apply_pipe_convention(value, False, None) == (expected, True)


class TestToStoredAttributes:
    def test_new_item_versions_every_attribute(self):
        stored = to_stored_attributes(RESOLVED, False, {})
        assert stored == {
            "gateway_address": "nuc-av-b1.curtin.edu.au|0",
            "gateway_port": "443|0",
            "gateway_username": "admin|0",
            "gateway_password": "ciphertext|0",
            "gateway_encrypted": "True|0",
        }

    def test_existing_item_without_the_attributes_writes_raw(self):
        # The item exists because asset persistence just created it, but
        # Room Manager did not mirror the gateway fields onto it.
        assert to_stored_attributes(RESOLVED, True, {}) == RESOLVED

    def test_existing_versioned_values_keep_their_version(self):
        existing = {
            "gateway_address": "old.example.com|3",
            "gateway_port": "80|3",
            "gateway_username": "old|3",
            "gateway_password": "oldcipher|3",
            "gateway_encrypted": "False|3",
        }
        stored = to_stored_attributes(RESOLVED, True, existing)
        assert stored == {
            "gateway_address": "nuc-av-b1.curtin.edu.au|3",
            "gateway_port": "443|3",
            "gateway_username": "admin|3",
            "gateway_password": "ciphertext|3",
            "gateway_encrypted": "True|3",
        }

    def test_clearing_keeps_the_convention(self):
        cleared = {
            "gateway_address": "",
            "gateway_port": None,
            "gateway_username": "",
            "gateway_password": "",
            "gateway_encrypted": False,
        }
        existing = {"gateway_address": "old.example.com|2"}
        stored = to_stored_attributes(cleared, True, existing)
        assert stored["gateway_address"] == "|2"
        # Attributes with no prior string value stay raw.
        assert stored["gateway_port"] is None
        assert stored["gateway_encrypted"] is False

    def test_mixed_shapes_are_handled_per_attribute(self):
        existing = {
            "gateway_address": "old.example.com|5",
            "gateway_username": "old",
            "gateway_encrypted": False,
        }
        stored = to_stored_attributes(RESOLVED, True, existing)
        assert stored["gateway_address"] == "nuc-av-b1.curtin.edu.au|5"
        assert stored["gateway_username"] == "admin|0"
        assert stored["gateway_encrypted"] is True
        assert stored["gateway_port"] == 443
