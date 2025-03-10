#
# auto-pts - The Bluetooth PTS Automation Framework
#
# Copyright (c) 2017, Intel Corporation.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
import binascii
import logging
import copy
import threading
from threading import Lock, Timer, Event
from time import sleep

from autopts.pybtp import defs
from autopts.pybtp.types import AdType, Addr, IOCap
from autopts.utils import raise_on_global_end, ResultWithFlag

STACK = None
log = logging.debug


class GattAttribute:
    def __init__(self, handle, perm, uuid, att_rsp):
        self.handle = handle
        self.perm = perm
        self.uuid = uuid
        self.att_read_rsp = att_rsp


class GattService(GattAttribute):
    def __init__(self, handle, perm, uuid, att_rsp, end_handle=None):
        super().__init__(handle, perm, uuid, att_rsp)
        self.end_handle = end_handle


class GattPrimary(GattService):
    pass


class GattSecondary(GattService):
    pass


class GattServiceIncluded(GattAttribute):
    def __init__(self, handle, perm, uuid, att_rsp, incl_svc_hdl, end_grp_hdl):
        GattAttribute.__init__(self, handle, perm, uuid, att_rsp)
        self.incl_svc_hdl = incl_svc_hdl
        self.end_grp_hdl = end_grp_hdl


class GattCharacteristic(GattAttribute):
    def __init__(self, handle, perm, uuid, att_rsp, prop, value_handle):
        GattAttribute.__init__(self, handle, perm, uuid, att_rsp)
        self.prop = prop
        self.value_handle = value_handle


class GattCharacteristicDescriptor(GattAttribute):
    def __init__(self, handle, perm, uuid, att_rsp, value):
        GattAttribute.__init__(self, handle, perm, uuid, att_rsp)
        self.value = value
        self.has_changed_cnt = 0
        self.has_changed = Event()


class GattDB:
    def __init__(self):
        self.db = dict()

    def attr_add(self, handle, attr):
        self.db[handle] = attr

    def attr_lookup_handle(self, handle):
        if handle in self.db:
            return self.db[handle]
        return None


class Property:
    def __init__(self, data):
        self._lock = Lock()
        self.data = data

    def __get__(self, instance, owner):
        with self._lock:
            return getattr(instance, self.data)

    def __set__(self, instance, value):
        with self._lock:
            setattr(instance, self.data, value)


class WildCard:
    def __eq__(self, other):
        return True


def wait_event_with_condition(event_queue, condition_cb, timeout, remove):
    flag = Event()
    flag.set()

    t = Timer(timeout, timeout_cb, [flag])
    t.start()

    while flag.is_set():
        raise_on_global_end()

        for ev in event_queue:
            if isinstance(ev, tuple):
                result = condition_cb(*ev)
            else:
                result = condition_cb(ev)

            if result:
                t.cancel()
                if ev and remove:
                    event_queue.remove(ev)

                return ev

            # TODO: Use wait() and notify() from threading.Condition
            #  instead of sleep()
            sleep(0.5)

    return None


def wait_for_event_iut(event_queue, timeout, remove):
    flag = Event()
    flag.set()

    t = Timer(timeout, timeout_cb, [flag])
    t.start()

    while flag.is_set():
        raise_on_global_end()

        for ev in event_queue:
            if ev:
                t.cancel()
                if ev and remove:
                    event_queue.remove(ev)
                return ev

            sleep(0.5)

    return None


def timeout_cb(flag):
    flag.clear()


class ConnParams:
    def __init__(self, conn_itvl_min, conn_itvl_max, conn_latency, supervision_timeout):
        self.conn_itvl_min = conn_itvl_min
        self.conn_itvl_max = conn_itvl_max
        self.conn_latency = conn_latency
        self.supervision_timeout = supervision_timeout


