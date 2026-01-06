# controller/latency_slice.py

"""
LatencySliceManager: slice-specific logic for the low-latency slice.

This module MUST NOT define a RyuApp.
It contains only policy/decision logic and flow programming for the "latency" slice.

Design goals:
- Keep behavior identical to the previous monolithic SliceQosApp implementation.
- Do not own OpenFlow event handlers or monitoring threads.
- Be callable from an orchestrator RyuApp that owns shared state (topology, datapaths, stats).

Recovery-safe extension:
- If no feasible path exists, enter an explicit DISCONNECTED state:
    * current_path = None
    * delete slice flows to avoid blackholing traffic
  This allows the orchestrator to trigger a recovery recompute on link-up.
"""

from __future__ import annotations

import math
from typing import Dict, List, Any

from flow_utils import (
    build_match_for_slice,
    build_reverse_match_for_slice,
    build_actions_for_slice,
    add_flow,
    delete_flows_for_cookie,
)


COOKIE_LATENCY = 0xA2_000001  # any non-zero constant identifying "our" latency slice flows
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


class LatencySliceManager:
    """
    Latency slice path management policy (Architecture 2):

    - First time (bootstrap):
        * compute the best path using a cost function (delay + congestion penalty),
        * if cost <= max_delay_ms: install it;
          else: install the best available path but explicitly log QoS not satisfied.

    - After that:
        * DO NOT recompute continuously,
        * recompute only when:
            - a link on the current path goes DOWN (handled by orchestrator via PortStatus), OR
            - QoS is violated on the current path (estimated cost > max_delay_ms).

    Recovery-safe behavior:
      - If no path exists, set current_path=None and delete flows (DISCONNECTED state).
    """

    def __init__(
        self,
        *,
        topo,
        datapaths: Dict[int, Any],
        slice_conf: Dict[str, Any],
        next_hop_port: Dict[tuple, int],
        logger,
        flow_priority: int,
        cookie: int = COOKIE_LATENCY,
    ):
        self.topo = topo
        self.datapaths = datapaths
        self.slice_conf = slice_conf
        self.next_hop_port = next_hop_port
        self.logger = logger

        self.flow_priority = int(flow_priority)
        self.cookie = int(cookie)

        # Sanity checks (fail fast)
        assert self.slice_conf is not None, "LatencySliceManager: slice_conf is None"
        assert "ingress_switch" in self.slice_conf, "LatencySliceManager: missing ingress_switch"
        assert "egress_switch" in self.slice_conf, "LatencySliceManager: missing egress_switch"
        assert "max_delay_ms" in self.slice_conf, "LatencySliceManager: missing max_delay_ms"

    # ------------------------------------------------------------------
    # Public entry points used by the orchestrator
    # ------------------------------------------------------------------
    def recompute_path(self, reason: str = "unspecified"):
        """
        Compute best latency path and (re)install flows if path changes.

        Recovery-safe behavior:
          - If NO path exists:
              * enter "disconnected" state by setting current_path = None,
              * delete slice flows to avoid blackholing traffic,
              * rely on the orchestrator to trigger recovery recompute on link-up.
        """
        ingress = self.slice_conf["ingress_switch"]
        egress = self.slice_conf["egress_switch"]
        max_delay = float(self.slice_conf["max_delay_ms"])

        path = self.topo.compute_latency_path(ingress, egress, weight_fn=self._latency_weight)
        if path is None:
            # No feasible route exists in the logical graph.
            # Enter an explicit DISCONNECTED state and remove flows to avoid stale forwarding.
            if self.slice_conf.get("current_path") is not None:
                self.logger.warning(
                    "SLICE=latency EV=DISCONNECTED reason=%s ingress=%s egress=%s",
                    reason, ingress, egress
                )

            self.slice_conf["current_path"] = None
            self.delete_flows()
            return

        total_cost = self.topo.estimate_path_cost(path, weight_fn=self._latency_weight)

        old_path = self.slice_conf.get("current_path")
        if old_path == path:
            return

        if total_cost <= max_delay:
            self.logger.info(
                "SLICE=latency EV=PATH_INSTALL reason=%s path=%s cost_ms=%.1f max_ms=%.1f qos=OK",
                reason, path, total_cost, max_delay
            )
        else:
            self.logger.warning(
                "SLICE=latency EV=PATH_INSTALL reason=%s path=%s cost_ms=%.1f max_ms=%.1f qos=UNSAT",
                reason, path, total_cost, max_delay
            )

        self.delete_flows()
        self.install_flows(path)
        self.slice_conf["current_path"] = path

    def check_qos(self):
        """
        Evaluate QoS on the current path and recompute if violated.
        Called periodically by the orchestrator after stats updates.
        """
        path = self.slice_conf.get("current_path")
        if not path:
            return

        max_delay = float(self.slice_conf["max_delay_ms"])
        total_cost = self.topo.estimate_path_cost(path, weight_fn=self._latency_weight)

        if total_cost <= max_delay:
            return

        self.logger.warning(
            "SLICE=latency EV=QOS_VIOLATION reason=qos_violation path=%s cost_ms=%.1f max_ms=%.1f",
            path, total_cost, max_delay
        )

        self.recompute_path(reason="qos_violation")

    def on_path_affected(self, reason: str = "path_affected"):
        """
        Called by the orchestrator when it detects that a link-down event affects the current path.
        """
        self.logger.warning("SLICE=latency EV=PATH_AFFECTED reason=%s action=RECOMPUTE", reason)
        self.recompute_path(reason=reason)

    def delete_flows(self):
        """
        Remove only the flows owned by this slice (cookie-scoped).
        """
        for _, dp in self.datapaths.items():
            delete_flows_for_cookie(dp, cookie=self.cookie, cookie_mask=COOKIE_MASK_ALL)

    def install_flows(self, path: List[int]):
        """
        Install bidirectional slice rules:
          - FORWARD:  client -> server
          - REVERSE:  server -> client

        Requires in slice_config.py:
          - ingress_host_port (port on ingress_switch towards client host)
          - egress_host_port  (port on egress_switch towards server host)

        Requires in link_config.py:
          - NEXT_HOP_PORT[(dpid, next_dpid)] -> out_port
        """
        ingress = self.slice_conf["ingress_switch"]
        egress = self.slice_conf["egress_switch"]

        assert "ingress_host_port" in self.slice_conf, "Missing ingress_host_port in slice_conf"
        assert "egress_host_port" in self.slice_conf, "Missing egress_host_port in slice_conf"

        in_host_port = int(self.slice_conf["ingress_host_port"])
        out_host_port = int(self.slice_conf["egress_host_port"])

        prio = int(self.flow_priority)

        # ---------- FORWARD (client -> server) ----------
        for idx, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser
            fwd_match = build_match_for_slice(parser, self.slice_conf)

            if dpid == egress:
                # Last switch: deliver to server host
                actions = build_actions_for_slice(parser, out_host_port, self.slice_conf)
                add_flow(
                    dp,
                    priority=prio,
                    match=fwd_match,
                    actions=actions,
                    idle_timeout=0,
                    hard_timeout=0,
                    cookie=self.cookie,
                )
                continue

            # intermediate hop -> next switch
            if idx >= len(path) - 1:
                continue

            next_dpid = path[idx + 1]
            out_port = self.next_hop_port.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("SLICE=latency EV=NO_PORT_MAP direction=fwd u=%s v=%s", dpid, next_dpid)
                continue

            actions = build_actions_for_slice(parser, int(out_port), self.slice_conf)
            add_flow(
                dp,
                priority=prio,
                match=fwd_match,
                actions=actions,
                idle_timeout=0,
                hard_timeout=0,
                cookie=self.cookie,
            )

        # ---------- REVERSE (server -> client) ----------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser
            rev_match = build_reverse_match_for_slice(parser, self.slice_conf)

            if dpid == ingress:
                # Last hop in reverse: deliver to client host
                actions = build_actions_for_slice(parser, in_host_port, self.slice_conf)
                add_flow(
                    dp,
                    priority=prio,
                    match=rev_match,
                    actions=actions,
                    idle_timeout=0,
                    hard_timeout=0,
                    cookie=self.cookie,
                )
                continue

            if idx >= len(rev_path) - 1:
                continue

            next_dpid = rev_path[idx + 1]
            out_port = self.next_hop_port.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("SLICE=latency EV=NO_PORT_MAP direction=fwd u=%s v=%s", dpid, next_dpid)
                continue

            actions = build_actions_for_slice(parser, int(out_port), self.slice_conf)
            add_flow(
                dp,
                priority=prio,
                match=rev_match,
                actions=actions,
                idle_timeout=0,
                hard_timeout=0,
                cookie=self.cookie,
            )

    # ------------------------------------------------------------------
    # Internal helpers (latency objective)
    # ------------------------------------------------------------------
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
        util = max(0.0, min(util, 0.999))

        # Congestion penalty: grows fast near saturation
        ALPHA = 10.0
        EPS = 1e-3
        congestion_penalty = ALPHA * (util / (1.0 - util + EPS))

        PROC_MS_PER_HOP = 0.5
        return delay + congestion_penalty + PROC_MS_PER_HOP
