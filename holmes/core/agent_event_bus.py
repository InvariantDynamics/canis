import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import nats
except ImportError:  # pragma: no cover - optional dependency
    nats = None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentEventBusConfig:
    enabled: bool
    url: str
    subject_prefix: str
    token: Optional[str]
    user: Optional[str]
    password: Optional[str]
    connect_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "AgentEventBusConfig":
        return cls(
            enabled=_env_flag("KEPLER_NATS_ENABLED", False),
            url=os.getenv("KEPLER_NATS_URL", "nats://127.0.0.1:4222"),
            subject_prefix=os.getenv("KEPLER_NATS_SUBJECT_PREFIX", "").strip("."),
            token=os.getenv("KEPLER_NATS_TOKEN"),
            user=os.getenv("KEPLER_NATS_USER"),
            password=os.getenv("KEPLER_NATS_PASSWORD"),
            connect_timeout_seconds=int(
                os.getenv("KEPLER_NATS_CONNECT_TIMEOUT_SECONDS", "3")
            ),
        )


class AgentEventBus:
    def __init__(self, config: AgentEventBusConfig):
        self._config = config
        self._warned_missing_client = False

    @classmethod
    def from_env(cls) -> "AgentEventBus":
        return cls(AgentEventBusConfig.from_env())

    def publish(self, event_name: str, payload: Dict[str, Any]) -> None:
        if not self._config.enabled:
            return

        if nats is None:
            if not self._warned_missing_client:
                logging.warning(
                    "KEPLER_NATS_ENABLED=true but python package 'nats-py' is not installed; skipping NATS publish"
                )
                self._warned_missing_client = True
            return

        subject = (
            f"{self._config.subject_prefix}.{event_name}"
            if self._config.subject_prefix
            else event_name
        )
        message = json.dumps(payload, default=str).encode("utf-8")

        try:
            asyncio.run(self._publish_once(subject, message))
        except RuntimeError:
            logging.warning(
                "Unable to publish NATS event for %s due to active asyncio loop",
                subject,
            )
        except Exception:
            logging.error("Failed publishing NATS event for %s", subject, exc_info=True)

    async def _publish_once(self, subject: str, payload: bytes) -> None:
        connect_kwargs: Dict[str, Any] = {
            "servers": [self._config.url],
            "connect_timeout": self._config.connect_timeout_seconds,
        }
        if self._config.token:
            connect_kwargs["token"] = self._config.token
        if self._config.user:
            connect_kwargs["user"] = self._config.user
        if self._config.password:
            connect_kwargs["password"] = self._config.password

        nc = await nats.connect(**connect_kwargs)
        try:
            await nc.publish(subject, payload)
            await nc.flush()
        finally:
            await nc.drain()
