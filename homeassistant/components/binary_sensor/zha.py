"""
Binary sensors on Zigbee Home Automation networks.

For more details on this platform, please refer to the documentation
at https://home-assistant.io/components/binary_sensor.zha/
"""
import logging

from homeassistant.components.binary_sensor import DOMAIN, BinarySensorDevice
from homeassistant.components.zha import helpers
from homeassistant.components.zha.const import (
    DATA_ZHA, DATA_ZHA_DISPATCHERS, REPORT_CONFIG_IMMEDIATE, ZHA_DISCOVERY_NEW)
from homeassistant.components.zha.entities import ZhaEntity
from homeassistant.const import STATE_ON
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ['zha']

# Zigbee Cluster Library Zone Type to Home Assistant device class
CLASS_MAPPING = {
    0x000d: 'motion',
    0x0015: 'opening',
    0x0028: 'smoke',
    0x002a: 'moisture',
    0x002b: 'gas',
    0x002d: 'vibration',
}


async def async_setup_platform(hass, config, async_add_entities,
                               discovery_info=None):
    """Old way of setting up Zigbee Home Automation binary sensors."""
    pass


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the Zigbee Home Automation binary sensor from config entry."""
    async def async_discover(discovery_info):
        await _async_setup_entities(hass, config_entry, async_add_entities,
                                    [discovery_info])

    unsub = async_dispatcher_connect(
        hass, ZHA_DISCOVERY_NEW.format(DOMAIN), async_discover)
    hass.data[DATA_ZHA][DATA_ZHA_DISPATCHERS].append(unsub)

    binary_sensors = hass.data.get(DATA_ZHA, {}).get(DOMAIN)
    if binary_sensors is not None:
        await _async_setup_entities(hass, config_entry, async_add_entities,
                                    binary_sensors.values())
        del hass.data[DATA_ZHA][DOMAIN]


async def _async_setup_entities(hass, config_entry, async_add_entities,
                                discovery_infos):
    """Set up the ZHA binary sensors."""
    entities = []
    for discovery_info in discovery_infos:
        from zigpy.zcl.clusters.general import OnOff
        from zigpy.zcl.clusters.security import IasZone
        if IasZone.cluster_id in discovery_info['in_clusters']:
            entities.append(await _async_setup_iaszone(discovery_info))
        elif OnOff.cluster_id in discovery_info['out_clusters']:
            entities.append(await _async_setup_remote(discovery_info))

    async_add_entities(entities, update_before_add=True)


async def _async_setup_iaszone(discovery_info):
    device_class = None
    from zigpy.zcl.clusters.security import IasZone
    cluster = discovery_info['in_clusters'][IasZone.cluster_id]
    if discovery_info['new_join']:
        await cluster.bind()
        ieee = cluster.endpoint.device.application.ieee
        await cluster.write_attributes({'cie_addr': ieee})

    try:
        zone_type = await cluster['zone_type']
        device_class = CLASS_MAPPING.get(zone_type, None)
    except Exception:  # pylint: disable=broad-except
        # If we fail to read from the device, use a non-specific class
        pass

    return BinarySensor(device_class, **discovery_info)


async def _async_setup_remote(discovery_info):
    remote = Remote(**discovery_info)

    if discovery_info['new_join']:
        await remote.async_configure()
    return remote


class BinarySensor(RestoreEntity, ZhaEntity, BinarySensorDevice):
    """The ZHA Binary Sensor."""

    _domain = DOMAIN

    def __init__(self, device_class, **kwargs):
        """Initialize the ZHA binary sensor."""
        super().__init__(**kwargs)
        self._device_class = device_class
        from zigpy.zcl.clusters.security import IasZone
        self._ias_zone_cluster = self._in_clusters[IasZone.cluster_id]

    @property
    def should_poll(self) -> bool:
        """Let zha handle polling."""
        return False

    @property
    def is_on(self) -> bool:
        """Return True if entity is on."""
        if self._state is None:
            return False
        return bool(self._state)

    @property
    def device_class(self):
        """Return the class of this device, from component DEVICE_CLASSES."""
        return self._device_class

    def cluster_command(self, tsn, command_id, args):
        """Handle commands received to this cluster."""
        if command_id == 0:
            self._state = args[0] & 3
            _LOGGER.debug("Updated alarm state: %s", self._state)
            self.async_schedule_update_ha_state()
        elif command_id == 1:
            _LOGGER.debug("Enroll requested")
            res = self._ias_zone_cluster.enroll_response(0, 0)
            self.hass.async_add_job(res)

    async def async_added_to_hass(self):
        """Run when about to be added to hass."""
        await super().async_added_to_hass()
        old_state = await self.async_get_last_state()
        if self._state is not None or old_state is None:
            return

        _LOGGER.debug("%s restoring old state: %s", self.entity_id, old_state)
        if old_state.state == STATE_ON:
            self._state = 3
        else:
            self._state = 0

    async def async_update(self):
        """Retrieve latest state."""
        from zigpy.types.basic import uint16_t

        result = await helpers.safe_read(self._endpoint.ias_zone,
                                         ['zone_status'],
                                         allow_cache=False,
                                         only_cache=(not self._initialized))
        state = result.get('zone_status', self._state)
        if isinstance(state, (int, uint16_t)):
            self._state = result.get('zone_status', self._state) & 3


class Remote(RestoreEntity, ZhaEntity, BinarySensorDevice):
    """ZHA switch/remote controller/button."""

    _domain = DOMAIN

    class OnOffListener:
        """Listener for the OnOff Zigbee cluster."""

        def __init__(self, entity):
            """Initialize OnOffListener."""
            self._entity = entity

        def cluster_command(self, tsn, command_id, args):
            """Handle commands received to this cluster."""
            if command_id in (0x0000, 0x0040):
                self._entity.set_state(False)
            elif command_id in (0x0001, 0x0041, 0x0042):
                self._entity.set_state(True)
            elif command_id == 0x0002:
                self._entity.set_state(not self._entity.is_on)

        def attribute_updated(self, attrid, value):
            """Handle attribute updates on this cluster."""
            if attrid == 0:
                self._entity.set_state(value)

        def zdo_command(self, *args, **kwargs):
            """Handle ZDO commands on this cluster."""
            pass

        def zha_send_event(self, cluster, command, args):
            """Relay entity events to hass."""
            pass  # don't let entities fire events

    class LevelListener:
        """Listener for the LevelControl Zigbee cluster."""

        def __init__(self, entity):
            """Initialize LevelListener."""
            self._entity = entity

        def cluster_command(self, tsn, command_id, args):
            """Handle commands received to this cluster."""
            if command_id in (0x0000, 0x0004):  # move_to_level, -with_on_off
                self._entity.set_level(args[0])
            elif command_id in (0x0001, 0x0005):  # move, -with_on_off
                # We should dim slowly -- for now, just step once
                rate = args[1]
                if args[0] == 0xff:
                    rate = 10  # Should read default move rate
                self._entity.move_level(-rate if args[0] else rate)
            elif command_id in (0x0002, 0x0006):  # step, -with_on_off
                # Step (technically may change on/off)
                self._entity.move_level(-args[1] if args[0] else args[1])

        def attribute_update(self, attrid, value):
            """Handle attribute updates on this cluster."""
            if attrid == 0:
                self._entity.set_level(value)

        def zdo_command(self, *args, **kwargs):
            """Handle ZDO commands on this cluster."""
            pass

        def zha_send_event(self, cluster, command, args):
            """Relay entity events to hass."""
            pass  # don't let entities fire events

    def __init__(self, **kwargs):
        """Initialize Switch."""
        super().__init__(**kwargs)
        self._level = 0
        from zigpy.zcl.clusters import general
        self._out_listeners = {
            general.OnOff.cluster_id: self.OnOffListener(self),
            general.LevelControl.cluster_id: self.LevelListener(self),
        }
        out_clusters = kwargs.get('out_clusters')
        self._zcl_reporting = {}
        for cluster_id in [general.OnOff.cluster_id,
                           general.LevelControl.cluster_id]:
            if cluster_id not in out_clusters:
                continue
            cluster = out_clusters[cluster_id]
            self._zcl_reporting[cluster] = {0: REPORT_CONFIG_IMMEDIATE}

    @property
    def should_poll(self) -> bool:
        """Let zha handle polling."""
        return False

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return the device state attributes."""
        self._device_state_attributes.update({
            'level': self._state and self._level or 0
        })
        return self._device_state_attributes

    @property
    def zcl_reporting_config(self):
        """Return ZCL attribute reporting configuration."""
        return self._zcl_reporting

    def move_level(self, change):
        """Increment the level, setting state if appropriate."""
        if not self._state and change > 0:
            self._level = 0
        self._level = min(255, max(0, self._level + change))
        self._state = bool(self._level)
        self.async_schedule_update_ha_state()

    def set_level(self, level):
        """Set the level, setting state if appropriate."""
        self._level = level
        self._state = bool(self._level)
        self.async_schedule_update_ha_state()

    def set_state(self, state):
        """Set the state."""
        self._state = state
        if self._level == 0:
            self._level = 255
        self.async_schedule_update_ha_state()

    async def async_added_to_hass(self):
        """Run when about to be added to hass."""
        await super().async_added_to_hass()
        old_state = await self.async_get_last_state()
        if self._state is not None or old_state is None:
            return

        _LOGGER.debug("%s restoring old state: %s", self.entity_id, old_state)
        if 'level' in old_state.attributes:
            self._level = old_state.attributes['level']
        self._state = old_state.state == STATE_ON

    async def async_update(self):
        """Retrieve latest state."""
        from zigpy.zcl.clusters.general import OnOff
        result = await helpers.safe_read(
            self._endpoint.out_clusters[OnOff.cluster_id],
            ['on_off'],
            allow_cache=False,
            only_cache=(not self._initialized)
        )
        self._state = result.get('on_off', self._state)
