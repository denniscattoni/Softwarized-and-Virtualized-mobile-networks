# controller/slice_qos_app.py

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, DEAD_DISPATCHER, CONFIG_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3

from ryu.lib import hub
import time
import logging
import math

from slice_config import SLICES
from graph_state import TopologyGraph
from flow_utils import (
    build_match_for_slice,
    build_reverse_match_for_slice,
    build_actions_for_slice,
    add_flow,
    delete_flows_for_cookie,
)

from link_config import LINK_PARAMS, NEXT_HOP_PORT, PORT_TO_NEIGHBOR


COOKIE_LATENCY = 0xA2_000001  # any non-zero constant identifying "our" slice flows
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


class SliceQosApp(app_manager.RyuApp):
    """
    SliceQosApp (Architecture 2)

    Key idea:
      - Do NOT use Ryu topology discovery (--observe-links, LLDP).
      - Treat the topology as known a priori via LINK_PARAMS.
      - Consider topology "ready" when all expected switches have connected.
      - Compute the initial best latency path once, then recompute only when:
          * a link on the current path goes DOWN (via OFPPortStatus), OR
          * QoS is violated on the current path (estimated cost > max_delay_ms).

    Operational assumption:
      - For baseline connectivity / loop avoidance you can still run:
            ryu-manager controller.slice_qos_app
        Our app installs higher-priority rules for the latency slice traffic.
    """

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    SLICE_FLOW_PRIORITY = 10  # must be > L2/STP app priority

    def __init__(self, *args, **kwargs):
        super(SliceQosApp, self).__init__(*args, **kwargs)

        logging.getLogger().setLevel(logging.WARNING)
        self.logger.setLevel(logging.INFO)

        self.datapaths = {}
        self.topo = TopologyGraph()
        self.slices = SLICES

        self.port_stats_state = {}  # (dpid, port_no) -> {tx_bytes, time}

        self.expected_switches = self._derive_expected_switches()
        self.bootstrap_done = False

        self._init_logical_graph_from_config()

        self.monitor_thread = hub.spawn(self._monitor)

        self.logger.info(
            "SliceQosApp initialized (arch2). Expected switches: %s",
            sorted(self.expected_switches),
        )

    # ------------------------------------------------------------------
    # Bootstrap / expected topology
    # ------------------------------------------------------------------
    def _derive_expected_switches(self):
        s = set()
        for (u, v) in LINK_PARAMS.keys():
            s.add(u)
            s.add(v)
        for _, conf in self.slices.items():
            s.add(conf["ingress_switch"])
            s.add(conf["egress_switch"])
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

        self.logger.info("\nTopology READY (all expected datapaths registered). Computing initial latency path.")
        self._recompute_latency_slice_path(reason="bootstrap_all_switches_connected")
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

        # Table-miss -> controller (needed if you implement ARP/L2/routing logic yourself)
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

        current_path = self.slices["latency"].get("current_path")
        affected = False

        if is_down:
            if (dpid, nbr) in LINK_PARAMS:
                self.topo.remove_link(dpid, nbr)
                affected = affected or self._edge_in_path((dpid, nbr), current_path)

            if (nbr, dpid) in LINK_PARAMS:
                self.topo.remove_link(nbr, dpid)
                affected = affected or self._edge_in_path((nbr, dpid), current_path)

            self.logger.warning("Link DOWN via port status: %s(port %s) <-> %s", dpid, port_no, nbr)

            if affected:
                self.logger.warning("Current latency path affected. Recomputing.")
                self._recompute_latency_slice_path(reason="port_down_on_current_path")

        else:
            if (dpid, nbr) in LINK_PARAMS:
                p = LINK_PARAMS[(dpid, nbr)]
                self.topo.add_link(dpid, nbr, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])

            if (nbr, dpid) in LINK_PARAMS:
                p = LINK_PARAMS[(nbr, dpid)]
                self.topo.add_link(nbr, dpid, delay_ms=p["delay_ms"], capacity_mbps=p["capacity_mbps"])

            self.logger.info("Link UP via port status: %s(port %s) <-> %s", dpid, port_no, nbr)

            # Conservative Arch2: no reroute on UP.
            # If you want to "improve" the route when the network recovers, uncomment:
            # if current_path is not None:
            #     self._recompute_latency_slice_path(reason="link_up_improvement")

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
          - Poll PortStats less frequently (MONITOR_INTERVAL_S).
          - Poll ONLY switches on the current latency-slice path (critical datapaths),
          - Still evaluate QoS on the current path after stats updates.

        Notes:
          - If there is no current_path yet, we only run bootstrap logic.
        """
        MONITOR_INTERVAL_S = 10

        while True:
            # Ensure we bootstrap once all expected datapaths are registered
            self._bootstrap_if_ready()

            # If path not installed yet, nothing useful to poll
            current_path = self.slices["latency"].get("current_path")
            if not current_path:
                hub.sleep(MONITOR_INTERVAL_S)
                continue

            # Poll ONLY datapaths on the current path
            for dpid in dict.fromkeys(current_path):
                dp = self.datapaths.get(dpid)
                if dp is None:
                    continue
                self._request_port_stats(dp)

            # After updating link usage (via PortStatsReply handler), check QoS
            self._check_latency_slice_qos(current_path)

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
                    # counter reset/wrap; ignore sample
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

    # ------------------------------------------------------------------
    # Latency slice: compute and install best path
    # ------------------------------------------------------------------
    """
    Latency slice path management policy (Architecture 2):

    - First time (bootstrap):
        * wait until all expected switches have registered (datapaths ready),
        * compute the best path using a cost function (delay + congestion penalty),
        * if cost <= max_delay_ms: install it;
          else: install the best available path but explicitly log QoS not satisfied.

    - After that:
        * DO NOT recompute continuously,
        * recompute only when:
            - a link on the current path goes DOWN (via OFPPortStatus), OR
            - QoS is violated on the current path (estimated cost > max_delay_ms).
    """

    def _latency_weight(self, u, v, d):
        """
        Edge cost in ms-like units.

        NOTE: EWMA is NOT computed here.
        The value d["used_mbps"] is already smoothed by TopologyGraph.update_link_usage().
        """
        assert d is not None, f"Edge metrics dict is None for {u}->{v}"

        # Hard requirements (no fail-safe defaults)
        assert "delay_ms" in d, f"Missing delay_ms for {u}->{v}"
        assert "capacity_mbps" in d, f"Missing capacity_mbps for {u}->{v}"
        assert "used_mbps" in d, f"Missing used_mbps for {u}->{v}"

        delay = float(d["delay_ms"])
        cap = float(d["capacity_mbps"])
        used = float(d["used_mbps"])  # already EWMA-smoothed

        assert math.isfinite(delay) and delay >= 0.0, f"Invalid delay_ms={delay} for {u}->{v}"
        assert math.isfinite(cap) and cap > 0.0, f"Invalid capacity_mbps={cap} for {u}->{v}"
        assert math.isfinite(used) and used >= 0.0, f"Invalid used_mbps={used} for {u}->{v}"

        util = used / cap
        # keep it strictly < 1 to avoid singularities
        util = max(0.0, min(util, 0.999))

        # Congestion penalty: grows fast near saturation
        ALPHA = 10.0
        EPS = 1e-3
        congestion_penalty = ALPHA * (util / (1.0 - util + EPS))

        PROC_MS_PER_HOP = 0.5
        return delay + congestion_penalty + PROC_MS_PER_HOP

    def _recompute_latency_slice_path(self, reason="unspecified"):
        slice_conf = self.slices["latency"]
        ingress = slice_conf["ingress_switch"]
        egress = slice_conf["egress_switch"]
        max_delay = float(slice_conf["max_delay_ms"])

        path = self.topo.compute_latency_path(ingress, egress, weight_fn=self._latency_weight)
        if path is None:
            self.logger.warning(
                "No path found for latency slice from %s to %s [reason=%s]",
                ingress, egress, reason,
            )
            return

        total_cost = self.topo.estimate_path_cost(path, weight_fn=self._latency_weight)

        old_path = slice_conf.get("current_path")
        if old_path == path:
            return

        if total_cost <= max_delay:
            self.logger.info(
                "Installing latency path %s (cost=%.1f ms <= %.1f ms) [reason=%s]",
                path, total_cost, max_delay, reason,
            )
        else:
            self.logger.warning(
                "Latency slice QoS NOT satisfied: best path %s has cost=%.1f ms > %.1f ms. "
                "Installing best available path anyway. [reason=%s]",
                path, total_cost, max_delay, reason,
            )

        self._delete_latency_slice_flows()
        self._install_latency_slice_flows(path)
        slice_conf["current_path"] = path

    def _check_latency_slice_qos(self, path):
        slice_conf = self.slices["latency"]
        max_delay = float(slice_conf["max_delay_ms"])

        total_cost = self.topo.estimate_path_cost(path, weight_fn=self._latency_weight)
        if total_cost <= max_delay:
            return

        self.logger.warning(
            "Latency slice QoS VIOLATION on current path %s (cost=%.1f ms > %.1f ms). Recomputing.",
            path, total_cost, max_delay,
        )
        self._recompute_latency_slice_path(reason="qos_violation")

    def _delete_latency_slice_flows(self):
        for _, dp in self.datapaths.items():
            delete_flows_for_cookie(dp, cookie=COOKIE_LATENCY, cookie_mask=COOKIE_MASK_ALL)

    def _install_latency_slice_flows(self, path):
        """
        Install bidirectional slice rules:
          - FORWARD:  c2 -> srv1
          - REVERSE:  srv1 -> c2

        Requires in slice_config.py:
          - ingress_host_port (port on ingress_switch towards client host)
          - egress_host_port  (port on egress_switch towards server host)

        Requires in link_config.py:
          - NEXT_HOP_PORT[(dpid, next_dpid)] -> out_port
        """
        slice_conf = self.slices["latency"]
        ingress = slice_conf["ingress_switch"]
        egress = slice_conf["egress_switch"]

        assert "ingress_host_port" in slice_conf, "Missing ingress_host_port in slice_conf"
        assert "egress_host_port" in slice_conf, "Missing egress_host_port in slice_conf"

        in_host_port = int(slice_conf["ingress_host_port"])
        out_host_port = int(slice_conf["egress_host_port"])

        prio = int(self.SLICE_FLOW_PRIORITY)

        # ---------- FORWARD (client -> server) ----------
        for idx, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser
            fwd_match = build_match_for_slice(parser, slice_conf)

            if dpid == egress:
                # Last switch: deliver to server host
                actions = build_actions_for_slice(parser, out_host_port, slice_conf)
                add_flow(
                    dp,
                    priority=prio,
                    match=fwd_match,
                    actions=actions,
                    idle_timeout=0,
                    hard_timeout=0,
                    cookie=COOKIE_LATENCY,
                )
                continue

            # intermediate hop -> next switch
            if idx >= len(path) - 1:
                continue

            next_dpid = path[idx + 1]
            out_port = NEXT_HOP_PORT.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s", dpid, next_dpid)
                continue

            actions = build_actions_for_slice(parser, int(out_port), slice_conf)
            add_flow(
                dp,
                priority=prio,
                match=fwd_match,
                actions=actions,
                idle_timeout=0,
                hard_timeout=0,
                cookie=COOKIE_LATENCY,
            )

        # ---------- REVERSE (server -> client) ----------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser
            rev_match = build_reverse_match_for_slice(parser, slice_conf)

            if dpid == ingress:
                # Last hop in reverse: deliver to client host
                actions = build_actions_for_slice(parser, in_host_port, slice_conf)
                add_flow(
                    dp,
                    priority=prio,
                    match=rev_match,
                    actions=actions,
                    idle_timeout=0,
                    hard_timeout=0,
                    cookie=COOKIE_LATENCY,
                )
                continue

            if idx >= len(rev_path) - 1:
                continue

            next_dpid = rev_path[idx + 1]
            out_port = NEXT_HOP_PORT.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s (reverse)", dpid, next_dpid)
                continue

            actions = build_actions_for_slice(parser, int(out_port), slice_conf)
            add_flow(
                dp,
                priority=prio,
                match=rev_match,
                actions=actions,
                idle_timeout=0,
                hard_timeout=0,
                cookie=COOKIE_LATENCY,
            )


