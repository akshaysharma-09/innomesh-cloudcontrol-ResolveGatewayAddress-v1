import os

import boto3

ssm = boto3.client("ssm")

env = os.environ.get("Environment")

_kms_public_key_cache = {}


def get_kms_public_key(tenant):
    """
    Tenant RSA public key used to encrypt device credentials at rest.

    Same parameter, same cache-per-tenant behaviour as
    innomesh-cloudcontrol-pushAssetMetadata-v1, so a gateway password this
    lambda encrypts is byte-compatible with the device password that lambda
    encrypts, and the Sight worker decrypts both with one routine.
    """
    if tenant in _kms_public_key_cache:
        return _kms_public_key_cache[tenant]
    try:
        value = extract_value(
            ssm.get_parameter(Name=f"/cap-innomesh-v1/{tenant}/aws/kms/{env}/public-key", WithDecryption=True)
        )
        _kms_public_key_cache[tenant] = value
        return value
    except (KeyError, ssm.exceptions.ParameterNotFound):
        return None


def extract_value(param_info):
    return param_info.get("Parameter").get("Value")


class Category:
    ASSETS = "assets"