class Gap:
    def __init__(self, name, manufacturer_data, appearance, svc_data, flags,
                 svcs, uri=None, periodic_data=None, le_supp_feat=None):

        self.ad = {}
        self.sd = {}

        if name:
            if isinstance(name, bytes):
                self.ad[AdType.name_full] = name
            else:
                self.ad[AdType.name_full] = name.encode('utf-8')

        if manufacturer_data:
            self.sd[AdType.manufacturer_data] = manufacturer_data

        self.name = name
        self.manufacturer_data = manufacturer_data
        self.appearance = appearance
        self.svc_data = svc_data
        self.flags = flags
        self.svcs = svcs
        self.uri = uri
        self.le_supp_feat = le_supp_feat
        self.periodic_data = periodic_data
        self.oob_legacy = "0000000000000000FE12036E5A889F4D"

        # If disconnected - None
        # If connected - remote address tuple (addr, addr_type)
        self.connected = Property(None)
        self.current_settings = Property({
            "Powered": False,
            "Connectable": False,
            "Fast Connectable": False,
            "Discoverable": False,
            "Bondable": False,
            "Link Level Security": False,  # Link Level Security (Sec. mode 3)
            "SSP": False,  # Secure Simple Pairing
            "BREDR": False,  # Basic Rate/Enhanced Data Rate
            "HS": False,  # High Speed
            "LE": False,  # Low Energy
            "Advertising": False,
            "SC": False,  # Secure Connections
            "Debug Keys": False,
            "Privacy": False,
            "Controller Configuration": False,
            "Static Address": False,
            "Extended Advertising": False,
            "SC Only": False,
        })
        self.iut_bd_addr = Property({
            "address": None,
            "type": None,
        })
        self.discoverying = Property(False)
        self.found_devices = Property([])  # List of found devices

        self.passkey = Property(None)
        self.conn_params = Property(None)
        self.pairing_failed_rcvd = Property(None)

        # bond_lost data (addr_type, addr)
        self.bond_lost_ev_data = Property(None)
        # if no io_cap was set it means we use no_input_output
        self.io_cap = IOCap.no_input_output
        self.sec_level = Property(None)
        # if IUT doesn't support it, it should be disabled in preconditions
        self.pair_user_interaction = True
        self.periodic_report_rxed = False
        self.periodic_sync_established_rxed = False
        self.periodic_transfer_received = False

    def wait_for_connection(self, timeout, conn_count=0):
        if self.is_connected(conn_count):
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.is_connected(conn_count):
                t.cancel()
                return True

        return False

    def wait_for_disconnection(self, timeout):
        if not self.is_connected(0):
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if not self.is_connected(0):
                t.cancel()
                return True

        return False

    def is_connected(self, conn_count):
        if conn_count > 0:
            if self.connected.data is not None:
                return len(self.connected.data) >= conn_count
            return False

        return self.connected.data

    def wait_periodic_report(self, timeout):
        if self.periodic_report_rxed:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.periodic_report_rxed:
                t.cancel()
                self.periodic_report_rxed = False
                return True

        return False

    def wait_periodic_established(self, timeout):
        if self.periodic_sync_established_rxed:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.periodic_sync_established_rxed:
                t.cancel()
                self.periodic_sync_established_rxed = False
                return True

        return False

    def wait_periodic_transfer_received(self, timeout):
        if self.periodic_transfer_received:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.periodic_transfer_received:
                t.cancel()
                self.periodic_transfer_received = False
                return True

        return False

    def current_settings_set(self, key):
        if key in self.current_settings.data:
            self.current_settings.data[key] = True
        else:
            logging.error("%s %s not in current_settings",
                          self.current_settings_set.__name__, key)

    def current_settings_clear(self, key):
        if key in self.current_settings.data:
            self.current_settings.data[key] = False
        else:
            logging.error("%s %s not in current_settings",
                          self.current_settings_clear.__name__, key)

    def current_settings_get(self, key):
        if key in self.current_settings.data:
            return self.current_settings.data[key]
        logging.error("%s %s not in current_settings",
                      self.current_settings_get.__name__, key)
        return False

    def iut_addr_get_str(self):
        addr = self.iut_bd_addr.data["address"]
        if addr:
            return addr.decode("utf-8")
        return "000000000000"

    def iut_addr_set(self, addr, addr_type):
        self.iut_bd_addr.data["address"] = addr
        self.iut_bd_addr.data["type"] = addr_type

    def iut_addr_is_random(self):
        return self.iut_bd_addr.data["type"] == Addr.le_random

    def iut_has_privacy(self):
        return self.current_settings_get("Privacy")

    def set_conn_params(self, params):
        self.conn_params.data = params

    def reset_discovery(self):
        self.discoverying.data = True
        self.found_devices.data = []

    def set_passkey(self, passkey):
        self.passkey.data = passkey

    def get_passkey(self, timeout=5):
        if self.passkey.data is None:
            flag = Event()
            flag.set()

            t = Timer(timeout, timeout_cb, [flag])
            t.start()

            while flag.is_set():
                raise_on_global_end()

                if self.passkey.data:
                    t.cancel()
                    break

        return self.passkey.data

    def gap_wait_for_pairing_fail(self, timeout=5):
        if self.pairing_failed_rcvd.data is None:
            flag = Event()
            flag.set()

            t = Timer(timeout, timeout_cb, [flag])
            t.start()

            while flag.is_set():
                raise_on_global_end()

                if self.pairing_failed_rcvd.data:
                    t.cancel()
                    break

        return self.pairing_failed_rcvd.data

    def gap_wait_for_lost_bond(self, timeout=5):
        if self.bond_lost_ev_data.data is None:
            flag = Event()
            flag.set()

            t = Timer(timeout, timeout_cb, [flag])
            t.start()

            while flag.is_set():
                raise_on_global_end()

                if self.bond_lost_ev_data.data:
                    t.cancel()
                    break

        return self.bond_lost_ev_data.data

    def gap_wait_for_sec_lvl_change(self, level, timeout=5):
        if self.sec_level != level:
            flag = Event()
            flag.set()

            t = Timer(timeout, timeout_cb, [flag])
            t.start()

            while flag.is_set():
                raise_on_global_end()

                if self.sec_level == level:
                    t.cancel()
                    break

        return self.sec_level

    def gap_set_pair_user_interaction(self, user_interaction):
        self.pair_user_interaction = user_interaction


