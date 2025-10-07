"""Utilities for sending state updates to Yandex Smart Home callback API."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import requests


class YandexCallbackError(RuntimeError):
    """Base exception for callback related errors."""


class BadCallbackUrlError(YandexCallbackError):
    """Raised when callback URL template cannot be formatted."""


@dataclass
class YandexCallbackClient:
    """Minimal client for the Yandex Smart Home callback endpoint.

    The implementation mirrors the reference snippet that toggles the
    ``on`` capability and rotates through predefined program names when
    reporting the state of a device.  It is intentionally lightweight so it
    can be reused both from ad-hoc scripts and from the Flask application.
    """

    skill_id: str
    token: str
    callback_url_template: str = "https://dialogs.yandex.net/api/v1/skills/{skill_id}/callback/state"
    timeout: float = 5.0

    def build_url(self) -> str:
        """Return the fully formatted callback URL.

        Raises
        ------
        BadCallbackUrlError
            If the template contains placeholders other than ``skill_id``.
        """

        try:
            return (self.callback_url_template or "").format(skill_id=self.skill_id)
        except KeyError as exc:  # pragma: no cover - defensive branch
            raise BadCallbackUrlError(f"bad_callback_url:{exc}") from exc

    @property
    def is_configured(self) -> bool:
        """Whether the client has enough data to perform requests."""

        return bool(self.skill_id and self.token and self.callback_url_template)

    def build_device_payload(
        self,
        device_id: str,
        on: bool,
        program_value: str,
        *,
        properties: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Construct the ``devices`` entry for the callback request."""

        payload: Dict[str, Any] = {
            "id": device_id,
            "capabilities": [
                {
                    "type": "devices.capabilities.on_off",
                    "state": {"instance": "on", "value": bool(on)},
                },
                {
                    "type": "devices.capabilities.mode",
                    "state": {"instance": "program", "value": program_value},
                },
            ],
            "properties": list(properties or ()),
        }
        return payload

    def build_callback_body(
        self,
        user_id: str,
        device_payload: Dict[str, Any],
        *,
        timestamp: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Compose the full callback request body."""

        ts = int(timestamp if timestamp is not None else time.time())
        return {
            "ts": ts,
            "payload": {
                "user_id": str(user_id),
                "devices": [device_payload],
            },
        }

    def send_device_state(
        self,
        user_id: str,
        device_payload: Dict[str, Any],
        *,
        timestamp: Optional[int] = None,
        body: Optional[Dict[str, Any]] = None,
        url: Optional[str] = None,
    ) -> requests.Response:
        """Send the prepared payload to the callback API.

        Parameters
        ----------
        user_id:
            Identifier that should match the ``user_id`` known to Yandex.
        device_payload:
            Structure describing the device state, compatible with the
            ``devices`` entry from the Smart Home callback contract.
        timestamp:
            Optional UNIX timestamp.  If omitted, ``time.time()`` is used.
        body:
            Optional pre-built callback body.  When provided it is used as-is
            allowing the caller to reuse the same serialized payload for
            logging purposes.
        """

        if not self.is_configured:
            raise YandexCallbackError("callback_client_not_configured")

        target_url = url or self.build_url()
        effective_body = body or self.build_callback_body(
            user_id,
            device_payload,
            timestamp=timestamp,
        )
        headers = {
            "Authorization": f"OAuth {self.token}",
            "Content-Type": "application/json",
        }

        serialized = json.dumps(effective_body, ensure_ascii=False)
        response = requests.post(
            target_url,
            headers=headers,
            data=serialized,
            timeout=self.timeout,
        )
        return response


__all__ = [
    "BadCallbackUrlError",
    "YandexCallbackClient",
    "YandexCallbackError",
]
