import logging
import os
from queue import Empty
from threading import Thread
from time import time

from monitoring.adapters.keypads.base import KeypadBase
from monitoring.adapters.mock.keypad import MockKeypad
from monitoring.constants import (LOG_ADKEYPAD, MONITOR_ARM_AWAY,
                                  MONITOR_ARM_STAY, MONITOR_DISARM,
                                  MONITOR_STOP, MONITOR_UPDATE_KEYPAD,
                                  THREAD_KEYPAD)

if os.uname()[4][:3] == "arm":
    from monitoring.adapters.keypads.dsc import DSCKeypad

COMMUNICATION_PERIOD = 0.5  # sec


class Keypad(Thread):
    # pins
    CLOCK_PIN = 5
    DATA_PIN = 0

    def __init__(self, commands, responses):
        super(Keypad, self).__init__(name=THREAD_KEYPAD)
        self._logger = logging.getLogger(LOG_ADKEYPAD)
        self._commands = commands
        self._responses = responses
        self._codes = []
        self._keypad: KeypadBase = None

    def set_type(self, type):
        # check if running on Raspberry
        if os.uname()[4][:3] != "arm":
            self._keypad = MockKeypad(Keypad.CLOCK_PIN, Keypad.DATA_PIN)
        elif type == "dsc":
            with self._commands.mutex:
                self._commands.queue.clear()
            self._keypad = DSCKeypad(Keypad.CLOCK_PIN, Keypad.DATA_PIN)
        elif type is None:
            self._logger.debug("Keypad removed")
            self._keypad = None
        else:
            self._logger.error("Unknown keypad type: %s", type)
        self._logger.debug("Keypad created type: %s", type)

    def run(self):
        # load from db
        self.set_type("dsc")
        self._codes = ["1234", "1111"]

        try:
            self.communicate()
        except KeyboardInterrupt:
            self._logger.error("Keyboard interrupt")
            pass
        except Exception:
            self._logger.exception("Keypad communication failed!")

        self._logger.info("Keypad manager stopped")

    def communicate(self):
        self._keypad.initialise()

        last_press = int(time())
        presses = ""
        while True:
            try:
                # self._logger.debug("Wait for command...")
                message = self._commands.get(timeout=COMMUNICATION_PERIOD)
                self._logger.info("Command: %s", message)

                if message == MONITOR_UPDATE_KEYPAD:
                    # load keypad from db
                    pass
                elif message in (MONITOR_ARM_AWAY, MONITOR_ARM_STAY):
                    self._keypad.set_armed(True)
                elif message == MONITOR_DISARM:
                    self._keypad.set_armed(False)
                elif message == MONITOR_STOP:
                    break

            except Empty:
                pass

            self._keypad.communicate()

            if int(time()) - last_press > 3 and presses:
                presses = ""
                self._logger.info("Cleared presses after 3 secs")

            if self._keypad.pressed in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                presses += self._keypad.pressed
                last_press = time()
            elif self._keypad.pressed in ("away", "stay"):
                last_press = time()
                pass
            else:
                # remove unknow codes from the list
                try:
                    self._keypad.pressed = ""
                except IndexError:
                    pass

            self._logger.debug("Presses: %s", presses)
            self._keypad.pressed = None

            if presses in self._codes:
                self._logger.debug("Code: %s", presses)
                self._responses.put(MONITOR_DISARM)
                self._keypad.set_armed(False)
                presses = ""
            elif len(presses) == 4:
                self._logger.info("Invalid code")
                presses = ""