class Mesh:
    def __init__(self, uuid, uuid_lt2=None):

        # init data
        self.dev_uuid = uuid
        self.dev_uuid_lt2 = uuid_lt2
        self.static_auth = None
        self.output_size = 0
        self.output_actions = None
        self.input_size = 0
        self.input_actions = None
        self.crpl_size = 0
        self.auth_metod = 0

        self.oob_action = Property(None)
        self.oob_data = Property(None)
        self.is_provisioned = Property(False)
        self.is_initialized = False
        self.last_seen_prov_link_state = Property(None)
        self.prov_invalid_bearer_rcv = Property(False)
        self.blob_lost_target = False

        # network data
        self.lt1_addr = 0x0001

        # provision node data
        self.net_key = '0123456789abcdef0123456789abcdef'
        self.net_key_idx = 0x0000
        self.flags = 0x00
        self.iv_idx = 0x00000000
        self.seq_num = 0x00000000
        self.address_iut = 0x0003
        self.dev_key = '0123456789abcdef0123456789abcdef'
        self.iut_is_provisioner = False
        self.pub_key = Property(None)
        self.priv_key = Property(None)

        # health model data
        self.health_test_id = Property(0x00)
        self.health_current_faults = Property(None)
        self.health_registered_faults = Property(None)

        # vendor model data
        self.vendor_model_id = '0002'

        # IV update
        self.iv_update_timeout = Property(120)
        self.is_iv_test_mode_enabled = Property(False)
        self.iv_test_mode_autoinit = False

        # Network
        # net_recv_ev_store - store data for further verification
        self.net_recv_ev_store = Property(False)
        # net_recv_ev_data (ttl, ctl, src, dst, payload)
        self.net_recv_ev_data = Property(None)
        # model_recv_ev_store - store data for further verification
        self.model_recv_ev_store = Property(False)
        # model_recv_ev_data (src, dst, payload)
        self.model_recv_ev_data = Property(None)
        self.incomp_timer_exp = Property(False)
        self.friendship = Property(False)
        self.lpn = Property(False)

        # Lower tester composition data
        self.tester_comp_data = Property({})

        # LPN
        self.lpn_subscriptions = []

        # Node Identity
        self.proxy_identity = False

        # Config Client
        self.net_idx = 0x0000
        self.address_lt1 = 0x0001
        self.address_lt2 = None
        self.net_key_index = 0x0000
        self.el_address = 0x0001
        self.status = Property(None)
        self.model_data = Property(None)
        self.app_idx = 0x0000
        self.provisioning_in_progress = Property(None)
        self.nodes_added = Property({})
        self.nodes_expected = Property([])

        # SAR
        self.sar_transmitter_state = Property((0x01, 0x07, 0x01, 0x07, 0x01, 0x02, 0x03))
        self.sar_receiver_state = Property((0x04, 0x02, 0x01, 0x01, 0x01))

        # Large Composition Data models
        self.large_comp_data = Property(None)
        self.models_metadata = Property(None)

        # MMDL Blob transfer timeout
        self.timeout = 0

        # MMDL Blob transfer TTL
        self.transfer_ttl = 2

        # MMDL Blob transfer server rxed data size
        self.blob_rxed_bytes = 0

        # MMDL expected status data
        self.expect_status_data = Property({
            "Ack": True,
            'Status': [],
            'Remaining Time': 0,
        })

        # MMDL received status data
        self.recv_status_data = Property({
            "Ack": True,
            'Status': [],
            'Remaining Time': 0,
        })

    def get_dev_uuid(self):
        return self.dev_uuid

    def get_dev_uuid_lt2(self):
        return self.dev_uuid_lt2

    def reset_state(self):
        '''Used to set MESH status to uninitialised. It's used after
        IUT restart when mesh was set to initialised before it'''
        self.is_initialized = False

    def set_prov_data(self, oob, output_size, output_actions, input_size,
                      input_actions, crpl_size, auth_method):
        self.static_auth = oob
        self.output_size = output_size
        self.output_actions = output_actions
        self.input_size = input_size
        self.input_actions = input_actions
        self.crpl_size = crpl_size
        self.auth_metod = auth_method

    def node_added(self, net_idx, addr, uuid, num_elems):
        self.nodes_added.data[uuid] = (net_idx, addr, uuid, num_elems)

    def expect_node(self, uuid):
        self.nodes_expected.data.append(uuid)

    def wait_for_node_added_uuid(self, timeout, uuid):
        if uuid in self.nodes_added.data:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if uuid in self.nodes_added.data:
                t.cancel()
                return True

        return False

    def wait_for_model_added_op(self, timeout, op):
        if self.model_recv_ev_data.data is not None and \
               self.model_recv_ev_data.data[2][0:4] == op:
            self.model_recv_ev_data.data = (0, 0, b'')
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            if self.model_recv_ev_data.data is not None and \
                    self.model_recv_ev_data.data[2][0:4] == op:
                t.cancel()
                self.model_recv_ev_data.data = (0, 0, b'')
                return True

        return False

    def set_iut_provisioner(self, _is_prov):
        self.iut_is_provisioner = _is_prov

    def set_iut_addr(self, _addr):
        self.address_iut = _addr

    def timeout_set(self, timeout):
        self.timeout = timeout

    def timeout_get(self):
        return self.timeout

    def transfer_ttl_set(self, ttl):
        self.transfer_ttl = ttl

    def transfer_ttl_get(self):
        return self.transfer_ttl

    def set_tester_comp_data(self, page, comp):
        self.tester_comp_data.data[page] = comp

    def get_tester_comp_data(self, page):
        if page in self.tester_comp_data.data:
            return self.tester_comp_data.data[page]

    def recv_status_data_set(self, key, data):
        if key in self.recv_status_data.data:
            self.recv_status_data.data[key] = data
        else:
            logging.error("%s %s not in store data",
                          self.recv_status_data_set.__name__, key)

    def recv_status_data_get(self, key):
        if key in self.recv_status_data.data:
            return self.recv_status_data.data[key]
        logging.error("%s %s not in store data",
                      self.recv_status_data_get.__name__, key)
        return False

    def expect_status_data_set(self, key, data):
        if key in self.expect_status_data.data:
            self.expect_status_data.data[key] = data
        else:
            logging.error("%s %s not in store data",
                          self.expect_status_data_set.__name__, key)

    def expect_status_data_get(self, key):
        if key in self.expect_status_data.data:
            return self.expect_status_data.data[key]
        logging.error("%s %s not in store data",
                      self.expect_status_data_get.__name__, key)
        return False

    def proxy_identity_enable(self):
        self.proxy_identity = True

    def wait_for_incomp_timer_exp(self, timeout):
        if self.incomp_timer_exp.data:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.incomp_timer_exp.data:
                t.cancel()
                return True

        return False

    def wait_for_prov_link_close(self, timeout):
        if not self.last_seen_prov_link_state.data:
            self.last_seen_prov_link_state.data = ('uninitialized', None)

        state, _ = self.last_seen_prov_link_state.data
        if state == 'closed':
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            state, _ = self.last_seen_prov_link_state.data
            if state == 'closed':
                t.cancel()
                return True

        return False

    def wait_for_lpn_established(self, timeout):
        if self.lpn.data:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.lpn.data:
                t.cancel()
                return True

        return False

    def wait_for_lpn_terminated(self, timeout):
        if not self.lpn.data:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if not self.lpn.data:
                t.cancel()
                return True

        return False

    def wait_for_blob_target_lost(self, timeout):
        if self.blob_lost_target:
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            if self.blob_lost_target:
                t.cancel()
                return True

        return False

    def pub_key_set(self, pub_key):
        self.pub_key.data = pub_key

    def pub_key_get(self):
        return self.pub_key.data

    def priv_key_set(self, priv_key):
        self.priv_key.data = priv_key

    def priv_key_get(self):
        return self.priv_key.data


