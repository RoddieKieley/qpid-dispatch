#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import
from __future__ import print_function

import unittest as unittest
import os, json, re, signal
import time

from system_test import TestCase, Qdrouterd, main_module, Process, TIMEOUT, DIR
from subprocess import PIPE, STDOUT
from proton import ConnectionException, Timeout, Url
from proton.handlers import MessagingHandler
from proton.reactor import Container
from proton.utils import BlockingConnection, LinkDetached, SyncRequestResponse
from qpid_dispatch_internal.policy.policy_util import is_ipv6_enabled
from qpid_dispatch_internal.compat import dict_iteritems
from test_broker import FakeBroker

class AbsoluteConnectionCountLimit(TestCase):
    """
    Verify that connections beyond the absolute limit are denied
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(AbsoluteConnectionCountLimit, cls).setUpClass()
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 2, 'enableVhostPolicy': 'false'})
        ])

        cls.router = cls.tester.qdrouterd('conn-limit-router', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def test_verify_maximum_connections(self):
        addr = self.address()

        # two connections should be ok
        denied = False
        try:
            bc1 = BlockingConnection(addr)
            bc2 = BlockingConnection(addr)
        except ConnectionException:
            denied = True

        self.assertFalse(denied) # assert if connections that should open did not open

        # third connection should be denied
        denied = False
        try:
            bc3 = BlockingConnection(addr)
        except ConnectionException:
            denied = True

        self.assertTrue(denied) # assert if connection that should not open did open

        bc1.close()
        bc2.close()

class LoadPolicyFromFolder(TestCase):
    """
    Verify that specifying a policy folder from the router conf file
    effects loading the policies in that folder.
    This test relies on qdmanage utility.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(LoadPolicyFromFolder, cls).setUpClass()

        ipv6_enabled = is_ipv6_enabled()

        policy_config_path = os.path.join(DIR, 'policy-1')
        replacements = {'{IPV6_LOOPBACK}':', ::1'}
        for f in os.listdir(policy_config_path):
            if f.endswith(".json.in"):
                with open(policy_config_path+"/"+f[:-3], 'w') as outfile:
                    with open(policy_config_path + "/" + f) as infile:
                        for line in infile:
                            for src, target in dict_iteritems(replacements):
                                if ipv6_enabled:
                                    line = line.replace(src, target)
                                else:
                                    line = line.replace(src, '')
                            outfile.write(line)

        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('conn-limit-router', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', re.sub(r'amqp://', 'amqp://u1:password@', self.address()), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect,
            universal_newlines=True)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception as e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def test_verify_policies_are_loaded(self):
        addr = self.address()

        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 5)

    def new_policy(self):
        return """
{
    "hostname": "dispatch-494",
    "maxConnections": 50,
    "maxConnectionsPerHost": 20,
    "maxConnectionsPerUser": 8,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def updated_policy(self):
        return """
{
    "maxConnections": 500,
    "maxConnectionsPerHost": 2,
    "maxConnectionsPerUser": 30,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 123,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 222
        }
    }
}
"""

    def test_verify_policy_add_update_delete(self):
        # verify current vhost count
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 5)

        # create
        self.run_qdmanage('create --type=vhost --name=dispatch-494 --stdin', input=self.new_policy())
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 6)
        found = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-494':
                found = True
                self.assertEqual(ruleset['maxConnections'], 50)
                self.assertEqual(ruleset['maxConnectionsPerHost'], 20)
                self.assertEqual(ruleset['maxConnectionsPerUser'], 8)
                break
        self.assertTrue(found)

        # update
        self.run_qdmanage('update --type=vhost --name=dispatch-494 --stdin', input=self.updated_policy())
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 6)
        found = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-494':
                found = True
                self.assertEqual(ruleset['maxConnections'], 500)
                self.assertEqual(ruleset['maxConnectionsPerHost'], 2)
                self.assertEqual(ruleset['maxConnectionsPerUser'], 30)
                break
        self.assertTrue(found)

        # delete
        self.run_qdmanage('delete --type=vhost --name=dispatch-494')
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 5)
        absent = True
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-494':
                absent = False
                break
        self.assertTrue(absent)

    def test_repeated_create_delete(self):
        for i in range(0, 10):
            rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
            self.assertEqual(len(rulesets), 5)

            # create
            self.run_qdmanage('create --type=vhost --name=dispatch-494 --stdin', input=self.new_policy())
            rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
            self.assertEqual(len(rulesets), 6)
            found = False
            for ruleset in rulesets:
                if ruleset['hostname'] == 'dispatch-494':
                    found = True
                    break
            self.assertTrue(found)

            # delete
            self.run_qdmanage('delete --type=vhost --name=dispatch-494')
            rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
            self.assertEqual(len(rulesets), 5)
            absent = True
            for ruleset in rulesets:
                if ruleset['hostname'] == 'dispatch-494':
                    absent = False
                    break
            self.assertTrue(absent)


class SenderReceiverLimits(TestCase):
    """
    Verify that specifying a policy folder from the router conf file
    effects loading the policies in that folder.
    This test relies on qdmanage utility.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(SenderReceiverLimits, cls).setUpClass()
        policy_config_path = os.path.join(DIR, 'policy-3')
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('SenderReceiverLimits', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def test_verify_n_receivers(self):
        n = 4
        addr = self.address()
        br1 = BlockingConnection(addr)

        # n receivers OK
        br1.create_receiver(address="****YES_1of4***")
        br1.create_receiver(address="****YES_20f4****")
        br1.create_receiver(address="****YES_3of4****")
        br1.create_receiver(address="****YES_4of4****")

        # receiver n+1 should be denied
        self.assertRaises(LinkDetached, br1.create_receiver, "****NO****")

        br1.close()

    def test_verify_n_senders(self):
        n = 2
        addr = self.address()
        bs1 = BlockingConnection(addr)

        # n senders OK
        bs1.create_sender(address="****YES_1of2****")
        bs1.create_sender(address="****YES_2of2****")
        # sender n+1 should be denied
        self.assertRaises(LinkDetached, bs1.create_sender, "****NO****")

        bs1.close()


class PolicyVhostOverride(TestCase):
    """
    Verify that specifying a policy folder from the router conf file
    effects loading the policies in that folder.
    This test relies on qdmanage utility.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(PolicyVhostOverride, cls).setUpClass()
        policy_config_path = os.path.join(DIR, 'policy-3')
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port(), 'policyVhost': 'override.host.com'}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('PolicyVhostOverride', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def test_verify_n_receivers(self):
        n = 4
        addr = self.address()
        br1 = BlockingConnection(addr)

        # n receivers OK
        br1.create_receiver(address="****YES_1of5***")
        br1.create_receiver(address="****YES_20f5****")
        br1.create_receiver(address="****YES_3of5****")
        br1.create_receiver(address="****YES_4of5****")
        br1.create_receiver(address="****YES_5of5****")

        # receiver n+1 should be denied
        self.assertRaises(LinkDetached, br1.create_receiver, "****NO****")

        br1.close()

    def test_verify_n_senders(self):
        n = 2
        addr = self.address()
        bs1 = BlockingConnection(addr)

        # n senders OK
        bs1.create_sender(address="****YES_1of3****")
        bs1.create_sender(address="****YES_2of3****")
        bs1.create_sender(address="****YES_3of3****")
        # sender n+1 should be denied
        self.assertRaises(LinkDetached, bs1.create_sender, "****NO****")

        bs1.close()


class InterrouterLinksAllowed(TestCase):

    inter_router_port = None

    @classmethod
    def setUpClass(cls):
        """Start a router"""
        super(InterrouterLinksAllowed, cls).setUpClass()

        policy_config_path = os.path.join(DIR, 'policy-5')

        def router(name, connection):

            config = [
                ('router', {'mode': 'interior', 'id': name}),
                ('listener', {'port': cls.tester.get_port()}),
                ('log', {'module': 'DEFAULT', 'enable': 'trace+'}),
                ('policy', {'enableVhostPolicy': 'yes', 'policyDir': policy_config_path}),
                connection
            ]

            config = Qdrouterd.Config(config)

            cls.routers.append(cls.tester.qdrouterd(name, config, wait=True))

        cls.routers = []

        inter_router_port = cls.tester.get_port()

        router('A', ('listener', {'role': 'inter-router', 'port': inter_router_port}))
        router('B', ('connector', {'name': 'connectorToA', 'role': 'inter-router', 'port': inter_router_port, 'verifyHostname': 'no'}))

        # With these configs before DISPATCH-920 the routers never connect
        # because the links are disallowed by policy. Before the wait_ready
        # functions complete the routers should have tried the interrouter
        # link.

        cls.routers[0].wait_ready()
        cls.routers[1].wait_ready()

        cls.routers[0].teardown()
        cls.routers[1].teardown()

    def test_01_router_links_allowed(self):
        with  open(self.routers[0].outfile + '.out', 'r') as router_log:
            log_lines = router_log.read().split("\n")
            disallow_lines = [s for s in log_lines if "link disallowed" in s]
            self.assertTrue(len(disallow_lines) == 0, msg='Inter-router links should be allowed but some were blocked by policy.')


class VhostPolicyNameField(TestCase):
    """
    Verify that vhosts can be created getting the name from
    'id' or from 'hostname'.
    This test relies on qdmanage utility.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(VhostPolicyNameField, cls).setUpClass()

        ipv6_enabled = is_ipv6_enabled()

        policy_config_path = os.path.join(DIR, 'policy-1')
        replacements = {'{IPV6_LOOPBACK}':', ::1'}
        for f in os.listdir(policy_config_path):
            if f.endswith(".json.in"):
                with open(policy_config_path+"/"+f[:-3], 'w') as outfile:
                    with open(policy_config_path + "/" + f) as infile:
                        for line in infile:
                            for src, target in dict_iteritems(replacements):
                                if ipv6_enabled:
                                    line = line.replace(src, target)
                                else:
                                    line = line.replace(src, '')
                            outfile.write(line)

        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('vhost-policy-name-field', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', re.sub(r'amqp://', 'amqp://u1:password@', self.address()), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect,
            universal_newlines=True)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception as e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def id_policy(self):
        return """
{
    "id": "dispatch-918",
    "maxConnections": 50,
    "maxConnectionsPerHost": 20,
    "maxConnectionsPerUser": 8,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def hostname_policy(self):
        return """
{
    "hostname": "dispatch-918",
    "maxConnections": 51,
    "maxConnectionsPerHost": 20,
    "maxConnectionsPerUser": 8,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def both_policy(self):
        return """
{
    "id":       "isogyre",
    "hostname": "dispatch-918",
    "maxConnections": 52,
    "maxConnectionsPerHost": 20,
    "maxConnectionsPerUser": 8,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def neither_policy(self):
        return """
{
    "maxConnections": 53,
    "maxConnectionsPerHost": 20,
    "maxConnectionsPerUser": 8,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 9999,
            "maxFrameSize": 222222,
            "sources": "public, private, $management, neither_policy",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""


    def test_01_id_vs_hostname(self):
        # verify current vhost count
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 5)

        # create using 'id'
        self.run_qdmanage('create --type=vhost --name=dispatch-918 --stdin', input=self.id_policy())
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 6)
        found = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-918':
                found = True
                self.assertEqual(ruleset['maxConnections'], 50)
                break
        self.assertTrue(found)

        # update using 'hostname'
        self.run_qdmanage('update --type=vhost --name=dispatch-918 --stdin', input=self.hostname_policy())
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 6)
        found = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-918':
                found = True
                self.assertEqual(ruleset['maxConnections'], 51)
                break
        self.assertTrue(found)

        # update 'id' and 'hostname'
        try:
            self.run_qdmanage('update --type=vhost --name=dispatch-918 --stdin',
                              input=self.both_policy())
            self.assertTrue(False) # should not be able to update 'id'
        except Exception as e:
            pass

        # update using neither
        self.run_qdmanage('update --type=vhost --name=dispatch-918 --stdin', input=self.neither_policy())
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 6)
        found = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'dispatch-918':
                found = True
                self.assertEqual(ruleset['maxConnections'], 53)
                break
        self.assertTrue(found)
        isoFound = False
        for ruleset in rulesets:
            if ruleset['hostname'] == 'isogyre':
                isoFound = True
                break
        self.assertFalse(isoFound)


