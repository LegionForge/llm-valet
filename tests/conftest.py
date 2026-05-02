# Re-exports shared fixtures from legionforge-dev-rig so pytest discovers them
# automatically — no imports needed in individual test files.
#
# To install dev-rig locally (path-editable until published to PyPI):
#   pip install -e "../../LegionForge-dev-rig/dev-rig"
#
# Once published:
#   pip install legionforge-dev-rig

# Settle time after model eviction before the next inference-dependent assertion.
# Ollama needs ~2s to fully release memory after keep_alive=0; tests that assert
# on post-eviction state must wait at least this long.
_EVICTION_SETTLE_S = 1.5

try:
    from legionforge_dev_rig.fixtures import mock_http_client, respx_mock_base_url

    __all__ = ["mock_http_client", "respx_mock_base_url"]
except ImportError:
    # dev-rig not installed — shared fixtures unavailable, unit tests still run
    pass
