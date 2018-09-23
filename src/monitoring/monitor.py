'''
Created on 2017. aug. 28.

@author: gkovacs
'''

import logging

from os import environ
from threading import Thread, Event
from time import sleep
from eventlet.queue import Empty

from models import *
import monitoring.alert

from monitoring.constants import *
from monitoring.adapters.sensor import SensorAdapter
from monitoring.socket_io import send_system_state_change, send_sensors_state, \
    send_arm_state, send_alert_state, send_syren_state
from monitoring import storage


MEASUREMENT_CYCLES = 2
MEASUREMENT_TIME = 3
TOLERANCE = float(environ['TOLERANCE'])

# 2000.01.01 00:00:00
DEFAULT_DATETIME = 946684800


def isclose(a, b, tolerance=0.0):
    return abs(a - b) < tolerance


class Monitor(Thread):
    '''
    classdocs
    '''

    def __init__(self, actions):
        '''
        Constructor
        '''
        super(Monitor, self).__init__(name=THREAD_MONITOR)
        self._logger = logging.getLogger(LOG_MONITOR)
        self._sensorAdapter = SensorAdapter()
        self._actions = actions
        self._sensors = None
        self._alerts = {}
        self._db_alert = None
        self._stop_alert = Event()

        self._logger.info('Monitoring created')
        storage.set('state', MONITORING_STARTUP)
        storage.set('arm', ARM_DISARM)


    def run(self):
        self._logger.info('Monitoring started')

        # wait some seconds to build up socket IO connection before emit messages
        sleep(5)

        # remove invalid state items from db before startup
        self.cleanup_database()

        # initialize state
        send_alert_state(json.dumps(False))
        send_syren_state(None)
        send_arm_state(ARM_DISARM)

        self.load_sensors()

        while True:
            try:
                action = self._actions.get(True, 1 / int(environ['SAMPLE_RATE']))
                if action == MONITOR_STOP:
                    break
                elif action == MONITOR_ARM_AWAY:
                    storage.set('arm', ARM_AWAY)
                    send_arm_state(ARM_AWAY)
                    storage.set('state', MONITORING_ARMED)
                    send_system_state_change(MONITORING_ARMED)
                    self._stop_alert.clear()
                elif action == MONITOR_ARM_STAY:
                    storage.set('arm', ARM_STAY)
                    send_arm_state(ARM_STAY)
                    storage.set('state', MONITORING_ARMED)
                    send_system_state_change(MONITORING_ARMED)
                    self._stop_alert.clear()
                elif action == MONITOR_DISARM:
                    current_state = storage.get('state')
                    current_arm = storage.get('arm')
                    if current_state == MONITORING_ARMED and (current_arm == ARM_AWAY or current_arm == ARM_STAY):
                        storage.set('arm', ARM_DISARM)
                        send_arm_state(ARM_DISARM)
                        storage.set('state', MONITORING_READY)
                        send_system_state_change(MONITORING_READY)
                    self._stop_alert.set()
                    continue
                elif action == MONITOR_UPDATE_CONFIG:
                    self.load_sensors()
            except Empty:
                pass

            self.scan_sensors()
            self.handle_alerts()

        self._stop_alert.set()
        self._logger.info("Monitoring stopped")


    def validate_sensor_config(self):
        self._logger.debug("Validating config...")
        channels = set()
        for sensor in self._sensors:
            if sensor.channel in channels:
                self._logger.debug("Channels: %s", channels)
                return False
            else:
                channels.add(sensor.channel)

        self._logger.debug("Channels: %s", channels)
        return True


    def load_sensors(self):
        '''Load the sensors from the db in the thread to avoid session problems'''
        storage.set('state', MONITORING_UPDATING_CONFIG)
        send_sensors_state(None)
        send_system_state_change(MONITORING_UPDATING_CONFIG)

        # TODO: wait a little bit to see status for debug
        sleep(3)

        # !!! delete old sensors before load again
        self._sensors = []
        self._sensors = Sensor.query.filter_by(deleted=False).all()
        self._logger.debug("Sensors reloaded!")

        if len(self._sensors) > self._sensorAdapter.channel_count:
            self._logger.info("Invalid number of sensors to monitor (Found=%s > Max=%s)",
                              len(self._sensors), self._sensorAdapter.channel_count)
            self._sensors = []
            storage.set('state', MONITORING_INVALID_CONFIG)
            send_system_state_change(MONITORING_INVALID_CONFIG)
        elif not self.validate_sensor_config():
            self._logger.info("Invalid channel configuration")
            self._sensors = []
            storage.set('state', MONITORING_INVALID_CONFIG)
            send_system_state_change(MONITORING_INVALID_CONFIG)
        elif self.has_uninitialized_sensor():
            self._logger.info("Found sensor(s) without reference value")
            self.calibrate_sensors()
            storage.set('state', MONITORING_READY)
            send_system_state_change(MONITORING_READY)
        else:
            storage.set('state', MONITORING_READY)
            send_system_state_change(MONITORING_READY)

        send_sensors_state(False)


    def calibrate_sensors(self):
        self._logger.info("Initialize sensor references...")
        new_references = self.measure_sensor_references()
        if len(new_references) == self._sensorAdapter.channel_count:
            self._logger.info("New references: %s", new_references)
            self.save_sensor_references(new_references)
        else:
            self._logger.error("Error measure values! %s", self._references)


    def has_uninitialized_sensor(self):
        for sensor in self._sensors:
            if sensor.reference_value is None:
                return True

        return False


    def cleanup_database(self):
        changed = False
        for sensor in Sensor.query.all():
            if sensor.alert:
                sensor.alert = False
                changed = True
                self._logger.debug('Cleared sensor')

        for alert in Alert.query.filter_by(end_time=None).all():
            alert.end_time = datetime.datetime.fromtimestamp(DEFAULT_DATETIME)
            self._logger.debug('Cleared alert')
            changed = True

        if changed:
            self._logger.debug('Cleared db')
            db.session.commit()
        else:
            self._logger.debug('Cleared nothing')


    def save_sensor_references(self, references):
        for sensor in self._sensors:
            sensor.reference_value = references[sensor.channel]
            db.session.commit()


    def measure_sensor_references(self):
        measurements = []
        for cycle in range(MEASUREMENT_CYCLES):
            measurements.append(self._sensorAdapter.get_values())
            sleep(MEASUREMENT_TIME)

        self._logger.debug("Measured values: %s", measurements)

        references = {}
        for channel in range(self._sensorAdapter.channel_count):
            value_sum = 0
            for cycle in range(MEASUREMENT_CYCLES):
                value_sum += measurements[cycle][channel]
            references[channel] = value_sum / MEASUREMENT_CYCLES

        return list(references.values())


    def scan_sensors(self):
        changes = False
        found_alert = False
        for sensor in self._sensors:
            value = self._sensorAdapter.get_value(sensor.channel)
            #self._logger.debug("Sensor({}): R:{} -> V:{}".format(sensor.channel, sensor.reference_value, value))
            if not isclose(value, sensor.reference_value, TOLERANCE):
                if not sensor.alert:
                    self._logger.debug('Alert on channel: %s, (changed %s -> %s)', sensor.channel, sensor.reference_value, value)
                    sensor.alert = True
                    changes = True
            else:
                if sensor.alert:
                    self._logger.debug('Cleared alert on channel: %s', sensor.channel)
                    sensor.alert = False
                    changes = True

            if sensor.alert:
                found_alert = True

        if changes:
            db.session.commit()
            send_sensors_state(found_alert)


    def handle_alerts(self):
        # check for alerting sensors if armed

        # save current state to avoid concurrency
        current_state = storage.get('state')
        current_arm = storage.get('arm')

        changes = False
        for sensor in self._sensors:
            if sensor.alert and sensor.id not in self._alerts and sensor.enabled:
                if not sensor.zone.disarmed_delay is None and current_state == MONITORING_READY or \
                    not sensor.zone.disarmed_delay is None and current_state == MONITORING_SABOTAGE or \
                    not sensor.zone.away_delay is None and current_arm == ARM_AWAY or \
                    not sensor.zone.stay_delay is None and current_arm == ARM_STAY:
                    self._alerts[sensor.id] = {'alert': monitoring.alert.SensorAlert(sensor.id, current_arm, self._stop_alert)}
                    self._alerts[sensor.id]['alert'].start()
                    changes = True
                    self._stop_alert.clear()
            elif not sensor.alert and sensor.id in self._alerts:
                if self._alerts[sensor.id]['alert']._arm_type == ARM_DISARM:
                    # stop sabotage
                    storage.set('state', MONITORING_READY)
                    send_system_state_change(MONITORING_READY)
                del self._alerts[sensor.id]

        if changes:
            self._logger.debug("Save sensor changes")
            db.session.commit()
