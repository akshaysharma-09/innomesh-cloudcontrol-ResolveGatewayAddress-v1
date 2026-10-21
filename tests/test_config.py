import os
import sys
from unittest.mock import patch

with patch("boto3.client"), patch("boto3.resource"), patch("boto3.Session"):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

    import config


class TestGetKmsPublicKey:
    """
    Same parameter and same caching as pushAssetMetadata, so the key that
    encrypts a gateway password is the key that encrypts the device password
    on the very same asset item.
    """

    def setup_method(self):
        config._kms_public_key_cache.clear()

    def test_reads_the_tenant_parameter_decrypted(self):
        with patch.object(config, "ssm") as ssm, patch.object(config, "env", "dev"):
            ssm.get_parameter.return_value = {"Parameter": {"Value": "base64-der"}}

            assert config.get_kms_public_key("curtin") == "base64-der"

            kwargs = ssm.get_parameter.call_args.kwargs
            assert kwargs["Name"] == "/cap-innomesh-v1/curtin/aws/kms/dev/public-key"
            assert kwargs["WithDecryption"] is True

    def test_caches_per_tenant(self):
        with patch.object(config, "ssm") as ssm, patch.object(config, "env", "dev"):
            ssm.get_parameter.return_value = {"Parameter": {"Value": "base64-der"}}

            config.get_kms_public_key("curtin")
            config.get_kms_public_key("curtin")

            assert ssm.get_parameter.call_count == 1

    def test_missing_parameter_returns_none(self):
        class ParameterNotFound(Exception):
            pass

        with patch.object(config, "ssm") as ssm, patch.object(config, "env", "dev"):
            ssm.exceptions.ParameterNotFound = ParameterNotFound
            ssm.get_parameter.side_effect = ParameterNotFound()

            assert config.get_kms_public_key("curtin") is None


class TestExtractValue:
    def test_unwraps_the_ssm_response(self):
        assert config.extract_value({"Parameter": {"Value": "v"}}) == "v"


class TestCategory:
    def test_assets_table_prefix(self):
        # The table name is built as "assets" + "." + namespace, matching
        # every other writer of this table.
        assert config.Category.ASSETS == "assets"
        assert config.Category.ASSETS + "." + "curtin.uat.innomesh.com.au" == "assets.curtin.uat.innomesh.com.au"