class VCP:
    def __init__(self):
        self.wid_counter = 0
        self.event_queues = {
            defs.VCP_DISCOVERED_EV: [],
            defs.VCP_STATE_EV: [],
            defs.VCP_FLAGS_EV: [],
            defs.VCP_PROCEDURE_EV: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_discovery_completed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VCP_DISCOVERED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_vcp_state_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VCP_STATE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_vcp_flags_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VCP_FLAGS_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_vcp_procedure_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VCP_PROCEDURE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class VCS:
    pass


class VOCS:
    def __init__(self):
        self.event_queues = {
            defs.VOCS_OFFSET_EV: [],
            defs.VOCS_AUDIO_LOC_EV: [],
            defs.VOCS_PROCEDURE_EV: []
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_vocs_state_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VOCS_OFFSET_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_vocs_location_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VOCS_AUDIO_LOC_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_vocs_procedure_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.VOCS_PROCEDURE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class AICS:
    def __init__(self):
        self.event_queues = {
            defs.AICS_STATE_EV: [],
            defs.AICS_GAIN_SETTING_PROP_EV: [],
            defs.AICS_INPUT_TYPE_EV: [],
            defs.AICS_STATUS_EV: [],
            defs.AICS_DESCRIPTION_EV: [],
            defs.AICS_PROCEDURE_EV: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_aics_state_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_STATE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_aics_gain_setting_prop_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_GAIN_SETTING_PROP_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_aics_input_type_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_INPUT_TYPE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_aics_status_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_STATUS_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_aics_description_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_DESCRIPTION_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_aics_procedure_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.AICS_PROCEDURE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class PACS:
    def __init__(self):
        self.event_queues = {
            defs.PACS_EV_CHARACTERISTIC_SUBSCRIBED: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_pacs_characteristic_subscribed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.PACS_EV_CHARACTERISTIC_SUBSCRIBED],
            lambda _addr_type, _addr, *_: (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class MICP:
    def __init__(self):
        self.event_queues = {
            defs.MICP_DISCOVERED_EV: [],
            defs.MICP_MUTE_STATE_EV: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_discovery_completed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MICP_DISCOVERED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_mute_state_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MICP_MUTE_STATE_EV],
            lambda _addr_type, _addr, *_:
                (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class MICS:
    def __init__(self):
        self.mute_state = None
        self.event_queues = {
            defs.MICS_MUTE_STATE_EV: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_mute_state_ev(self, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MICS_MUTE_STATE_EV],
            lambda *_: True, timeout, remove)


class MCP:
    def __init__(self):
        self.event_queues = {
            defs.MCP_DISCOVERED_EV: [],
            defs.MCP_TRACK_DURATION_EV: [],
            defs.MCP_TRACK_POSITION_EV: [],
            defs.MCP_PLAYBACK_SPEED_EV: [],
            defs.MCP_SEEKING_SPEED_EV: [],
            defs.MCP_ICON_OBJ_ID_EV: [],
            defs.MCP_NEXT_TRACK_OBJ_ID_EV: [],
            defs.MCP_PARENT_GROUP_OBJ_ID_EV: [],
            defs.MCP_CURRENT_GROUP_OBJ_ID_EV: [],
            defs.MCP_PLAYING_ORDER_EV: [],
            defs.MCP_PLAYING_ORDERS_SUPPORTED_EV: [],
            defs.MCP_MEDIA_STATE_EV: [],
            defs.MCP_OPCODES_SUPPORTED_EV: [],
            defs.MCP_CONTENT_CONTROL_ID_EV: [],
            defs.MCP_SEGMENTS_OBJ_ID_EV: [],
            defs.MCP_CURRENT_TRACK_OBJ_ID_EV: [],
            defs.MCP_COMMAND_EV: [],
            defs.MCP_SEARCH_EV: [],
            defs.MCP_CMD_NTF_EV: [],
            defs.MCP_SEARCH_NTF_EV: []
        }
        self.error_opcodes = []

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_discovery_completed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_DISCOVERED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_track_duration_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_TRACK_DURATION_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_track_position_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_TRACK_POSITION_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_playback_speed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_PLAYBACK_SPEED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_seeking_speed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_SEEKING_SPEED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_icon_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_ICON_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_next_track_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_NEXT_TRACK_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_parent_group_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_PARENT_GROUP_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_current_group_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_CURRENT_GROUP_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_playing_order_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_PLAYING_ORDER_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_playing_orders_supported_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_PLAYING_ORDERS_SUPPORTED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_media_state_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_MEDIA_STATE_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_opcodes_supported_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_OPCODES_SUPPORTED_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_content_control_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_CONTENT_CONTROL_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_segments_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_SEGMENTS_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_current_track_obj_id_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_CURRENT_TRACK_OBJ_ID_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_control_point_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_COMMAND_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_search_control_point_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_SEARCH_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_cmd_notification_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_CMD_NTF_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_search_notification_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.MCP_SEARCH_NTF_EV],
            lambda _addr_type, _addr, *_:
            (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)


class GMCS:
    def __init__(self):
        self.track_obj_id = None
        self.event_queues = {}

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)


class ASCS:
    def __init__(self):
        self.event_queues = {
            defs.ASCS_EV_OPERATION_COMPLETED: [],
            defs.ASCS_EV_CHARACTERISTIC_SUBSCRIBED: [],
            defs.ASCS_EV_ASE_STATE_CHANGED: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_ascs_operation_complete_ev(self, addr_type, addr, ase_id, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.ASCS_EV_OPERATION_COMPLETED],
            lambda _addr_type, _addr, _ase_id, *_: (addr_type, addr, ase_id) == (_addr_type, _addr, _ase_id),
            timeout, remove)

    def wait_ascs_characteristic_subscribed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.ASCS_EV_CHARACTERISTIC_SUBSCRIBED],
            lambda _addr_type, _addr, *_: (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_ascs_ase_state_changed_ev(self, addr_type, addr, ase_id, state, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.ASCS_EV_ASE_STATE_CHANGED],
            lambda _addr_type, _addr, _ase_id, _state, *_:
            (addr_type, addr, ase_id, state) == (_addr_type, _addr, _ase_id, _state),
            timeout, remove)


class CORE:
    def __init__(self):
        self.event_queues = {
            defs.CORE_EV_IUT_READY: [],
        }

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_iut_ready_ev(self, timeout, remove=True):
        return wait_for_event_iut(
            self.event_queues[defs.CORE_EV_IUT_READY],
            timeout, remove)

    def cleanup(self):
        for key in self.event_queues:
            if key == defs.CORE_EV_IUT_READY:
                # To pass IUT ready event between test cases
                continue

            self.event_queues[key].clear()


class BAP:
    def __init__(self):
        self.broadcast_id = 0x1000000  # Invalid Broadcast ID
        self.broadcast_code = ''
        self.event_queues = {
            defs.BAP_EV_DISCOVERY_COMPLETED: [],
            defs.BAP_EV_CODEC_CAP_FOUND: [],
            defs.BAP_EV_ASE_FOUND: [],
            defs.BAP_EV_STREAM_RECEIVED: [],
            defs.BAP_EV_BAA_FOUND: [],
            defs.BAP_EV_BIS_FOUND: [],
            defs.BAP_EV_BIS_SYNCED: [],
            defs.BAP_EV_BIS_STREAM_RECEIVED: [],
            defs.BAP_EV_SCAN_DELEGATOR_FOUND: [],
            defs.BAP_EV_BROADCAST_RECEIVE_STATE: [],
            defs.BAP_EV_PA_SYNC_REQ: [],
        }

    def set_broadcast_code(self, broadcast_code):
        self.broadcast_code = broadcast_code

    def event_received(self, event_type, event_data_tuple):
        self.event_queues[event_type].append(event_data_tuple)

    def wait_codec_cap_found_ev(self, addr_type, addr, pac_dir, timeout, remove=False):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_CODEC_CAP_FOUND],
            lambda _addr_type, _addr, _pac_dir, *_:
                (addr_type, addr, pac_dir) == (_addr_type, _addr, _pac_dir),
            timeout, remove)

    def wait_discovery_completed_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_DISCOVERY_COMPLETED],
            lambda _addr_type, _addr, *_:
                (addr_type, addr) == (_addr_type, _addr),
            timeout, remove)

    def wait_ase_found_ev(self, addr_type, addr, ase_dir, timeout, remove=False):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_ASE_FOUND],
            lambda _addr_type, _addr, _ase_dir, *_:
                (addr_type, addr, ase_dir) == (_addr_type, _addr, _ase_dir),
            timeout, remove)

    def wait_stream_received_ev(self, addr_type, addr, ase_id, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_STREAM_RECEIVED],
            lambda _addr_type, _addr, _ase_id, *_:
                (addr_type, addr, ase_id) == (_addr_type, _addr, _ase_id),
            timeout, remove)

    def wait_baa_found_ev(self, addr_type, addr, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_BAA_FOUND],
            lambda ev: (addr_type, addr) == (ev['addr_type'], ev['addr']),
            timeout, remove)

    def wait_bis_found_ev(self, broadcast_id, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_BIS_FOUND],
            lambda ev: broadcast_id == ev['broadcast_id'],
            timeout, remove)

    def wait_bis_synced_ev(self, broadcast_id, bis_id, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_BIS_SYNCED],
            lambda ev: (broadcast_id, bis_id) == (ev['broadcast_id'], ev['bis_id']),
            timeout, remove)

    def wait_bis_stream_received_ev(self, broadcast_id, bis_id, timeout, remove=True):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_BIS_STREAM_RECEIVED],
            lambda ev: (broadcast_id, bis_id) == (ev['broadcast_id'], ev['bis_id']),
            timeout, remove)

    def wait_scan_delegator_found_ev(self, addr_type, addr, timeout, remove=False):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_SCAN_DELEGATOR_FOUND],
            lambda ev: (addr_type, addr) == (ev["addr_type"], ev["addr"]),
            timeout, remove)

    def wait_broadcast_receive_state_ev(self, broadcast_id, peer_addr_type, peer_addr,
                                        broadcaster_addr_type, broadcaster_addr,
                                        pa_sync_state, timeout, remove=False):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_BROADCAST_RECEIVE_STATE],
            lambda ev: (broadcast_id, peer_addr_type, peer_addr,
                        broadcaster_addr_type, broadcaster_addr,
                        pa_sync_state) ==
                       (ev["broadcast_id"], ev["addr_type"], ev["addr"],
                        ev["broadcaster_addr_type"], ev["broadcaster_addr"],
                        ev['pa_sync_state']),
            timeout, remove)

    def wait_pa_sync_req_ev(self, addr_type, addr, timeout, remove=False):
        return wait_event_with_condition(
            self.event_queues[defs.BAP_EV_PA_SYNC_REQ],
            lambda ev: (addr_type, addr) == (ev["addr_type"], ev["addr"]),
            timeout, remove)


