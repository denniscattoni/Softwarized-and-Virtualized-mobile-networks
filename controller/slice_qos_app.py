#!/usr/bin/env python3
# controller/slice_qos_app.py

"""
SliceQosApp Orchestrator

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

Monitoring policy:
- Do NOT use LLDP / topology discovery.
- Topology is known a priori from LINK_PARAMS.
- PortStats polling implements a Dual-Tier Polling Scheduler:
    * Tier A (active): poll switches on the CURRENT active path(s) every cycle (priority).
    * Tier B (background): poll a small number of non-active switches in round-robin
      to avoid "frozen" EWMA metrics on links outside the active path(s).
"""

from __future__ import annotations

import time
import logging
from typing import List

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
# Feature flags
# -------------------------------------------------------------------
ENABLE_LATENCY_SLICE = True
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

    Key idea:
      - Do NOT use Ryu topology discovery (--observe-links, LLDP).
      - Treat topology as known a priori via LINK_PARAMS.
      - Consider topology "ready" when all expected switches have connected.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(SliceQosApp, self).__init__(*args, **kwargs)

        logging.getLogger().setLevel(logging.WARNING)
        self.logger.setLevel(logging.INFO)

        # ------------------------------------------------------------------
        # Shared controller state
        # ------------------------------------------------------------------
        self.datapaths = {}
        self.topo = TopologyGraph()
        self.slices = SLICES

        self.port_stats_state = {}  # (dpid, port_no) -> {tx_bytes, time}

        self.expected_switches = self._derive_expected_switches()
        self.bootstrap_done = False

        # Build the logical graph immediately (static topology)
        self._init_logical_graph_from_config()

        # ------------------------------------------------------------------
        # Dual-Tier Polling Scheduler state (single-thread)
        # ------------------------------------------------------------------
        # Background RR list is recomputed deterministically from expected_switches \ active_set.
        # We keep an index for round-robin selection across monitor cycles.
        self._bg_rr_list: List[int] = []
        self._bg_rr_idx: int = 0

        # ------------------------------------------------------------------
        # Structured logging / tick context (single-thread)
        # ------------------------------------------------------------------
        # Monotonic tick sequence number (for correlating async PortStats replies).
        self._tick_seq: int = 0

        # Last poll context per DPID (updated when a PortStatsRequest is SENT).
        # dpid -> { "tick": int, "tier": "active"|"rr", "label": str }
        # label is an operator-facing hint (e.g., "active_latency", "active_throughput", "rr").
        self._poll_ctx_by_dpid = {}

        # Snapshot of last-known active paths (for readable per-tick PATH_STATE).
        self._last_lat_path = None
        self._last_thr_path = None

        # ------------------------------------------------------------------
        # Slice managers (CREATE THEM BEFORE STARTING ANY THREADS)
        # This avoids races where _monitor() runs before managers exist.
        # ------------------------------------------------------------------
        self.latency_mgr = None
        self.throughput_mgr = None

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

        # ------------------------------------------------------------------
        # Start monitor thread LAST (after managers exist)
        # ------------------------------------------------------------------
        self.monitor_thread = hub.spawn(self._monitor)

        self.logger.info(
            "SLICE=orchestrator EV=INIT expected_switches=%s",
            sorted(self.expected_switches),
        )
        self.logger.info(
            "SLICE=orchestrator EV=FEATURE_FLAGS latency=%s throughput=%s",
            bool(ENABLE_LATENCY_SLICE),
            bool(ENABLE_THROUGHPUT_SLICE),
        )
        self.logger.info(
            "SLICE=orchestrator EV=FLOW_PRIORITY latency=%s throughput=%s",
            int(SLICE_FLOW_PRIORITY_LATENCY),
            int(SLICE_FLOW_PRIORITY_THROUGHPUT),
        )

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

        self.logger.info("SLICE=orchestrator EV=TOPO_READY expected_dpids=%s", sorted(self.expected_switches))

        if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
            self.latency_mgr.recompute_path(reason="bootstrap_all_switches_connected")

        if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
            self.throughput_mgr.recompute_path(reason="bootstrap_all_switches_connected")

        self.bootstrap_done = True

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
            s.add(conf["remote_backend"]["egress_switch"])
            s.add(conf["closest_backend"]["egress_switch"])

        return s

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
                self.logger.info("SLICE=orchestrator EV=DP_REGISTER dpid=%s", dpid)
                self._bootstrap_if_ready()

        elif ev.state == DEAD_DISPATCHER:
            if dpid in self.datapaths:
                self.logger.warning("SLICE=orchestrator EV=DP_UNREGISTER dpid=%s", dpid)
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
    # Link failure handling (OFPPortStatus)
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        dpid = dp.id
        port_no = msg.desc.port_no
        ofproto = dp.ofproto

        # Best-effort correlation: PortStatus is async, so we attach the latest tick seq.
        seq = int(getattr(self, "_tick_seq", 0))

        # Map (dpid, port) -> neighbor switch (inter-switch ports only)
        nbr = PORT_TO_NEIGHBOR.get((dpid, port_no))
        if nbr is None:
            return

        is_down = bool(msg.desc.state & ofproto.OFPPS_LINK_DOWN)

        # Current paths (may be None)
        lat_path = self.slices.get("latency", {}).get("current_path") if ENABLE_LATENCY_SLICE else None
        thr_path = self.slices.get("throughput", {}).get("current_path") if ENABLE_THROUGHPUT_SLICE else None

        affected_latency = False
        affected_throughput = False

        if is_down:
            # DOWN: remove edges from graph (both directions if present)
            if (dpid, nbr) in LINK_PARAMS:
                self.topo.remove_link(dpid, nbr)
                affected_latency = affected_latency or self._edge_in_path((dpid, nbr), lat_path)
                affected_throughput = affected_throughput or self._edge_in_path((dpid, nbr), thr_path)

            if (nbr, dpid) in LINK_PARAMS:
                self.topo.remove_link(nbr, dpid)
                affected_latency = affected_latency or self._edge_in_path((nbr, dpid), lat_path)
                affected_throughput = affected_throughput or self._edge_in_path((nbr, dpid), thr_path)

            self.logger.warning(
                "SLICE=orchestrator EV=LINK_DOWN seq=%s dpid=%s port=%s nbr=%s",
                seq, dpid, port_no, nbr
            )

            # Trigger recompute only if affected
            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None and affected_latency:
                self.latency_mgr.on_path_affected(reason="port_down_on_current_path")

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None and affected_throughput:
                self.throughput_mgr.on_path_affected(reason="port_down_on_current_path")

        else:
            # UP: restore edges in graph (both directions if present)
            if (dpid, nbr) in LINK_PARAMS:
                p = LINK_PARAMS[(dpid, nbr)]
                self.topo.add_link(dpid, nbr, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])

            if (nbr, dpid) in LINK_PARAMS:
                p = LINK_PARAMS[(nbr, dpid)]
                self.topo.add_link(nbr, dpid, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])

            self.logger.info(
                "SLICE=orchestrator EV=LINK_UP seq=%s dpid=%s port=%s nbr=%s",
                seq, dpid, port_no, nbr
            )

            # On link UP, recompute ONLY if slice is DISCONNECTED (current_path is None)
            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
                if self.slices.get("latency", {}).get("current_path") is None:
                    self.logger.warning("SLICE=latency EV=RECOVERY_TRIGGER seq=%s reason=link_up_recovery", seq)
                    self.latency_mgr.recompute_path(reason="link_up_recovery")

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
                if self.slices.get("throughput", {}).get("current_path") is None:
                    self.logger.warning("SLICE=throughput EV=RECOVERY_TRIGGER seq=%s reason=link_up_recovery", seq)
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
        Semi-passive monitoring with Dual-Tier Polling Monitor:

          - Poll PortStats every MONITOR_INTERVAL_S.
          - Tier A (active, priority): poll ONLY switches on active paths (latency + throughput),
            to keep control-plane overhead bounded while keeping QoS enforcement reactive.
          - Tier B (background, RR): additionally poll a small number of non-active switches
            in round-robin to prevent "frozen" EWMA used_mbps metrics outside the active path(s).
          - After stats updates, evaluate QoS:
              * latency slice: cost-based QoS
              * throughput slice: bottleneck residual QoS
        """
        MONITOR_INTERVAL_S = 10

        # Dual-Tier budget:
        # - Tier A: poll all dpids on active path(s) every cycle (priority).
        # - Tier B: poll K background dpids per cycle using round-robin.
        BACKGROUND_POLL_PER_TICK = 2

        while True:
            self._bootstrap_if_ready()

            if not self.bootstrap_done:
                hub.sleep(MONITOR_INTERVAL_S)
                continue

            self._tick_seq += 1
            tick = self._tick_seq
            now = time.time()

            lat_path = self.slices.get("latency", {}).get("current_path") if ENABLE_LATENCY_SLICE else None
            thr_path = self.slices.get("throughput", {}).get("current_path") if ENABLE_THROUGHPUT_SLICE else None

            self._last_lat_path = lat_path
            self._last_thr_path = thr_path

            # ---------------- Tick header (operator-friendly) ----------------
            self.logger.info("\nSLICE=orchestrator EV=TICK seq=%s ts=%.3f interval_s=%s", tick, now, MONITOR_INTERVAL_S)

            # ---------------- Slice state summary (stable per tick) ----------------
            if ENABLE_LATENCY_SLICE:
                p = lat_path
                state = "DISCONNECTED" if not p else "ACTIVE"
                self.logger.info("SLICE=latency EV=PATH_STATE seq=%s state=%s path=%s", tick, state, p)

            if ENABLE_THROUGHPUT_SLICE:
                conf = self.slices.get("throughput", {})
                p = thr_path
                state = conf.get("state", "?")
                backend = conf.get("active_backend", "?")
                min_mbps = conf.get("min_throughput_mbps", "?")
                self.logger.info(
                    "SLICE=throughput EV=PATH_STATE seq=%s state=%s backend=%s min_mbps=%s path=%s",
                    tick, state, backend, min_mbps, p
                )

            # ---------------- Tier A (active path polling) ----------------
            active_dpids = []
            if lat_path:
                active_dpids.extend(lat_path)
            if thr_path:
                active_dpids.extend(thr_path)

            # Remove duplicates preserving order
            active_dpids = list(dict.fromkeys(active_dpids))
            active_set = set(active_dpids)

            if active_dpids:
                self.logger.info("SLICE=orchestrator EV=POLL_ACTIVE seq=%s dpids=%s", tick, active_dpids)

            # Poll Tier A first (priority). Label per dpid is chosen deterministically:
            # if dpid appears on both paths, label as "active_both".
            lat_set = set(lat_path) if lat_path else set()
            thr_set = set(thr_path) if thr_path else set()

            for dpid in active_dpids:
                dp = self.datapaths.get(dpid)
                if dp is None:
                    continue

                if (dpid in lat_set) and (dpid in thr_set):
                    label = "active_both"
                elif dpid in lat_set:
                    label = "active_latency"
                elif dpid in thr_set:
                    label = "active_throughput"
                else:
                    label = "active"

                self._request_port_stats(dp, tier="active", label=label, tick_seq=tick)

            # ---------------- Tier B (background RR polling) ----------------
            bg_list = sorted(self.expected_switches - active_set)

            # Update RR list if membership changed
            if bg_list != self._bg_rr_list:
                self._bg_rr_list = bg_list
                if self._bg_rr_list:
                    self._bg_rr_idx = self._bg_rr_idx % len(self._bg_rr_list)
                else:
                    self._bg_rr_idx = 0

            sent_bg = []
            if self._bg_rr_list and BACKGROUND_POLL_PER_TICK > 0:
                guard = 0
                while len(sent_bg) < BACKGROUND_POLL_PER_TICK and guard < len(self._bg_rr_list):
                    dpid = self._bg_rr_list[self._bg_rr_idx]
                    dp = self.datapaths.get(dpid)

                    # advance index every attempt (but count "sent" only on actual send)
                    self._bg_rr_idx = (self._bg_rr_idx + 1) % len(self._bg_rr_list)

                    if dp is not None:
                        self._request_port_stats(dp, tier="rr", label="rr", tick_seq=tick)
                        sent_bg.append(dpid)

                    guard += 1

            if sent_bg:
                self.logger.info("SLICE=orchestrator EV=POLL_RR seq=%s dpids=%s", tick, sent_bg)

            # ---------------- QoS checks (managers) ----------------
            if ENABLE_LATENCY_SLICE and self.latency_mgr is not None:
                self.latency_mgr.check_qos()

            if ENABLE_THROUGHPUT_SLICE and self.throughput_mgr is not None:
                self.throughput_mgr.check_qos()

            hub.sleep(MONITOR_INTERVAL_S)

    def _request_port_stats(self, datapath, *, tier: str, label: str, tick_seq: int):
        """
        Send a PortStats request and record poll context for later correlation
        when the async PortStatsReply arrives.
        """
        dpid = datapath.id

        # Record context for this DPID (used by _port_stats_reply_handler)
        self._poll_ctx_by_dpid[dpid] = {
            "tick": int(tick_seq),
            "tier": str(tier),
            "label": str(label),
        }

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        now = time.time()

        ctx = self._poll_ctx_by_dpid.get(dpid, None)
        seq = ctx["tick"] if ctx else -1
        tier = ctx["tier"] if ctx else "unknown"
        label = ctx["label"] if ctx else "unknown"

        for stat in ev.msg.body:
            port_no = stat.port_no
            tx_bytes = stat.tx_bytes

            key = (dpid, port_no)
            last = self.port_stats_state.get(key)

            if last is not None:
                delta_tx = tx_bytes - last["tx_bytes"]
                delta_t = now - last["time"]

                if delta_tx < 0:
                    # Counter wrapped/reset; skip this sample
                    self.port_stats_state[key] = {"tx_bytes": tx_bytes, "time": now}
                    continue

                if delta_t > 0:
                    used_mbps = (delta_tx * 8.0) / (delta_t * 1e6)

                    nbr = PORT_TO_NEIGHBOR.get((dpid, port_no))
                    if nbr is not None:
                        self.topo.update_link_usage(dpid, nbr, used_mbps)
                        m = self.topo.get_link_metrics(dpid, nbr)
                        if m is None:
                            self.logger.warning(
                                "SLICE=orchestrator EV=LINK_METRICS_MISSING seq=%s tier=%s label=%s u=%s v=%s port=%s",
                                seq, tier, label, dpid, nbr, port_no
                            )
                        else:
                            self.logger.info(
                                "SLICE=orchestrator EV=LINK seq=%s tier=%s label=%s u=%s v=%s port=%s "
                                "raw=%.3f ewma=%.3f cap=%.1f",
                                seq, tier, label, dpid, nbr, port_no,
                                float(m["used_mbps_raw"]), float(m["used_mbps_ewma"]), float(m["capacity_mbps"])
                            )

            self.port_stats_state[key] = {"tx_bytes": tx_bytes, "time": now}
