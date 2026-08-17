import asyncio
import logging
from cbpi.api import *
from aioesphomeapi import APIClient, APIConnectionError

logger = logging.getLogger("cbpi4-ESPHome-Actor")

# Dictionairy waarin we actieve verbindingen per ESP bewaren: { "192.168.1.50:6053": ESPHomeManager }
_MANAGERS = {}


class ESPHomeManager:
    """Beheert ÉÉN enkele APIClient verbinding per ESPHome node voor meerdere actors."""

    def __init__(self, host, port, encryption_key, timeout=5):
        self.host = host
        self.port = port
        self.encryption_key = encryption_key
        self.timeout = timeout

        self.client = APIClient(
            self.host, self.port, password="", noise_psk=self.encryption_key
        )
        self.connected = False
        self.connect_task = None
        self.actors = {}  # { actor_id: ESPHomeActor }
        self.entities = []
        self.running = True

    async def start(self):
        if not self.connect_task:
            self.connect_task = asyncio.create_task(self._connect_loop())

    def register_actor(self, actor):
        self.actors[actor.id] = actor
        if self.connected and self.entities:
            self._resolve_actor_key(actor)

    def unregister_actor(self, actor):
        if actor.id in self.actors:
            del self.actors[actor.id]

    def _resolve_actor_key(self, actor):
        actor.entity_key = None
        for e in self.entities:
            if e.name == actor.entity_name or getattr(e, "object_id", None) == actor.entity_name:
                actor.entity_key = e.key
                logger.info(f"[ESPHomeManager] Entity '{actor.entity_name}' gekoppeld aan key {e.key} ({self.host})")
                break

        if actor.entity_key is None:
            logger.error(f"[ESPHomeManager] Entity '{actor.entity_name}' niet gevonden op {self.host}")

    async def _connect_loop(self):
        logger.debug(f"[ESPHomeManager] Verbinder-loop gestart voor {self.host}")

        while self.running:
            if not self.connected:
                try:
                    await self.client.connect(login=True)
                    self.entities, _ = await self.client.list_entities_services()

                    # Koppel entity keys voor alle geregistreerde actors op deze ESP
                    for actor in list(self.actors.values()):
                        self._resolve_actor_key(actor)

                    self.client.subscribe_states(self._on_state)
                    self.connected = True
                    logger.info(f"[ESPHomeManager] Verbonden met {self.host}")

                except (APIConnectionError, OSError, TimeoutError) as e:
                    if "Already connected" in str(e):
                        self.connected = True
                        await asyncio.sleep(1)
                        continue

                    logger.error(f"[ESPHomeManager] Verbindingsfout ({self.host}): {e}")
                    self.connected = False
                    await asyncio.sleep(self.timeout)
                    continue

            await asyncio.sleep(1)

    def _on_state(self, state):
        key = getattr(state, "key", None)
        if key is None or not hasattr(state, "state"):
            return

        # Stuur statusupdate door naar de juiste actor
        for actor in list(self.actors.values()):
            if actor.entity_key == key:
                actor.handle_external_state(bool(state.state))

    def set_switch(self, entity_key, state: bool):
        if not self.connected or entity_key is None:
            logger.warning(f"[ESPHomeManager] Kan commando niet sturen naar {self.host}: niet verbonden")
            return
        try:
            self.client.switch_command(key=entity_key, state=state)
            logger.info(f"[ESPHomeManager] Commando verzonden: key={entity_key}, state={state}")
        except Exception as e:
            logger.exception(f"[ESPHomeManager] switch_command error op {self.host}: {e}")
            self.connected = False

    async def stop(self):
        self.running = False
        if self.connect_task:
            self.connect_task.cancel()
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass


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
        super().__init__(cbpi, id, props)

        self.host = None
        self.port = 6053
        self.encryption_key = None
        self.entity_name = None
        self.entity_key = None

        self.state = False
        self.manager = None

    async def on_start(self):
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

            timeout = int(self.props.get("Request Timeout") or 5)

            # Haal de gedeelde manager op of maak een nieuwe aan per Host:Port
            manager_key = f"{self.host}:{self.port}"
            if manager_key not in _MANAGERS:
                _MANAGERS[manager_key] = ESPHomeManager(self.host, self.port, self.encryption_key, timeout)
                await _MANAGERS[manager_key].start()

            self.manager = _MANAGERS[manager_key]
            self.manager.register_actor(self)

            logger.info(f"[ESPHomeActor] Aangemeld bij manager {manager_key} voor entity '{self.entity_name}'")

        except Exception as e:
            logger.exception(f"[ESPHomeActor] Exception in on_start: {e}")

    def handle_external_state(self, new_state: bool):
        """Wordt aangeroepen door de Manager bij een statuswijziging vanuit ESPHome."""
        if new_state != self.state:
            logger.info(f"[ESPHomeActor] Externe statuswijziging ({self.entity_name}) -> {new_state}")
            self.state = new_state
            try:
                asyncio.create_task(self.cbpi.actor.actor_update(self.id, 100 if new_state else 0))
            except Exception as e:
                logger.debug(f"[ESPHomeActor] actor_update failed: {e}")

    async def on(self, power=None, *args, **kwargs):
        self.state = True
        if self.manager and self.entity_key is not None:
            self.manager.set_switch(self.entity_key, True)
        try:
            await self.cbpi.actor.actor_update(self.id, 100)
        except Exception:
            pass

    async def off(self, *args, **kwargs):
        self.state = False
        if self.manager and self.entity_key is not None:
            self.manager.set_switch(self.entity_key, False)
        try:
            await self.cbpi.actor.actor_update(self.id, 0)
        except Exception:
            pass

    async def run(self):
        while True:
            await asyncio.sleep(1)

    def get_state(self):
        return bool(self.state)

    async def on_shutdown(self):
        logger.info(f"[ESPHomeActor] Afmelden van actor {self.entity_name}")
        if self.manager:
            self.manager.unregister_actor(self)
            # Als er geen actors meer gekoppeld zijn aan deze manager, sluiten we de verbinding netjes af
            if not self.manager.actors:
                manager_key = f"{self.host}:{self.port}"
                logger.info(f"[ESPHomeActor] Geen actieve actors meer voor {manager_key}, verbinding wordt gesloten.")
                await self.manager.stop()
                _MANAGERS.pop(manager_key, None)


def setup(cbpi):
    cbpi.plugin.register("ESPHome Actor", ESPHomeActor)