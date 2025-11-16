import os

import pytest

from modules.utils.runtime_ssl import ensure_certifi_trust_store


def test_ensure_certifi_trust_store_sets_env(monkeypatch):
    ensure_certifi_trust_store.cache_clear()  # type: ignore[attr-defined]
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE"):
        monkeypatch.delenv(key, raising=False)

    cafile = ensure_certifi_trust_store()
    if cafile is None:
        pytest.skip("certifi not installed in this environment")

    assert os.environ["SSL_CERT_FILE"] == cafile
    assert os.environ["REQUESTS_CA_BUNDLE"] == cafile
    assert os.environ["AWS_CA_BUNDLE"] == cafile