class CCP:
    def __init__(self):
        self.events = {
            defs.CCP_EV_DISCOVERED:  { 'count': 0, 'status': 0, 'tbs_count': 0, 'gtbs': False },
            defs.CCP_EV_CALL_STATES: { 'count': 0, 'status': 0, 'index': 0, 'call_count': 0, 'states': [] }
        }

    def event_received(self, event_type, event_dict):
        count = self.events[event_type]['count']
        self.events[event_type] = copy.deepcopy(event_dict)
        self.events[event_type]['count'] = count+1


class L2capChan:
    def __init__(self, chan_id, psm, peer_mtu, peer_mps, our_mtu, our_mps,
                 bd_addr_type, bd_addr):
        self.id = chan_id
        self.psm = psm
        self.peer_mtu = peer_mtu
        self.peer_mps = peer_mps
        self.our_mtu = our_mtu
        self.our_mps = our_mps
        self.peer_bd_addr_type = bd_addr_type
        self.peer_bd_addr = bd_addr
        self.disconn_reason = None
        self.data_tx = []
        self.data_rx = []
        self.state = "init"  # "connected" / "disconnected"

    def _get_state(self, timeout):
        if self.state and self.state != "init":
            return self.state

        #  In case of self initiated connection, wait a while
        #  for connected/disconnected event
        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.state and self.state != "init":
                t.cancel()
                break

        return self.state

    def is_connected(self, timeout):
        state = self._get_state(timeout)
        if state == "connected":
            return True
        return False

    def connected(self, psm, peer_mtu, peer_mps, our_mtu, our_mps,
                  bd_addr_type, bd_addr):
        self.psm = psm
        self.peer_mtu = peer_mtu
        self.peer_mps = peer_mps
        self.our_mtu = our_mtu
        self.our_mps = our_mps
        self.peer_bd_addr_type = bd_addr_type
        self.peer_bd_addr = bd_addr
        self.state = "connected"

    def disconnected(self, psm, bd_addr_type, bd_addr, reason):
        self.psm = None
        self.peer_bd_addr_type = None
        self.peer_bd_addr = None
        self.disconn_reason = reason
        self.state = "disconnected"

    def rx(self, data):
        self.data_rx.append(data)

    def tx(self, data):
        self.data_tx.append(data)

    def rx_data_get(self, timeout):
        if len(self.data_rx) != 0:
            return self.data_rx

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if len(self.data_rx) != 0:
                t.cancel()
                return self.data_rx

        return None

    def tx_data_get(self):
        return self.data_tx


