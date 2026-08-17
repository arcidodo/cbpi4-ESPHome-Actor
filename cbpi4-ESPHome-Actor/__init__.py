import asyncio
import logging
from cbpi.api import *
from aioesphomeapi import APIClient, APIConnectionError

logger = logging.getLogger("cbpi4-ESPHome-Actor")


@parameters([
    Property.Text(
        label="Host",
        configurable=True,
        description="IP-adres van de ESPHome node (bv. 192.168.1.50)"
    ),
    Property.Number(
        label="Port",
        configurable=True,
        default_value=6053,
        description="Native API poort van ESPHome (standaard 6053)"
    ),
    Property.Text(
        label="Encryption Key",
        configurable=True,
        description="API encryption key (base64) uit je ESPHome yaml. Leeg laten indien niet gebruikt."
    ),
    Property.Text(
        label="Entity Name",
        configurable=True,
        description="Naam (of id) van de switch-entiteit zoals gedefinieerd in de ESPHome yaml"
    ),
    Property.Number(
        label="Request Timeout",
        configurable=True,
        description="Reconnect-interval in seconden bij verbindingsfouten",
        default_value=5
    )
])
class ESPHomeActor(CBPiActor):

    def __init__(self, cbpi, id, props):
        """Guaranteed to run before any other lifecycle method — use for safe defaults."""
        super().__init__(cbpi, id, props)

        self.host = None
        self.port = 6053
        self.encryption_key = None
        self.entity_name = None
        self.entity_key = None

        self.timeout = 5

        self.state = False

        self.client = None
        self.connected = False

        self._connect_task = None

        logger.debug("[ESPHomeActor] __init__ completed (defaults set)")

    async def on_start(self):
        """Called when plugin is started/initialized by CBPi."""
        try:
            self.host = (self.props.get("Host") or "").strip()
            if not self.host:
                logger.error("[ESPHomeActor] Missing Host")
                return

            self.port = int(self.props.get("Port") or 6053)
            self.encryption_key = (self.props.get("Encryption Key") or "").strip() or None
            self.entity_name = (self.props.get("Entity Name") or "").strip()
            if not self.entity_name:
                logger.error("[ESPHomeActor] Missing Entity Name")
                return

            self.timeout = int(self.props.get("Request Timeout") or 5)

            self.client = APIClient(self.host, self.port, password="", noise_psk=self.encryption_key)

            logger.info(f"[ESPHomeActor] on_start: entity={self.entity_name}, host={self.host}:{self.port}")

            self._connect_task = asyncio.create_task(self.connect_loop())

        except Exception as e:
            logger.exception(f"[ESPHomeActor] Exception in on_start: {e}")

    async def connect_loop(self):
        """Maintain connection to the ESPHome node, resolve entity key, subscribe to state changes."""
        await asyncio.sleep(1)
        logger.debug("[ESPHomeActor] connect_loop started")

        while True:
            if not self.connected:
                try:
                    await self.client.connect(login=True)
                    entities, _ = await self.client.list_entities_services()

                    self.entity_key = None
                    for e in entities:
                        if e.name == self.entity_name or getattr(e, "object_id", None) == self.entity_name:
                            self.entity_key = e.key
                            break

                    if self.entity_key is None:
                        logger.error(f"[ESPHomeActor] Entity '{self.entity_name}' niet gevonden op {self.host}")
                        await self.client.disconnect()
                        await asyncio.sleep(self.timeout)
                        continue

                    self.client.subscribe_states(self._on_state)
                    self.connected = True
                    logger.info(f"[ESPHomeActor] Verbonden met {self.host}, entity key {self.entity_key}")

                except (APIConnectionError, OSError, TimeoutError) as e:
                    logger.error(f"[ESPHomeActor] Verbindingsfout ({self.host}): {e}")
                    self.connected = False
                    await asyncio.sleep(self.timeout)
                    continue

            await asyncio.sleep(1)

    def _on_state(self, state):
        """Reflect externally-changed state (e.g. physical button) back into CBPi."""
        if getattr(state, "key", None) != self.entity_key:
            return
        if not hasattr(state, "state"):
            return

        new_state = bool(state.state)
        if new_state != self.state:
            logger.info(f"[ESPHomeActor] Externe statuswijziging -> {new_state}")
            self.state = new_state
            try:
                asyncio.create_task(self.cbpi.actor.actor_update(self.id, 100 if new_state else 0))
            except Exception as e:
                logger.debug(f"[ESPHomeActor] actor_update failed: {e}")

    async def _set_switch(self, on: bool):
        """Send a switch command directly to the ESPHome node."""
        if not self.connected or self.entity_key is None:
            logger.warning("[ESPHomeActor] _set_switch called but not connected; skipping")
            return
        try:
            await self.client.switch_command(key=self.entity_key, state=on)
        except Exception as e:
            logger.exception(f"[ESPHomeActor] switch_command error: {e}")
            self.connected = False  # forceer reconnect

    async def on(self, power=None):
        """Requested to turn actor ON (via CBPi UI / script)."""
        self.state = True
        await self._set_switch(True)
        try:
            await self.cbpi.actor.actor_update(self.id, 100)
        except Exception:
            pass

    async def off(self):
        """Requested to turn actor OFF (via CBPi UI / script)."""
        self.state = False
        await self._set_switch(False)
        try:
            await self.cbpi.actor.actor_update(self.id, 0)
        except Exception:
            pass

    async def run(self):
        """Geen PWM meer — actor is puur aan/uit, gestuurd via on()/off()."""
        while True:
            await asyncio.sleep(1)

    def get_state(self):
        """Return boolean state for CBPi UI."""
        return bool(self.state)

    async def on_shutdown(self):
        """Cleanup: cancel connect task and close ESPHome connection."""
        logger.info("[ESPHomeActor] on_shutdown called — cleaning up")
        if self._connect_task:
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"[ESPHomeActor] connect_task cancellation error: {e}")

        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.debug(f"[ESPHomeActor] disconnect error: {e}")

        logger.info("[ESPHomeActor] cleanup done")


def setup(cbpi):
    cbpi.plugin.register("ESPHome Actor", ESPHomeActor)