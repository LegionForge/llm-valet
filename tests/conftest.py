# Re-exports shared fixtures from legionforge-dev-rig so pytest discovers them
# automatically — no imports needed in individual test files.
#
# To install dev-rig locally (path-editable until published to PyPI):
#   pip install -e "../../LegionForge-dev-rig/dev-rig"
#
# Once published:
#   pip install legionforge-dev-rig

try:
    from legionforge_dev_rig.fixtures import mock_http_client, respx_mock_base_url
    __all__ = ["mock_http_client", "respx_mock_base_url"]
except ImportError:
    # dev-rig not installed — shared fixtures unavailable, unit tests still run
    pass