class L2cap:
    connection_success = 0x0000
    unknown_le_psm = 0x0002
    no_resources = 0x0004
    insufficient_authen = 0x0005
    insufficient_author = 0x0006
    insufficient_key_sz = 0x0007
    insufficient_enc = 0x0008
    invalid_source_cid = 0x0009
    source_cid_already_used = 0x000a
    unacceptable_parameters = 0x000b
    invalid_parameters = 0x000c

    def __init__(self, psm, initial_mtu):
        # PSM used for testing for Client role
        self.psm = psm
        self.initial_mtu = initial_mtu
        self.channels = []
        self.hold_credits = 0
        self.num_channels = 2

    def chan_lookup_id(self, chan_id):
        for chan in self.channels:
            if chan.id == chan_id:
                return chan
        return None

    def clear_data(self):
        for chan in self.channels:
            chan.data_tx = []
            chan.data_rx = []

    def reconfigured(self, chan_id, peer_mtu, peer_mps, our_mtu, our_mps):
        channel = self.chan_lookup_id(chan_id)
        channel.peer_mtu = peer_mtu
        channel.peer_mps = peer_mps
        channel.our_mtu = our_mtu
        channel.our_mps = our_mps

    def psm_set(self, psm):
        self.psm = psm

    def num_channels_set(self, num_channels):
        self.num_channels = num_channels

    def hold_credits_set(self, hold_credits):
        self.hold_credits = hold_credits

    def initial_mtu_set(self, initial_mtu):
        self.initial_mtu = initial_mtu

    def connected(self, chan_id, psm, peer_mtu, peer_mps, our_mtu, our_mps,
                  bd_addr_type, bd_addr):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            chan = L2capChan(chan_id, psm, peer_mtu, peer_mps, our_mtu, our_mps,
                             bd_addr_type, bd_addr)
            self.channels.append(chan)

        chan.connected(psm, peer_mtu, peer_mps, our_mtu, our_mps,
                       bd_addr_type, bd_addr)

    def disconnected(self, chan_id, psm, bd_addr_type, bd_addr, reason):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            logging.error("unknown channel")
            return
        # Remove channel from saved channels
        self.channels.remove(chan)

        chan.disconnected(psm, bd_addr_type, bd_addr, reason)

    def is_connected(self, chan_id):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            return False

        return chan.is_connected(10)

    def wait_for_disconnection(self, chan_id, timeout):
        if not self.is_connected(chan_id):
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if not self.is_connected(chan_id):
                t.cancel()
                return True

        return False

    def wait_for_connection(self, chan_id, timeout=5):
        if self.is_connected(chan_id):
            return True

        flag = Event()
        flag.set()

        t = Timer(timeout, timeout_cb, [flag])
        t.start()

        while flag.is_set():
            raise_on_global_end()

            if self.is_connected(chan_id):
                t.cancel()
                return True

        return False

    def rx(self, chan_id, data):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            logging.error("unknown channel")
            return

        chan.rx(data)

    def tx(self, chan_id, data):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            logging.error("unknown channel")
            return

        chan.tx(data)

    def rx_data_get(self, chan_id, timeout):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            logging.error("unknown channel")
            return None

        return chan.rx_data_get(timeout)

    def rx_data_get_all(self, timeout):
        data = []

        for chan in self.channels:
            data.append(chan.rx_data_get(timeout))

        return data

    def tx_data_get(self, chan_id):
        chan = self.chan_lookup_id(chan_id)
        if chan is None:
            logging.error("unknown channel")
            return None

        return chan.tx_data_get()

    def tx_data_get_all(self):
        data = []

        for chan in self.channels:
            data.append(chan.tx_data_get())

        return data


class SynchPoint:
    def __init__(self, test_case, wid, delay=None):
        self.test_case = test_case
        self.wid = wid
        self.delay = delay
        self.done = None

    def set_done(self):
        self.done.set(True)

    def clear(self):
        self.done.clear()

    def wait(self):
        self.done.wait()


class SynchElem:
    def __init__(self, sync_points):
        self.sync_points = sync_points
        self.active_synch_point = None
        count = len(sync_points)
        self._start_barrier = threading.Barrier(count, self.clear_flags)
        self._end_barrier = threading.Barrier(count, self.clear_flags)

    def find_matching(self, test_case, wid):
        matching_items = [item for item in self.sync_points if
                          item.test_case == test_case and item.wid == wid]
        if matching_items:
            return matching_items[0]
        return None

    def clear_flags(self):
        for point in self.sync_points:
            point.clear()

    def wait_for_start(self):
        # While debugging, do not step over Barrier.wait() or other
        # waits from threading module. This may cause the GIL deadlock.
        self._start_barrier.wait()
        if self._start_barrier.broken:
            return False
        return True

    def wait_for_end(self):
        self._end_barrier.wait()
        if self._end_barrier.broken:
            return False
        return True

    def wait_for_your_turn(self, synch_point):
        for point in self.sync_points:
            if point == synch_point:
                self.active_synch_point = synch_point
                return True

            point.wait()

            if self._start_barrier.broken or self._end_barrier.broken:
                return False

        return False

    def cancel_synch(self):
        self._end_barrier.abort()
        self._start_barrier.abort()

        for point in self.sync_points:
            point.set_done()


