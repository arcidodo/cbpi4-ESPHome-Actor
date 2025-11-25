import logging
import asyncio
from cbpi.api import *

import aioesphomeapi

logger = logging.getLogger("cbpi4-esphome-actor")

@parameters([
    Property.Text(label="ESPHome Host", configurable=True, description="IP or hostname of the ESPHome device"),
    Property.Number(label="ESPHome Port", configurable=True, description="Usually 6053", default_value=6053),
    Property.Text(label="API Encryption Key", configurable=True, description="Leave empty if no encryption key is used"),
    Property.Text(label="Switch ID", configurable=True, description="The ESPHome switch ID to control")
])
class ESPHomeSwitchActor(CBPiActor):

    def __init__(self, cbpi, id, props):
        super().__init__(cbpi, id, props)

        # Defaults to avoid race conditions
        self.api = None
        self.client = None

        self.state = False
        self.power = 0
        
        self.host = None
        self.port = None
        self.key = None
        self.switch_id = None

        self.connected = False
        self.running = True

    async def on_start(self):

        self.host = self.props.get("ESPHome Host")
        self.port = int(self.props.get("ESPHome Port", 6053))
        self.key = self.props.get("API Encryption Key", "")
        self.switch_id = self.props.get("Switch ID")

        # ESPHome client
        self.client = aioesphomeapi.APIClient(
            host=self.host,
            port=self.port,
            password="",            # Password-based API is deprecated
            noise_psk=self.key or None
        )

        asyncio.create_task(self.connection_manager())
        logger.info("[ESPHomeActor] Initialization completed.")

    async def connection_manager(self):
        """
        Maintains a persistent connection with ESPHome.
        Auto-reconnect if device or wifi resets.
        """
        while self.running:
            if not self.connected:
                try:
                    logger.info(f"[ESPHomeActor] Connecting to ESPHome at {self.host}:{self.port}")
                    await self.client.connect(login=True)
                    self.connected = True
                    logger.info("[ESPHomeActor] Connected to ESPHome")

                    # Get entity list
                    entities, _ = await self.client.list_entities_services()
                    self.switch = None

                    for ent in entities:
                        if isinstance(ent, aioesphomeapi.SwitchInfo) and ent.object_id == self.switch_id:
                            self.switch = ent
                            logger.info(f"[ESPHomeActor] Found switch: {ent.object_id}")
                            break

                    if self.switch is None:
                        logger.error(f"[ESPHomeActor] ERROR: Switch ID '{self.switch_id}' not found on the ESPHome device")

                except Exception as e:
                    logger.warning(f"[ESPHomeActor] ESPHome connect failed: {e}")
                    self.connected = False
                    await asyncio.sleep(5)
            else:
                await asyncio.sleep(1)

    async def send_switch(self, state: bool):
        """
        Sends a switch command to ESPHome if connected.
        """
        if not self.connected or self.switch is None:
            logger.warning("[ESPHomeActor] Cannot send command: not connected or switch missing.")
            return

        try:
            await self.client.switch_command(key=self.switch.key, state=state)
            self.state = state
            logger.info(f"[ESPHomeActor] Switch '{self.switch_id}' set to {state}")
        except Exception as e:
            logger.error(f"[ESPHomeActor] Error sending switch command: {e}")
            self.connected = False

    async def on(self, power=None):
        await self.send_switch(True)

    async def off(self):
        await self.send_switch(False)

    def get_state(self):
        return self.state

    @action("Set Power", parameters=[Property.Number("Power", description="0 or 100", default_value=100)])
    async def setpower(self, Power=100, **kwargs):
        if int(Power) > 0:
            await self.on()
        else:
            await self.off()

    async def on_stop(self):
        self.running = False
        try:
            if self.client is not None:
                await self.client.disconnect()
        except:
            pass

def setup(cbpi):
    cbpi.plugin.register("ESPHome Switch Actor", ESPHomeSwitchActor)
