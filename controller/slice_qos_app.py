# controller/slice_qos_app.py

"""
SliceQosApp Orchestrator (Architecture 2)

This is the ONLY RyuApp loaded by ryu-manager.

Responsibilities:
- Own shared controller state (datapaths registry, topology graph, port stats history).
- Handle OpenFlow events (StateChange, SwitchFeatures, PortStatus, PortStatsReply).
- Run a monitoring loop to periodically request stats and trigger QoS checks.
- Delegate slice-specific behavior to slice managers (plain Python modules).

Design goals:
- Keep behavior of the existing low-latency slice unchanged.
- Add a high-throughput slice with widest-path routing and VIP NAT, without interfering
  with the low-latency slice.
"""

from __future__ import annotations

import time
import logging

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib import hub

from slice_config import SLICES
from graph_state import TopologyGraph
from flow_utils import add_flow

from link_config import LINK_PARAMS, PORT_TO_NEIGHBOR, NEXT_HOP_PORT
from latency_slice import LatencySliceManager
from throughput_slice import ThroughputSliceManager


# -------------------------------------------------------------------
# Feature flags (debug-friendly)
# -------------------------------------------------------------------
ENABLE_LATENCY_SLICE = False
ENABLE_THROUGHPUT_SLICE = True


# -------------------------------------------------------------------
# Flow priorities (OpenFlow)
# -------------------------------------------------------------------
# Higher number => higher priority.
#
# Policy:
#  - low-latency slice must win over high-throughput slice
#  - best-effort remains below both (table-miss, L2, etc.)
SLICE_FLOW_PRIORITY_LATENCY = 20
SLICE_FLOW_PRIORITY_THROUGHPUT = 10


