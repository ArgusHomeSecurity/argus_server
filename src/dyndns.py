#!/usr/bin/env python

import json
import logging
import socket
from copy import copy
from ipaddress import ip_address

import requests
from noipy.main import execute_update

from _socket import gaierror
from models import Option, db
from monitoring.constants import LOG_DYNDNS
from tools.dictionary import filter_keys


class DynDns:

    def __init__(self, logger=None):
        self._logger = logger if logger else logging.getLogger(LOG_DYNDNS)

    def update_ip(self, force=False):
        '''
        Compare IP address in DNS server and actual lookup result.
        Update the IP address at DNSprovider if it's necesarry.
        :param force: force the update
        '''
        noip_config = Option.query.filter_by(name='network', section='dyndns').first()
        if noip_config:
            noip_config = json.loads(noip_config.value)

        if not noip_config:
            self._logger.error("Missing dyndns settings!")
            return

        noip_config['force'] = force
        tmp_config = copy(noip_config)
        filter_keys(tmp_config, ['password'])
        self._logger.info("Update dynamics DNS provider with options: %s" % tmp_config)

        # DNS lookup IP from hostname
        try:
            current_ip = socket.gethostbyname(noip_config['hostname'])
        except gaierror:
            return False

        # Getting public IP
        new_ip = requests.get("http://ifconfig.me/ip").text.strip()
        try:
            # converting the address to string for comparision
            new_ip = format(ip_address(new_ip))
        except ValueError:
            self._logger.info("Invalid IP address: %s" % new_ip)
            return False

        self._logger.info("IP: '%s' => '%s'" % (current_ip, new_ip))

        if (new_ip != current_ip) or force:
            noip_config['ip'] = new_ip
            result = self.save_ip(noip_config)
            self._logger.info("Update result: '%s'" % result)
            return True
        else:
            self._logger.info("No IP update necessary")

        return True

    def save_ip(self, noip_config):
        '''
        Save IP to the DNS provider
        :param noip_config: dictonary of settings (provider, username, passowrd, hostname, ip)
        '''
        class Arguments():
            pass

        args = Arguments()
        args.store = False
        args.provider = noip_config['provider']
        args.usertoken = noip_config['username']
        args.password = noip_config['password']
        args.hostname = noip_config['hostname']
        args.ip = noip_config['ip']
        return execute_update(args)


if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)-15s %(message)s', level=logging.INFO)

    DynDns(logging.getLogger("argus_noip")).update_ip()