class Synch:
    def __init__(self):
        self._synch_table = []
        self._synch_condition = threading.Condition()

    def reinit(self):
        self._synch_table.clear()

    def add_synch_element(self, elem):
        for sync_point in elem:
            # If a test case has to be repeated, its SyncPoints will be reused.
            # Reinit done-flags to renew potentially broken locks.
            sync_point.done = ResultWithFlag()

        self._synch_table.append(SynchElem(elem))

    def wait_for_start(self, wid, tc_name):
        synch_point = None
        elem = None

        for i, elem in enumerate(self._synch_table):
            synch_point = elem.find_matching(tc_name, wid)
            if synch_point:
                # Found a sync point matching the test case and wid
                break

        if not synch_point:
            # No synch point found
            return None

        log(f'SYNCH: Waiting at barrier for start, tc {tc_name} wid {wid}')
        if not elem.wait_for_start():
            log(f'SYNCH: Cancelled waiting at barrier for start, tc {tc_name} wid {wid}')
            return None

        log(f'SYNCH: Waiting for turn to start, tc {tc_name} wid {wid}')

        if not elem.wait_for_your_turn(synch_point):
            log(f'SYNCH: Cancelled waiting for turn to start, tc {tc_name} wid {wid}')
            return None

        log(f'SYNCH: Started tc {tc_name} wid {wid}')

        return elem

    def wait_for_end(self, synch_elem):
        synch_point = synch_elem.active_synch_point

        if synch_point.delay:
            sleep(synch_point.delay)

        # Let other LT-threads know that this one completed the wid
        synch_point.set_done()
        tc_name = synch_point.test_case
        wid = synch_point.wid

        log(f'SYNCH: Waiting at end barrier, tc {tc_name} wid {wid}')
        if not synch_elem.wait_for_end():
            log(f'SYNCH: Cancelled waiting at end barrier, tc {tc_name} wid {wid}')
            return None

        log(f'SYNCH: Finished waiting at end barrier, tc {tc_name} wid {wid}')

        # Remove the synch element
        try:
            self._synch_table.remove(synch_elem)
        except:
            # Already cleaned up by other thread
            pass

        return None

    def cancel_synch(self):
        for elem in self._synch_table:
            elem.cancel_synch()
        self._synch_table = []


class Gatt:
    def __init__(self):
        self.server_db = GattDB()
        self.last_unique_uuid = 0
        self.verify_values = []
        self.notification_events = []
        self.notification_ev_received = Event()
        self.signed_write_handle = 0

    def attr_value_set(self, handle, value):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr:
            attr.value = value
            return

        attr = GattCharacteristicDescriptor(handle, None, None, None, value)
        self.server_db.attr_add(handle, attr)

    def attr_value_get(self, handle):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr:
            return attr.value

        return None

    def attr_value_set_changed(self, handle):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr is None:
            logging.error("No attribute with %r handle", handle)
            return

        attr.has_changed_cnt += 1
        attr.has_changed.set()

    def attr_value_clr_changed(self, handle):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr is None:
            logging.error("No attribute with %r handle", handle)
            return

        attr.has_changed_cnt = 0
        attr.has_changed.clear()

    def attr_value_get_changed_cnt(self, handle):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr is None:
            logging.error("No attribute with %r handle", handle)
            return 0

        return attr.has_changed_cnt

    def wait_attr_value_changed(self, handle, timeout=None):
        attr = self.server_db.attr_lookup_handle(handle)
        if attr is None:
            attr = GattCharacteristicDescriptor(handle, None, None, None, None)
            self.server_db.attr_add(handle, attr)

        if attr.has_changed.wait(timeout=timeout):
            return attr.value

        logging.debug("timed out")
        return None

    def notification_ev_recv(self, addr_type, addr, notif_type, handle, data):
        self.notification_events.append((addr_type, addr, notif_type, handle, data))
        self.notification_ev_received.set()

    def wait_notification_ev(self, timeout=None):
        self.notification_ev_received.wait(timeout)
        self.notification_ev_received.clear()


def wait_for_event(timeout, test, args=None):
    if test(args):
        return True

    flag = Event()
    flag.set()

    t = Timer(timeout, timeout_cb, [flag])
    t.start()

    while flag.is_set():
        raise_on_global_end()

        if test(args):
            t.cancel()
            return True

    return False


def is_procedure_done(list, cnt):
    if cnt is None:
        return False

    if cnt <= 0:
        return True

    return len(list) == cnt


class IAS:
    ALERT_LEVEL_NONE = 0
    ALERT_LEVEL_MILD = 1
    ALERT_LEVEL_HIGH = 2

    def __init__(self):
        self.alert_lvl = None

    def is_mild_alert_set(self, args):
        return self.alert_lvl == self.ALERT_LEVEL_MILD

    def is_high_alert_set(self, args):
        return self.alert_lvl == self.ALERT_LEVEL_HIGH

    def is_alert_stopped(self, args):
        return self.alert_lvl == self.ALERT_LEVEL_NONE

    def wait_for_mild_alert(self, timeout=30):
        return wait_for_event(timeout, self.is_mild_alert_set)

    def wait_for_high_alert(self, timeout=30):
        return wait_for_event(timeout, self.is_high_alert_set)

    def wait_for_stop_alert(self, timeout=30):
        return wait_for_event(timeout, self.is_alert_stopped)


class GattCl:
    def __init__(self):
        # if MTU exchanged tuple (addr, addr_type, status)
        self.mtu_exchanged = Property(None)
        self.verify_values = []
        self.prim_svcs_cnt = None
        self.prim_svcs = []
        self.incl_svcs_cnt = None
        self.incl_svcs = []
        self.chrcs_cnt = None
        self.chrcs = []
        self.dscs_cnt = None
        self.dscs = []
        self.notifications = []
        self.write_status = None
        self.event_to_await = None

    def set_event_to_await(self, event):
        self.event_to_await = event

    def wait_for_rsp_event(self, timeout=30):
        return wait_for_event(timeout, self.event_to_await)

    def is_mtu_exchanged(self, args):
        return self.mtu_exchanged.data

    def wait_for_mtu_exchange(self, timeout=30):
        return wait_for_event(timeout, self.is_mtu_exchanged)

    def is_prim_disc_complete(self, args):
        return is_procedure_done(self.prim_svcs, self.prim_svcs_cnt)

    def wait_for_prim_svcs(self, timeout=30):
        return wait_for_event(timeout, self.is_prim_disc_complete)

    def is_incl_disc_complete(self, args):
        return is_procedure_done(self.incl_svcs, self.incl_svcs_cnt)

    def wait_for_incl_svcs(self, timeout=30):
        return wait_for_event(timeout, self.is_incl_disc_complete)

    def is_chrcs_disc_complete(self, args):
        return is_procedure_done(self.chrcs, self.chrcs_cnt)

    def wait_for_chrcs(self, timeout=30):
        return wait_for_event(timeout, self.is_chrcs_disc_complete)

    def is_dscs_disc_complete(self, args):
        return is_procedure_done(self.dscs, self.dscs_cnt)

    def wait_for_descs(self, timeout=30):
        return wait_for_event(timeout, self.is_dscs_disc_complete)

    def is_read_complete(self, args):
        return self.verify_values != []

    def wait_for_read(self, timeout=30):
        return wait_for_event(timeout, self.is_read_complete)

    def is_notification_rxed(self, expected_count):
        if expected_count > 0:
            return len(self.notifications) == expected_count
        return len(self.notifications) > 0

    def wait_for_notifications(self, timeout=30, expected_count=0):
        return wait_for_event(timeout,
                              self.is_notification_rxed, expected_count)

    def is_write_completed(self, args):
        return self.write_status is not None

    def wait_for_write_rsp(self, timeout=30):
        return wait_for_event(timeout, self.is_write_completed)


