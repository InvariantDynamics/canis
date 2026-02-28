import builtins
import logging

import holmes.plugins.toolsets as toolsets_module


def test_load_python_toolsets_continues_when_prometheus_import_fails(
    monkeypatch, caplog
):
    original_import = builtins.__import__

    def import_with_prometheus_failure(
        name, globals=None, locals=None, fromlist=(), level=0
    ):
        if name == "holmes.plugins.toolsets.prometheus.prometheus":
            raise RuntimeError("simulated prometheus import failure")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(toolsets_module, "DISABLE_PROMETHEUS_TOOLSET", False)
    monkeypatch.setattr(builtins, "__import__", import_with_prometheus_failure)

    with caplog.at_level(logging.WARNING):
        toolsets = toolsets_module.load_python_toolsets(dal=None)

    assert toolsets
    assert "Failed to load Prometheus toolset" in caplog.text