class PolicyWarnings(TestCase):
    """
    Verify that specifying a policy that generates a warning does
    not cause the router to exit without showing the warning.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(PolicyWarnings, cls).setUpClass()
        listen_port = cls.tester.get_port()
        policy_config_path = os.path.join(DIR, 'policy-6')
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': listen_port}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('PolicyWarnings', config, wait=False)
        try:
            cls.router.wait_ready(timeout = 5)
        except Exception as e:
            pass

    def test_03_policy_warnings(self):
        with  open('../setUpClass/PolicyWarnings.log', 'r') as router_log:
            log_lines = router_log.read().split("\n")
            critical_lines = [s for s in log_lines if "'PolicyManager' object has no attribute 'log_warning'" in s]
            self.assertTrue(len(critical_lines) == 0, msg='Policy manager does not forward policy warnings and shuts down instead.')


class PolicyLinkNamePatternTest(TestCase):
    """
    Verify that specifying a policy that generates a warning does
    not cause the router to exit without showing the warning.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(PolicyLinkNamePatternTest, cls).setUpClass()
        listen_port = cls.tester.get_port()
        policy_config_path = os.path.join(DIR, 'policy-7')
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': listen_port}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('PolicyLinkNamePatternTest', config, wait=False)
        try:
            cls.router.wait_ready(timeout = 5)
        except Exception as e:
            pass

    def address(self):
        return self.router.addresses[0]

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', re.sub(r'amqp://', 'amqp://u1:password@', self.address()), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect,
            universal_newlines=True)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception as e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def default_patterns(self):
        return """
{
    "hostname": "$default",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sourcePattern": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targetPattern": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def disallowed_source(self):
        return """
{
    "hostname": "DISPATCH-1993-2",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sources":       "public, private, $management",
            "sourcePattern": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targetPattern": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def disallowed_target(self):
        return """
{
    "id": "DISPATCH-1993-3",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sourcePattern": "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targetPattern": "public, private, $management",
            "targets": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def disallowed_source_pattern1(self):
        return """
{
    "id": "DISPATCH-1993-3",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sourcePattern": "public, private, $management, abc-${user}.xyz",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targetPattern": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def disallowed_source_pattern2(self):
        return """
{
    "id": "DISPATCH-1993-3",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sourcePattern": "public, private, $management, abc/${user}.xyz",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "targetPattern": "public, private, $management",
            "maxSenders": 22
        }
    }
}
"""

    def test_link_name_parse_tree_patterns(self):
        # update to replace source/target match patterns
        qdm_out = "<not written>"
        try:
            qdm_out = self.run_qdmanage('update --type=vhost --name=vhost/$default --stdin', input=self.default_patterns())
        except Exception as e:
            self.assertTrue(False, msg=('Error running qdmanage %s' % str(e)))
        self.assertFalse("PolicyError" in qdm_out)

        # attempt an create that should be rejected
        qdm_out = "<not written>"
        exception = False
        try:
            qdm_out = self.run_qdmanage('create --type=vhost --name=DISPATCH-1993-2 --stdin', input=self.disallowed_source())
        except Exception as e:
            exception = True
            self.assertTrue("InternalServerErrorStatus: PolicyError: Policy 'DISPATCH-1993-2' is invalid:" in str(e))
        self.assertTrue(exception)

        # attempt another create that should be rejected
        qdm_out = "<not written>"
        exception = False
        try:
            qdm_out = self.run_qdmanage('create --type=vhost --name=DISPATCH-1993-3 --stdin', input=self.disallowed_target())
        except Exception as e:
            exception = True
            self.assertTrue("InternalServerErrorStatus: PolicyError: Policy 'DISPATCH-1993-3' is invalid:" in str(e))
        self.assertTrue(exception)

        # attempt another create that should be rejected - name subst must whole token
        qdm_out = "<not written>"
        exception = False
        try:
            qdm_out = self.run_qdmanage('create --type=vhost --name=DISPATCH-1993-3 --stdin', input=self.disallowed_source_pattern1())
        except Exception as e:
            exception = True
            self.assertTrue("InternalServerErrorStatus: PolicyError:" in str(e))
            self.assertTrue("Policy 'DISPATCH-1993-3' is invalid:" in str(e))
        self.assertTrue(exception)

        # attempt another create that should be rejected - name subst must be prefix or suffix
        qdm_out = "<not written>"
        exception = False
        try:
            qdm_out = self.run_qdmanage('create --type=vhost --name=DISPATCH-1993-3 --stdin', input=self.disallowed_source_pattern2())
        except Exception as e:
            exception = True
            self.assertTrue("InternalServerErrorStatus: PolicyError:" in str(e))
            self.assertTrue("Policy 'DISPATCH-1993-3' is invalid:" in str(e))
        self.assertTrue(exception)


class PolicyHostamePatternTest(TestCase):
    """
    Verify hostname pattern matching
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(PolicyHostamePatternTest, cls).setUpClass()
        listen_port = cls.tester.get_port()
        policy_config_path = os.path.join(DIR, 'policy-8')
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy8'}),
            ('listener', {'port': listen_port}),
            ('policy', {'maxConnections': 2, 'policyDir': policy_config_path, 'enableVhostPolicy': 'true', 'enableVhostNamePatterns': 'true'})
        ])

        cls.router = cls.tester.qdrouterd('PolicyVhostNamePatternTest', config, wait=True)
        try:
            cls.router.wait_ready(timeout = 5)
        except Exception:
            pass

    def address(self):
        return self.router.addresses[0]

    def run_qdmanage(self, cmd, input=None, expect=Process.EXIT_OK):
        p = self.popen(
            ['qdmanage'] + cmd.split(' ') + ['--bus', re.sub(r'amqp://', 'amqp://u1:password@', self.address()), '--indent=-1', '--timeout', str(TIMEOUT)],
            stdin=PIPE, stdout=PIPE, stderr=STDOUT, expect=expect,
            universal_newlines=True)
        out = p.communicate(input)[0]
        try:
            p.teardown()
        except Exception as e:
            raise Exception("%s\n%s" % (e, out))
        return out

    def disallowed_hostname(self):
        return """
{
    "hostname": "#.#.0.0",
    "maxConnections": 3,
    "maxConnectionsPerHost": 3,
    "maxConnectionsPerUser": 3,
    "allowUnknownUser": true,
    "groups": {
        "$default": {
            "allowAnonymousSender": true,
            "maxReceivers": 99,
            "users": "*",
            "maxSessionWindow": 1000000,
            "maxFrameSize": 222222,
            "sources":       "public, private, $management",
            "maxMessageSize": 222222,
            "allowDynamicSource": true,
            "remoteHosts": "*",
            "maxSessions": 2,
            "maxSenders": 22
        }
    }
}
"""

    def test_hostname_pattern_00_hello(self):
        rulesets = json.loads(self.run_qdmanage('query --type=vhost'))
        self.assertEqual(len(rulesets), 1)

    def test_hostname_pattern_01_denied_add(self):
        qdm_out = "<not written>"
        try:
            qdm_out = self.run_qdmanage('create --type=vhost --name=#.#.0.0 --stdin', input=self.disallowed_hostname())
        except Exception as e:
            self.assertTrue("pattern conflicts" in str(e), msg=('Error running qdmanage %s' % str(e)))
        self.assertFalse("222222" in qdm_out)


class VhostPolicyFromRouterConfig(TestCase):
    """
    Verify that connections beyond the vhost limit are denied.
    Differently than global maxConnections, opening a connection
    does not raise a ConnectionException, but when an attempt to
    create a sync request and response client is made after limit
    is reached, the connection times out.
    """
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(VhostPolicyFromRouterConfig, cls).setUpClass()
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 100, 'enableVhostPolicy': 'true'}),
            ('vhost', {
                'hostname': '0.0.0.0', 'maxConnections': 2,
                'allowUnknownUser': 'true',
                'groups': [(
                    '$default', {
                        'users': '*', 'remoteHosts': '*',
                        'sources': '*', 'targets': '*',
                        'allowDynamicSource': 'true'
                    }
                ), (
                    'anonymous', {
                        'users': 'anonymous', 'remoteHosts': '*',
                        'sourcePattern': 'addr/*/queue/*, simpleaddress, queue.${user}',
                        'targets': 'addr/*, simpleaddress, queue.${user}',
                        'allowDynamicSource': 'true',
                        'allowAnonymousSender': 'true'
                    }
                )]
            })
        ])

        cls.router = cls.tester.qdrouterd('vhost-conn-limit-router', config, wait=True)

    def address(self):
        return self.router.addresses[0]

    def test_verify_vhost_maximum_connections(self):
        addr = "%s/$management" % self.address()
        timeout = 5

        # two connections should be ok
        denied = False
        try:
            bc1 = SyncRequestResponse(BlockingConnection(addr, timeout=timeout))
            bc2 = SyncRequestResponse(BlockingConnection(addr, timeout=timeout))
        except ConnectionException:
            denied = True
        except Timeout:
            denied = True

        self.assertFalse(denied)  # assert connections were opened

        # third connection should be denied
        denied = False
        try:
            bc3 = SyncRequestResponse(BlockingConnection(addr, timeout=timeout))
        except ConnectionException:
            denied = True
        except Timeout:
            denied = True

        self.assertTrue(denied)  # assert if connection that should not open did open

        bc1.connection.close()
        bc2.connection.close()

    def test_vhost_allowed_addresses(self):
        target_addr_list = ['addr/something', 'simpleaddress', 'queue.anonymous']
        source_addr_list = ['addr/something/queue/one', 'simpleaddress', 'queue.anonymous']

        # Attempt to connect to all allowed target addresses
        for target_addr in target_addr_list:
            sender = SenderAddressValidator("%s/%s" % (self.address(), target_addr))
            self.assertFalse(sender.link_error,
                             msg="target address must be allowed, but it was not [%s]" % target_addr)

        # Attempt to connect to all allowed source addresses
        for source_addr in source_addr_list:
            receiver = ReceiverAddressValidator("%s/%s" % (self.address(), source_addr))
            self.assertFalse(receiver.link_error,
                             msg="source address must be allowed, but it was not [%s]" % source_addr)

    def test_vhost_denied_addresses(self):
        target_addr_list = ['addr', 'simpleaddress1', 'queue.user']
        source_addr_list = ['addr/queue/one', 'simpleaddress1', 'queue.user']

        # Attempt to connect to all not allowed target addresses
        for target_addr in target_addr_list:
            sender = SenderAddressValidator("%s/%s" % (self.address(), target_addr))
            self.assertTrue(sender.link_error,
                            msg="target address must not be allowed, but it was [%s]" % target_addr)

        # Attempt to connect to all not allowed source addresses
        for source_addr in source_addr_list:
            receiver = ReceiverAddressValidator("%s/%s" % (self.address(), source_addr))
            self.assertTrue(receiver.link_error,
                            msg="source address must not be allowed, but it was [%s]" % source_addr)


class ClientAddressValidator(MessagingHandler):
    """
    Base client class used to validate vhost policies through
    receiver or clients based on allowed target and source
    addresses.
    Implementing classes must provide on_start() implementation
    and create the respective sender or receiver.
    """
    TIMEOUT = 3

    def __init__(self, url):
        super(ClientAddressValidator, self).__init__()
        self.url = Url(url)
        self.container = Container(self)
        self.link_error = False
        self.container.run()
        signal.signal(signal.SIGALRM, self.timeout)
        signal.alarm(ClientAddressValidator.TIMEOUT)

    def timeout(self, signum, frame):
        """
        In case router crashes or something goes wrong and client
        is unable to connect, this method will be invoked and
        set the link_error to True
        :param signum:
        :param frame:
        :return:
        """
        self.link_error = True
        self.container.stop()

    def on_link_error(self, event):
        """
        When link was closed by the router due to policy violation.
        :param event:
        :return:
        """
        self.link_error = True
        event.connection.close()
        signal.alarm(0)

    def on_link_opened(self, event):
        """
        When link was opened without error.
        :param event:
        :return:
        """
        event.connection.close()
        signal.alarm(0)


class ReceiverAddressValidator(ClientAddressValidator):
    """
    Receiver implementation used to validate vhost policies
    applied to source addresses.
    """
    def __init__(self, url):
        super(ReceiverAddressValidator, self).__init__(url)

    def on_start(self, event):
        """
        Creates the receiver.
        :param event:
        :return:
        """
        event.container.create_receiver(self.url)


class SenderAddressValidator(ClientAddressValidator):
    """
    Sender implementation used to validate vhost policies
    applied to target addresses.
    """
    def __init__(self, url):
        super(SenderAddressValidator, self).__init__(url)

    def on_start(self, event):
        """
        Creates the sender
        :param event:
        :return:
        """
        event.container.create_sender(self.url)


#
# Connector policy tests
#

class ConnectorPolicyMisconfiguredClient(FakeBroker):
    '''
    This client is targeted by a misconfigured connector whose policy
    causes an immediate connection close.
    '''
    def __init__(self, url, container_id=None):
        super(ConnectorPolicyMisconfiguredClient, self).__init__(url, container_id)
        self.connection_opening = 0
        self.connection_opened = 0
        self.connection_error = 0
        self.main_exited = False

    def _main(self):
        self._container.timeout = 1.0
        self._container.start()

        keep_running = True
        while keep_running:
            try:
                self._container.process()
            except:
                self._stop_thread = True
                keep_running = False
            if self._stop_thread:
                keep_running = False
        self.main_exited = True

    def join(self):
        if not self._stop_thread:
            self._stop_thread = True
            self._container.wakeup()
        if not self.main_exited:
            self._thread.join(timeout=5)

    def on_start(self, event):
        self.timer          = event.reactor.schedule(10.0, Timeout(self))        
        self.acceptor = event.container.listen(self.url)

    def timeout(self):
        self._error = "Timeout Expired"

    def on_connection_opening(self, event):
        self.connection_opening += 1
        super(ConnectorPolicyMisconfiguredClient, self).on_connection_opening(event)
        
    def on_connection_opened(self, event):
        self.connection_opened += 1
        super(ConnectorPolicyMisconfiguredClient, self).on_connection_opened(event)

    def on_connection_error(self, event):
        self.connection_error += 1


class ConnectorPolicyMisconfigured(TestCase):
    """
    Verify that a connector that has a vhostPolicy is not allowed
    to open the connection if the policy is not defined
    """
    remoteListenerPort = None
    
    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(ConnectorPolicyMisconfigured, cls).setUpClass()
        cls.remoteListenerPort = cls.tester.get_port();
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 100, 'enableVhostPolicy': 'true'}),
            ('connector', {'verifyHostname': 'false', 'name': 'novhost',
                           'idleTimeoutSeconds': 120, 'saslMechanisms': 'ANONYMOUS',
                           'host': '127.0.0.1', 'role': 'normal',
                           'port': cls.remoteListenerPort, 'policyVhost': 'nosuch'
                            }),

            ('vhost', {
                'hostname': '0.0.0.0', 'maxConnections': 2,
                'allowUnknownUser': 'true',
                'groups': [(
                    '$default', {
                        'users': '*', 'remoteHosts': '*',
                        'sources': '*', 'targets': '*',
                        'allowDynamicSource': 'true'
                    }
                ), (
                    'anonymous', {
                        'users': 'anonymous', 'remoteHosts': '*',
                        'sourcePattern': 'addr/*/queue/*, simpleaddress, queue.${user}',
                        'targets': 'addr/*, simpleaddress, queue.${user}',
                        'allowDynamicSource': 'true',
                        'allowAnonymousSender': 'true'
                    }
                )]
            })
        ])

        cls.router = cls.tester.qdrouterd('connectorPolicyMisconfigured', config, wait=False)

    def address(self):
        return self.router.addresses[0]

    def test_30_connector_policy_misconfigured(self):
        url = "127.0.0.1:%d" % self.remoteListenerPort
        tc = ConnectorPolicyMisconfiguredClient(url, "tc")
        while tc.connection_error == 0 and tc._error == None:
            time.sleep(0.1)
        tc.join()
        self.assertTrue(tc.connection_error == 1)
        
#

class ConnectorPolicyClient(FakeBroker):
    '''
    This client is targeted by a configured connector whose policy
    allows certain sources and targets.
    '''
    def __init__(self, url, container_id=None):
        super(ConnectorPolicyClient, self).__init__(url, container_id)
        self.connection_opening = 0
        self.connection_opened = 0
        self.connection_error = 0
        self.main_exited = False
        self.senders = []
        self.receivers = []
        self.link_error = False
        self.sender_request = ""
        self.receiver_request = ""
        self.request_in_flight = False
        self.req_close_sender = False
        self.req_close_receiver = False
        self.req_anonymous_sender = False

    def _main(self):
        self._container.timeout = 1.0
        self._container.start()

        keep_running = True
        while keep_running:
            try:
                self._container.process()
                if not self.request_in_flight:
                    if self.sender_request != "":
                        sndr = self._container.create_sender(
                                self._connections[0], self.sender_request)
                        self.senders.append(sndr)
                        self.request_in_flight = True
                        self.sender_request = ""
                    elif self.receiver_request != "":
                        rcvr = self._container.create_receiver(
                                self._connections[0], self.receiver_request)
                        self.receivers.append(rcvr)
                        self.request_in_flight = True
                        self.receiver_request = ""
                    elif self.req_close_sender:
                        self.senders[0].close()
                        self.req_close_sender = False
                    elif self.req_close_receiver:
                        self.receivers[0].close()
                        self.req_close_receiver = False
                    elif self.req_anonymous_sender:
                        sndr = self._container.create_sender(
                                self._connections[0], name="anon")
                        self.senders.append(sndr)
                        self.request_in_flight = True
                        self.req_anonymous_sender = False

            except:
                self._stop_thread = True
                keep_running = False
            if self._stop_thread:
                keep_running = False
        self.main_exited = True

    def join(self):
        if not self._stop_thread:
            self._stop_thread = True
            self._container.wakeup()
        if not self.main_exited:
            self._thread.join(timeout=5)

    def on_start(self, event):
        self.timer    = event.reactor.schedule(60, Timeout(self))        
        self.acceptor = event.container.listen(self.url)

    def timeout(self):
        self._error = "Timeout Expired"

    def on_connection_opening(self, event):
        self.connection_opening += 1
        super(ConnectorPolicyClient, self).on_connection_opening(event)

    def on_connection_opened(self, event):
        self.connection_opened += 1
        super(ConnectorPolicyClient, self).on_connection_opened(event)

    def on_connection_error(self, event):
        self.connection_error += 1

    def on_link_opened(self, event):
        self.request_in_flight = False

    def on_link_error(self, event):
        self.link_error = True
        self.request_in_flight = False

    def try_sender(self, addr):
        self.link_error = False
        self.sender_request = addr
        while (self.sender_request == addr or self.request_in_flight) \
                and self.link_error == False and self._error is None:
            time.sleep(0.10)
        time.sleep(0.10)
        return self.link_error == False

    def try_receiver(self, addr):
        self.link_error = False
        self.receiver_request = addr
        while (self.receiver_request == addr or self.request_in_flight) \
                and self.link_error == False and self._error is None:
            time.sleep(0.10)
        time.sleep(0.10)
        return self.link_error == False

    def close_sender(self):
        self.req_close_sender = True
        while self.req_close_sender:
            time.sleep(0.05)

    def close_receiver(self):
        self.req_close_receiver = True
        while self.req_close_receiver:
            time.sleep(0.05)

    def try_anonymous_sender(self):
        self.link_error = False
        self.req_anonymous_sender = True
        while (self.req_anonymous_sender or self.request_in_flight) \
                and self.link_error == False and self._error is None:
            time.sleep(0.10)
        time.sleep(0.10)
        return self.link_error == False


class ConnectorPolicySrcTgt(TestCase):
    """
    Verify that a connector that has a vhostPolicy
     * may open the connection
     * may access allowed sources and targets
     * may not access disallowed sources and targets
    """
    remoteListenerPort = None

    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(ConnectorPolicySrcTgt, cls).setUpClass()
        cls.remoteListenerPort = cls.tester.get_port();
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 100, 'enableVhostPolicy': 'true'}),
            ('connector', {'verifyHostname': 'false', 'name': 'novhost',
                           'idleTimeoutSeconds': 120, 'saslMechanisms': 'ANONYMOUS',
                           'host': '127.0.0.1', 'role': 'normal',
                           'port': cls.remoteListenerPort, 'policyVhost': 'test'
                            }),
            # Set up the prefix 'node' as a prefix for waypoint addresses
            ('address',  {'prefix': 'node', 'waypoint': 'yes'}),
            # Create a pair of default auto-links for 'node.1'
            ('autoLink', {'addr': 'node.1', 'containerId': 'container.1', 'direction': 'in'}),
            ('autoLink', {'addr': 'node.1', 'containerId': 'container.1', 'direction': 'out'}),
            ('vhost', {
                'hostname': 'test',
                'groups': [(
                    '$connector', {
                        'sources': 'test,examples,work*',
                        'targets': 'examples,$management,play*',
                    }
                )]
            })
        ])

        cls.router = cls.tester.qdrouterd('ConnectorPolicySrcTgt', config, wait=False)

    def address(self):
        return self.router.addresses[0]

    def test_31_connector_policy(self):
        url = "127.0.0.1:%d" % self.remoteListenerPort
        cpc = ConnectorPolicyClient(url, "cpc")
        while cpc.connection_opened == 0 and cpc._error == None:
            time.sleep(0.1)
        time.sleep(0.05)
        self.assertTrue(cpc.connection_error == 0) # expect connection to stay up
        self.assertTrue(cpc._error is None)

        # senders that should work
        for addr in ["examples", "$management", "playtime"]: # allowed targets
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertTrue(res)

        # senders that should fail
        for addr in ["test", "a/bad/addr"]: # denied targets
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertFalse(res)

        # receivers that should work
        for addr in ["examples", "test", "workaholic"]: # allowed sources
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertTrue(res)

        # receivers that should fail
        for addr in ["$management", "a/bad/addr"]: # denied sources
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertFalse(res)

        # anonomyous sender should be disallowed
        res = cpc.try_anonymous_sender()
        self.assertFalse(res)

        # waypoint links should be disallowed
        res = cpc.try_sender("node.1")
        self.assertFalse(res)
        res = cpc.try_receiver("node.1")
        self.assertFalse(res)


class ConnectorPolicyNSndrRcvr(TestCase):
    """
    Verify that a connector that has a vhostPolicy is allowed
     * to open the connection
     * is limited to the number of senders and receivers specified in the policy
    """
    remoteListenerPort = None
    MAX_SENDERS = 4
    MAX_RECEIVERS = 3

    @classmethod
    def setUpClass(cls):
        """Start the router"""
        super(ConnectorPolicyNSndrRcvr, cls).setUpClass()
        cls.remoteListenerPort = cls.tester.get_port();
        config = Qdrouterd.Config([
            ('router', {'mode': 'standalone', 'id': 'QDR.Policy'}),
            ('listener', {'port': cls.tester.get_port()}),
            ('policy', {'maxConnections': 100, 'enableVhostPolicy': 'true'}),
            ('connector', {'verifyHostname': 'false', 'name': 'novhost',
                           'idleTimeoutSeconds': 120, 'saslMechanisms': 'ANONYMOUS',
                           'host': '127.0.0.1', 'role': 'normal',
                           'port': cls.remoteListenerPort, 'policyVhost': 'test'
                            }),
            # Set up the prefix 'node' as a prefix for waypoint addresses
            ('address',  {'prefix': 'node', 'waypoint': 'yes'}),
            # Create a pair of default auto-links for 'node.1'
            ('autoLink', {'addr': 'node.1', 'containerId': 'container.1', 'direction': 'in'}),
            ('autoLink', {'addr': 'node.1', 'containerId': 'container.1', 'direction': 'out'}),
            ('vhost', {
                'hostname': 'test',
                'groups': [(
                    '$connector', {
                        'sources': '*',
                        'targets': '*',
                        'maxSenders': cls.MAX_SENDERS,
                        'maxReceivers': cls.MAX_RECEIVERS,
                        'allowAnonymousSender': 'true',
                        'allowWaypointLinks': 'true'
                    }
                )]
            })
        ])

        cls.router = cls.tester.qdrouterd('ConnectorPolicyNSndrRcvr', config, wait=False)

    def address(self):
        return self.router.addresses[0]

    def test_32_connector_policy_max_sndr_rcvr(self):
        url = "127.0.0.1:%d" % self.remoteListenerPort
        cpc = ConnectorPolicyClient(url, "cpc")
        while cpc.connection_opened == 0 and cpc._error == None:
            time.sleep(0.1)
        time.sleep(0.05)
        self.assertTrue(cpc.connection_error == 0) # expect connection to stay up
        self.assertTrue(cpc._error is None)

        # senders that should work
        # anonomyous sender should be allowed
        res = cpc.try_anonymous_sender()     # sender 1
        self.assertTrue(res)

        # waypoint links should be allowed
        res = cpc.try_sender("node.1")       # semder 2
        self.assertTrue(res)
        res = cpc.try_receiver("node.1")     # receiver 1
        self.assertTrue(res)

        addr = "vermillion"
        for i in range(self.MAX_SENDERS - 2):
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertTrue(res)

        # senders that should fail
        for i in range(2):
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertFalse(res)

        # receivers that should work
        for i in range(self.MAX_RECEIVERS - 1):
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertTrue(res)

        # receivers that should fail
        for i in range(2):
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertFalse(res)

        # close a sender and verify that another one only may open
        addr="skyblue"
        cpc.close_sender()

        for i in range(1):
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertTrue(res)

        # senders that should fail
        for i in range(1):
            try:
                res = cpc.try_sender(addr)
            except:
                res = False
            self.assertFalse(res)

        # close a receiver and verify that another one only may open
        cpc.close_receiver()

        for i in range(1):
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertTrue(res)

        # senders that should fail
        for i in range(1):
            try:
                res = cpc.try_receiver(addr)
            except:
                res = False
            self.assertFalse(res)


if __name__ == '__main__':
    unittest.main(main_module())