class Stack:
    def __init__(self):
        self.gap = None
        self.mesh = None
        self.l2cap = None
        self.synch = None
        self.gatt = None
        self.gatt_cl = None
        self.vcs = None
        self.ias = None
        self.vocs = None
        self.aics = None
        self.pacs = None
        self.ascs = None
        self.bap = None
        self.core = None
        self.micp = None
        self.mics = None
        self.ccp = None
        self.vcp = None
        self.mcp = None
        self.gmcs = None

        self.supported_svcs = 0

    def is_svc_supported(self, svc):
        # these are in little endian
        services = {
            "CORE": 1 << defs.BTP_SERVICE_ID_CORE,
            "GAP": 1 << defs.BTP_SERVICE_ID_GAP,
            "GATT": 1 << defs.BTP_SERVICE_ID_GATT,
            "L2CAP": 1 << defs.BTP_SERVICE_ID_L2CAP,
            "MESH": 1 << defs.BTP_SERVICE_ID_MESH,
            "MESH_MMDL": 1 << defs.BTP_SERVICE_ID_MMDL,
            "GATT_CL": 1 << defs.BTP_SERVICE_ID_GATTC,
            "VCS": 1 << defs.BTP_SERVICE_ID_VCS,
            "IAS": 1 << defs.BTP_SERVICE_ID_IAS,
            "AICS": 1 << defs.BTP_SERVICE_ID_AICS,
            "VOCS": 1 << defs.BTP_SERVICE_ID_VOCS,
            "PACS": 1 << defs.BTP_SERVICE_ID_PACS,
            "ASCS": 1 << defs.BTP_SERVICE_ID_ASCS,
            "BAP": 1 << defs.BTP_SERVICE_ID_BAP,
            "MICP": 1 << defs.BTP_SERVICE_ID_MICP,
            "HAS": 1 << defs.BTP_SERVICE_ID_HAS,
            "CSIS": 1 << defs.BTP_SERVICE_ID_CSIS,
            "MICS": 1 << defs.BTP_SERVICE_ID_MICS,
            "CCP": 1 << defs.BTP_SERVICE_ID_CCP,
            "VCP": 1 << defs.BTP_SERVICE_ID_VCP,
            "MCP": 1 << defs.BTP_SERVICE_ID_MCP,
            "GMCS": 1 << defs.BTP_SERVICE_ID_GMCS,
        }
        return self.supported_svcs & services[svc] > 0

    def gap_init(self, name=None, manufacturer_data=None, appearance=None,
                 svc_data=None, flags=None, svcs=None, uri=None, periodic_data=None,
                 le_supp_feat=None):
        self.gap = Gap(name, manufacturer_data, appearance, svc_data, flags,
                       svcs, uri, periodic_data, le_supp_feat)

    def mesh_init(self, uuid, uuid_lt2=None):
        if self.mesh:
            return

        self.mesh = Mesh(uuid, uuid_lt2)

    def l2cap_init(self, psm, initial_mtu):
        self.l2cap = L2cap(psm, initial_mtu)

    def gatt_init(self):
        self.gatt = Gatt()
        self.gatt_cl = self.gatt

    def vcs_init(self):
        self.vcs = VCS()

    def aics_init(self):
        self.aics = AICS()

    def vocs_init(self):
        self.vocs = VOCS()

    def ias_init(self):
        self.ias = IAS()

    def pacs_init(self):
        self.pacs = PACS()

    def ascs_init(self):
        self.ascs = ASCS()

    def bap_init(self):
        self.bap = BAP()

    def ccp_init(self):
        self.ccp = CCP()

    def core_init(self):
        if self.core:
            self.core.cleanup()
        else:
            self.core = CORE()

    def micp_init(self):
        self.micp = MICP()

    def mics_init(self):
        self.mics = MICS()

    def mcp_init(self):
        self.mcp = MCP()

    def gmcs_init(self):
        self.gmcs = GMCS()

    def gatt_cl_init(self):
        self.gatt_cl = GattCl()

    def synch_init(self):
        if not self.synch:
            self.synch = Synch()
        else:
            self.synch.reinit()

    def vcp_init(self):
        self.vcp = VCP()

    def cleanup(self):
        if self.gap:
            self.gap = Gap(self.gap.name, self.gap.manufacturer_data, None, None, None, None, None)

        if self.mesh:
            self.mesh = Mesh(self.mesh.get_dev_uuid(), self.mesh.get_dev_uuid_lt2())

        if self.vcs:
            self.vcs_init()

        if self.aics:
            self.aics_init()

        if self.vocs:
            self.vocs_init()

        if self.ias:
            self.ias_init()

        if self.pacs:
            self.pacs_init()

        if self.ascs:
            self.ascs_init()

        if self.bap:
            self.bap_init()

        if self.micp:
            self.micp_init()
            
        if self.ccp:
            self.ccp_init()

        if self.mics:
            self.mics_init()

        if self.gmcs:
            self.gmcs_init()

        if self.gatt:
            self.gatt_init()

        if self.gatt_cl:
            self.gatt_cl_init()

        if self.synch:
            self.synch.cancel_synch()

        if self.core:
            self.core_init()

        if self.vcp:
            self.vcp_init()

        if self.mcp:
            self.mcp_init()


def init_stack():
    global STACK

    STACK = Stack()


def cleanup_stack():
    global STACK

    STACK = None


def get_stack():
    return STACK