class SliceQosApp(app_manager.RyuApp):
    """
    SliceQosApp orchestrator.

    Key idea (Architecture 2):
      - Do NOT use Ryu topology discovery (--observe-links, LLDP).
      - Treat topology as known a priori via LINK_PARAMS.
      - Consider topology "ready" when all expected switches have connected.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SliceQosApp, self).__init__(*args, **kwargs)

        logging.getLogger().setLevel(logging.WARNING)
        self.logger.setLevel(logging.INFO)

        # Shared controller state
        self.datapaths = {}
        self.topo = TopologyGraph()
        self.slices = SLICES

        self.port_stats_state = {}  # (dpid, port_no) -> {tx_bytes, time}

        self.expected_switches = self._derive_expected_switches()
        self.bootstrap_done = False

        self._init_logical_graph_from_config()

        # Slice managers
        self.latency_mgr = None
        if ENABLE_LATENCY_SLICE:
            assert "latency" in self.slices, "ENABLE_LATENCY_SLICE=True but SLICES['latency'] missing"
            self.latency_mgr = LatencySliceManager(
                topo=self.topo,
                datapaths=self.datapaths,
                slice_conf=self.slices["latency"],
                next_hop_port=NEXT_HOP_PORT,
                logger=self.logger,
                flow_priority=SLICE_FLOW_PRIORITY_LATENCY,
            )

        self.throughput_mgr = None
        if ENABLE_THROUGHPUT_SLICE:
            assert "throughput" in self.slices, "ENABLE_THROUGHPUT_SLICE=True but SLICES['throughput'] missing"
            self.throughput_mgr = ThroughputSliceManager(
                topo=self.topo,
                datapaths=self.datapaths,
                slice_conf=self.slices["throughput"],
                next_hop_port=NEXT_HOP_PORT,
                logger=self.logger,
                flow_priority=SLICE_FLOW_PRIORITY_THROUGHPUT,
            )

        # Monitor loop
        self.monitor_thread = hub.spawn(self._monitor)

        self.logger.info(
            "SliceQosApp orchestrator initialized (arch2). Expected switches: %s",
            sorted(self.expected_switches),
        )
        self.logger.info(
            "Enabled slices: latency=%s throughput=%s",
            ENABLE_LATENCY_SLICE, ENABLE_THROUGHPUT_SLICE,
        )
        self.logger.info(
            "Flow priorities: latency=%s throughput=%s",
            SLICE_FLOW_PRIORITY_LATENCY, SLICE_FLOW_PRIORITY_THROUGHPUT,
        )

    # ------------------------------------------------------------------
    # Bootstrap / expected topology
    # ------------------------------------------------------------------
    def _derive_expected_switches(self):
        s = set()
        for (u, v) in LINK_PARAMS.keys():
            s.add(u)
            s.add(v)

        if ENABLE_LATENCY_SLICE:
            conf = self.slices.get("latency")
            assert conf is not None, "Missing SLICES['latency']"
            s.add(conf["ingress_switch"])
            s.add(conf["egress_switch"])

        if ENABLE_THROUGHPUT_SLICE:
            conf = self.slices.get("throughput")
            assert conf is not None, "Missing SLICES['throughput']"
            s.add(conf["ingress_switch"])
            # We may target either remote or closest backend; require both edge switches.
            s.add(conf["remote_backend"]["egress_switch"])
            s.add(conf["closest_backend"]["egress_switch"])

        return s

    def _init_logical_graph_from_config(self):
        for (u, v), params in LINK_PARAMS.items():
            self.topo.add_link(u, v, delay_ms=params["delay_ms"], capacity_mbps=params["capacity_mbps"])

    def _bootstrap_if_ready(self):
        if self.bootstrap_done:
            return
        if not self.expected_switches:
            self.bootstrap_done = True
            return

        have = set(self.datapaths.keys())
        missing = self.expected_switches - have
        if missing:
            return

        self.logger.info("\nTopology READY (all expected datapaths registered). Running initial slice bootstrap.")

        if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
            self.latency_mgr.recompute_path(reason="bootstrap_all_switches_connected")

        if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
            self.throughput_mgr.recompute_path(reason="bootstrap_all_switches_connected")

        self.bootstrap_done = True

    # ------------------------------------------------------------------
    # Datapath state tracking
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change_handler(self, ev):
        dp = ev.datapath
        dpid = dp.id

        if ev.state == MAIN_DISPATCHER:
            if dpid not in self.datapaths:
                self.datapaths[dpid] = dp
                self.logger.info("Register datapath: %s", dpid)
                self._bootstrap_if_ready()

        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                self.logger.warning("Unregister datapath: %s", dpid)
                del self.datapaths[dpid]

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        dp = ev.msg.datapath
        ofproto = dp.ofproto
        parser = dp.ofproto_parser

        # Table-miss -> controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]

        add_flow(
            dp,
            priority=0,
            match=match,
            actions=actions,
            idle_timeout=0,
            hard_timeout=0,
            cookie=0,  # not ours, just a bootstrap rule
        )

    # ------------------------------------------------------------------
    # Link failure handling WITHOUT LLDP (OFPPortStatus)
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        port_no = msg.desc.port_no
        ofproto = dp.ofproto

        nbr = PORT_TO_NEIGHBOR.get((dpid, port_no))
        if nbr is None:
            return

        is_down = bool(msg.desc.state & ofproto.OFPPS_LINK_DOWN)

        # Determine whether slice paths are affected BEFORE applying topology updates.
        latency_affected = False
        if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
            p = self.slices["latency"].get("current_path")
            latency_affected = (
                self._edge_in_path((dpid, nbr), p) or self._edge_in_path((nbr, dpid), p)
            )

        throughput_affected = False
        if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
            p = self.slices["throughput"].get("current_path")
            throughput_affected = (
                self._edge_in_path((dpid, nbr), p) or self._edge_in_path((nbr, dpid), p)
            )

        if is_down:
            if (dpid, nbr) in LINK_PARAMS:
                self.topo.remove_link(dpid, nbr)
            if (nbr, dpid) in LINK_PARAMS:
                self.topo.remove_link(nbr, dpid)

            self.logger.warning("Link DOWN via port status: %s(port %s) <-> %s", dpid, port_no, nbr)

            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None and latency_affected:
                self.latency_mgr.on_path_affected(reason="port_down_on_current_path")

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None and throughput_affected:
                self.throughput_mgr.on_path_affected(reason="port_down_on_current_path")

        else:
            if (dpid, nbr) in LINK_PARAMS:
                p = LINK_PARAMS[(dpid, nbr)]
                self.topo.add_link(dpid, nbr, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])
            if (nbr, dpid) in LINK_PARAMS:
                p = LINK_PARAMS[(nbr, dpid)]
                self.topo.add_link(nbr, dpid, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])

            self.logger.info("Link UP via port status: %s(port %s) <-> %s", dpid, port_no, nbr)

            # Conservative Arch2: no reroute on UP.
            # Exception: if a slice is DISCONNECTED, trigger recompute to restore connectivity.
            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
                if self.slices["latency"].get("current_path") is None:
                    self.logger.info("Latency slice recovery trigger: link is UP and slice is DISCONNECTED. Recomputing.")
                    self.latency_mgr.recompute_path(reason="link_up_recovery")

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
                if self.slices["throughput"].get("current_path") is None:
                    self.logger.info("Throughput slice recovery trigger: link is UP and slice is DISCONNECTED. Recomputing.")
                    self.throughput_mgr.recompute_path(reason="link_up_recovery")

    def _edge_in_path(self, edge, path):
        if not path or len(path) < 2:
            return False
        return edge in list(zip(path[:-1], path[1:]))

    # ------------------------------------------------------------------
    # Monitor thread: PortStats + QoS checks
    # ------------------------------------------------------------------
    def _monitor(self):
        """
        Semi-passive monitoring:
          - Poll PortStats less frequently.
          - Poll ONLY switches that are relevant to currently-installed slice paths.
          - Trigger slice QoS checks after stats updates.
        """
        MONITOR_INTERVAL_S = 10

        while True:
            self._bootstrap_if_ready()

            critical_dpids = []

            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
                p = self.slices["latency"].get("current_path")
                if p:
                    critical_dpids.extend(p)

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
                p = self.slices["throughput"].get("current_path")
                if p:
                    critical_dpids.extend(p)

            critical_dpids = list(dict.fromkeys(critical_dpids))

            if not critical_dpids:
                hub.sleep(MONITOR_INTERVAL_S)
                continue

            for dpid in critical_dpids:
                dp = self.datapaths.get(dpid)
                if dp is None:
                    continue
                self._request_port_stats(dp)

            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
                self.latency_mgr.check_qos()

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
                self.throughput_mgr.check_qos()

            hub.sleep(MONITOR_INTERVAL_S)

    def _request_port_stats(self, datapath):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()

        for stat in ev.msg.body:
            port_no = stat.port_no
            tx_bytes = stat.tx_bytes

            key = (dpid, port_no)
            last = self.port_stats_state.get(key)

            if last is not None:
                delta_tx = tx_bytes - last["tx_bytes"]
                delta_t = now - last["time"]
                if delta_tx < 0:
                    continue
                if delta_t > 0:
                    used_mbps = (delta_tx * 8.0) / (delta_t * 1e6)

                    nbr = PORT_TO_NEIGHBOR.get((dpid, port_no))
                    if nbr is not None:
                        self.topo.update_link_usage(dpid, nbr, used_mbps)
                        m = self.topo.get_link_metrics(dpid, nbr)
                        self.logger.info(
                            "LINK %s->%s raw=%.3f ewma=%.3f cap=%.1f",
                            dpid, nbr, m["used_mbps_raw"], m["used_mbps_ewma"], m["capacity_mbps"]
                        )

            self.port_stats_state[key] = {"tx_bytes": tx_bytes, "time": now}
