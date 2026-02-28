from unittest.mock import patch

from holmes.config import Config


@patch("holmes.core.llm.ROBUSTA_AI", False)
@patch("holmes.config.Config._Config__get_cluster_name", return_value="test-cluster")
def test_custom_runbook_catalogs_from_env_csv(mock_cluster, monkeypatch):
    monkeypatch.setenv(
        "CUSTOM_RUNBOOK_CATALOGS",
        "/etc/holmes/runbooks/catalog-a.json, /etc/holmes/runbooks/catalog-b.json",
    )
    config = Config.load_from_env()
    assert config.custom_runbook_catalogs == [
        "/etc/holmes/runbooks/catalog-a.json",
        "/etc/holmes/runbooks/catalog-b.json",
    ]


@patch("holmes.core.llm.ROBUSTA_AI", False)
@patch("holmes.config.Config._Config__get_cluster_name", return_value="test-cluster")
def test_custom_runbook_catalogs_from_env_json(mock_cluster, monkeypatch):
    monkeypatch.setenv(
        "CUSTOM_RUNBOOK_CATALOGS",
        '["/etc/holmes/runbooks/catalog-a.json","/etc/holmes/runbooks/catalog-b.json"]',
    )
    config = Config.load_from_env()
    assert config.custom_runbook_catalogs == [
        "/etc/holmes/runbooks/catalog-a.json",
        "/etc/holmes/runbooks/catalog-b.json",
    ]
