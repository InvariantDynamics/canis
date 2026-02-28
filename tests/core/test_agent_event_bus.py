import logging

from holmes.core.agent_event_bus import AgentEventBus, AgentEventBusConfig


def test_publish_noop_when_disabled():
    bus = AgentEventBus(
        AgentEventBusConfig(
            enabled=False,
            url="nats://127.0.0.1:4222",
            subject_prefix="",
            token=None,
            user=None,
            password=None,
            connect_timeout_seconds=1,
        )
    )
    bus.publish("agent.kepler.plan.generated", {"plan_id": "plan_1"})


def test_publish_warns_once_when_enabled_without_nats(monkeypatch, caplog):
    monkeypatch.setattr("holmes.core.agent_event_bus.nats", None)
    bus = AgentEventBus(
        AgentEventBusConfig(
            enabled=True,
            url="nats://127.0.0.1:4222",
            subject_prefix="",
            token=None,
            user=None,
            password=None,
            connect_timeout_seconds=1,
        )
    )

    with caplog.at_level(logging.WARNING):
        bus.publish("agent.kepler.plan.generated", {"plan_id": "plan_1"})
        bus.publish("agent.kepler.plan.simulated", {"plan_id": "plan_1"})

    missing_client_messages = [
        record
        for record in caplog.records
        if "nats-py" in record.message and "not installed" in record.message
    ]
    assert len(missing_client_messages) == 1
