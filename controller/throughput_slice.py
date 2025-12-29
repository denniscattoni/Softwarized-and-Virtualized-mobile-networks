# controller/throughput_slice.py

"""
ThroughputSliceManager: slice-specific logic for the high-throughput slice.

This module MUST NOT define a RyuApp.
It contains only policy/decision logic and flow programming for the "throughput" slice.

Design goals:
- Keep behavior deterministic and debuggable (hard-fail on missing config).
- Use a max-min (widest-path) policy to maximize bottleneck residual throughput.
- Implement SDN-based VIP NAT so the client targets a stable service IP independently
  from the physical backend location (srv2 or srv1).

Pipeline scope:
- Step 1: VIP + SDN NAT flows (both backends can be ON for validation).
- Step 2: Widest-path routing + QoS check (route-first).
- Step 3: Service migration (start/stop) will be integrated later.
"""

from __future__ import annotations

import heapq
import math
from typing import Dict, List, Any, Optional

from flow_utils import (
    build_match_for_slice,
    add_flow,
    delete_flows_for_cookie,
)


COOKIE_THROUGHPUT = 0xB2_000001  # any non-zero constant identifying "our" throughput slice flows
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


class ThroughputSliceManager:
    """
    High-throughput slice policy:

    - Bootstrap:
        * compute and install an initial widest path toward the active backend (default: remote=srv2).

    - Operational:
        * DO NOT recompute continuously.
        * recompute only when:
            - a link failure affects the current path, OR
            - the current path bottleneck residual throughput violates the QoS threshold.

    Recovery-safe behavior:
      - If no path exists, set current_path=None and delete flows (DISCONNECTED state).
      - On link-up, the orchestrator will trigger recompute only if DISCONNECTED.
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
        cookie: int = COOKIE_THROUGHPUT,
    ):
        self.topo = topo
        self.datapaths = datapaths
        self.slice_conf = slice_conf
        self.next_hop_port = next_hop_port
        self.logger = logger

        self.flow_priority = int(flow_priority)
        self.cookie = int(cookie)

        # Sanity checks (fail fast)
        assert self.slice_conf is not None, "ThroughputSliceManager: slice_conf is None"
        assert "ingress_switch" in self.slice_conf, "ThroughputSliceManager: missing ingress_switch"
        assert "min_throughput_mbps" in self.slice_conf, "ThroughputSliceManager: missing min_throughput_mbps"
        assert "remote_backend" in self.slice_conf, "ThroughputSliceManager: missing remote_backend"
        assert "closest_backend" in self.slice_conf, "ThroughputSliceManager: missing closest_backend"
        assert "vip_ip" in self.slice_conf and "vip_mac" in self.slice_conf, "ThroughputSliceManager: missing VIP params"
        assert "client_ip" in self.slice_conf and "client_mac" in self.slice_conf, "ThroughputSliceManager: missing client identity"

    # ------------------------------------------------------------------
    # Public entry points used by the orchestrator
    # ------------------------------------------------------------------
    def recompute_path(self, reason: str = "unspecified"):
        """
        Compute and (re)install widest-path toward the active backend.

        Widest-path objective:
          maximize bottleneck residual capacity, where:
              residual(u,v) = max(0, capacity_mbps - used_mbps_ewma)
              bottleneck(path) = min residual(u,v) along the path

        IMPORTANT (bidirectional safety):
          For each hop (u,v), the effective residual is:
              hop_residual = min(residual(u,v), residual(v,u))
          This makes QoS detection consistent with real download-heavy traffic
          (often dominates on the reverse direction).

        If no path exists:
          - enter DISCONNECTED state
          - delete flows to avoid stale forwarding
        """
        ingress = int(self.slice_conf["ingress_switch"])
        backend = self._get_active_backend_conf()
        egress = int(backend["egress_switch"])
        thr_min = float(self.slice_conf["min_throughput_mbps"])

        path = self._compute_widest_path_bidirectional(ingress, egress)
        if path is None:
            self.logger.warning(
                "Throughput slice DISCONNECTED: no path from %s to %s. Clearing current_path and deleting slice flows. "
                "[backend=%s reason=%s]",
                ingress, egress, backend.get("name", "?"), reason,
            )
            self.slice_conf["current_path"] = None
            self.slice_conf["state"] = "DISCONNECTED"
            self.delete_flows()
            return

        bottleneck = self._estimate_path_bottleneck_bidirectional_mbps(path)
        old_path = self.slice_conf.get("current_path")

        # Always log recompute outcome (this is the core signal for widest-path validation).
        self.logger.info(
            "Throughput recompute: selected path=%s bottleneck=%.2f Mbps (min=%.2f Mbps) backend=%s state=%s [reason=%s]",
            path,
            bottleneck,
            thr_min,
            backend.get("name", "?"),
            self.slice_conf.get("state", "?"),
            reason,
        )

        # If path is unchanged, do nothing (stability by construction).
        if old_path == path:
            if reason in ("qos_violation", "port_down_on_current_path"):
                self._log_path_residuals(path, prefix="Throughput path unchanged")
            return

        # Path changed -> install deterministic flows for the selected backend
        self._log_path_residuals(path, prefix="Throughput installing new path")
        self.delete_flows()
        self.install_flows(path, backend)
        self.slice_conf["current_path"] = path

        # State bookkeeping (migration will refine this later)
        if self.slice_conf.get("active_backend") == "closest":
            self.slice_conf["state"] = "NORMAL_CLOSEST"
        else:
            self.slice_conf["state"] = "NORMAL_REMOTE"

    def check_qos(self):
        """
        Evaluate QoS on the current path and recompute if violated.

        QoS condition:
          bottleneck_residual_mbps(path) >= min_throughput_mbps

        IMPORTANT (bidirectional safety):
          bottleneck is computed using hop_residual = min(res(u,v), res(v,u)).
        """
        path = self.slice_conf.get("current_path")
        if not path:
            return

        thr_min = float(self.slice_conf["min_throughput_mbps"])
        b = self._estimate_path_bottleneck_bidirectional_mbps(path)

        if b >= thr_min:
            return

        self.logger.warning(
            "Throughput slice QoS VIOLATION: path=%s bottleneck=%.2f Mbps < %.2f Mbps. Recomputing.",
            path, b, thr_min,
        )
        self._log_path_residuals(path, prefix="Throughput QoS violation on current path")
        self.recompute_path(reason="qos_violation")

    def on_path_affected(self, reason: str = "path_affected"):
        """
        Called by the orchestrator when it detects that a link-down event affects the current path.
        """
        self.logger.warning("Current throughput path affected. Recomputing. [reason=%s]", reason)
        self.recompute_path(reason=reason)

    def delete_flows(self):
        """
        Remove only the flows owned by this slice (cookie-scoped).
        """
        for _, dp in self.datapaths.items():
            delete_flows_for_cookie(dp, cookie=self.cookie, cookie_mask=COOKIE_MASK_ALL)

    def install_flows(self, path: List[int], backend_conf: Dict[str, Any]):
        """
        Install bidirectional slice rules with SDN-based VIP NAT:

          FORWARD:  c1 -> VIP:8080  (DNAT VIP->backend on egress edge)
          REVERSE:  backend -> c1   (SNAT backend->VIP on ingress edge)

        Notes:
          - We keep the same directional-graph simplification:
              reverse path = reversed(forward path).
          - We do not rely on L2 learning; flows are fully deterministic.

        Required in slice_config.py:
          - ingress_host_port
          - backend_conf: egress_host_port, ip, mac, tcp_port
          - vip_ip, vip_mac, client_ip, client_mac
        """
        ingress = int(self.slice_conf["ingress_switch"])
        ingress_host_port = int(self.slice_conf["ingress_host_port"])

        vip_ip = str(self.slice_conf["vip_ip"])
        vip_mac = str(self.slice_conf["vip_mac"])

        client_ip = str(self.slice_conf["client_ip"])
        client_mac = str(self.slice_conf["client_mac"])

        backend_ip = str(backend_conf["ip"])
        backend_mac = str(backend_conf["mac"])
        backend_tcp_port = int(backend_conf["tcp_port"])

        egress_switch = int(backend_conf["egress_switch"])
        egress_host_port = int(backend_conf["egress_host_port"])

        prio = int(self.flow_priority)

        # ---------- FORWARD (client -> VIP) ----------
        for idx, dpid in enumerate(path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser

            # Match on the slice forward definition (client -> VIP:8080)
            fwd_match = build_match_for_slice(parser, self.slice_conf)

            if dpid == egress_switch:
                # Last switch: DNAT VIP->backend and deliver to backend host
                actions = [
                    parser.OFPActionSetField(ipv4_dst=backend_ip),
                    parser.OFPActionSetField(eth_dst=backend_mac),
                    parser.OFPActionOutput(int(egress_host_port)),
                ]
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

            if idx >= len(path) - 1:
                continue

            next_dpid = path[idx + 1]
            out_port = self.next_hop_port.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s", dpid, next_dpid)
                continue

            actions = [parser.OFPActionOutput(int(out_port))]
            add_flow(
                dp,
                priority=prio,
                match=fwd_match,
                actions=actions,
                idle_timeout=0,
                hard_timeout=0,
                cookie=self.cookie,
            )

        # ---------- REVERSE (backend -> client) ----------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser

            # Reverse match must match packets from REAL backend IP to client IP.
            rev_match = self._build_reverse_match_for_backend(parser, backend_ip, backend_tcp_port, client_ip)

            if dpid == ingress:
                # Last hop in reverse: SNAT backend->VIP and deliver to client host.
                actions = [
                    parser.OFPActionSetField(ipv4_src=vip_ip),
                    parser.OFPActionSetField(eth_src=vip_mac),
                    parser.OFPActionSetField(eth_dst=client_mac),
                    parser.OFPActionOutput(int(ingress_host_port)),
                ]
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
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s (reverse)", dpid, next_dpid)
                continue

            actions = [parser.OFPActionOutput(int(out_port))]
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
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_active_backend_conf(self) -> Dict[str, Any]:
        """
        Return the currently active backend configuration dict.
        Hard-fail if active_backend is invalid.
        """
        ab = self.slice_conf.get("active_backend", "remote")
        if ab == "remote":
            return self.slice_conf["remote_backend"]
        if ab == "closest":
            return self.slice_conf["closest_backend"]
        raise AssertionError(f"ThroughputSliceManager: invalid active_backend={ab}")

    def _build_reverse_match_for_backend(self, parser, backend_ip: str, backend_port: int, client_ip: str):
        """
        Reverse match for backend->client traffic:

          ipv4_src = backend_ip
          ipv4_dst = client_ip
          ip_proto = TCP
          tcp_src  = backend_port (8080)

        IMPORTANT:
          Do NOT call build_reverse_match_for_slice() here.
          That helper swaps src/dst and would invert this match again.
        """
        return parser.OFPMatch(
            eth_type=0x0800,         # IPv4
            ipv4_src=str(backend_ip),
            ipv4_dst=str(client_ip),
            ip_proto=6,              # TCP
            tcp_src=int(backend_port),
        )

    def _edge_residual_mbps(self, u: int, v: int) -> Optional[float]:
        """
        Residual capacity on a directed edge (u,v):

          residual = max(0, capacity_mbps - used_mbps)

        Return None if metrics are missing.
        """
        m = self.topo.get_link_metrics(u, v)
        if m is None:
            return None

        cap = float(m["capacity_mbps"])
        used = float(m["used_mbps"])
        res = cap - used
        if res < 0.0:
            res = 0.0
        return float(res)

    def _estimate_path_bottleneck_bidirectional_mbps(self, path: List[int]) -> float:
        """
        Compute bottleneck residual throughput for the path using a bidirectional safety rule.

        For each hop (u,v), compute:
            res_fwd = residual(u,v)
            res_rev = residual(v,u)
            hop_residual = min(res_fwd, res_rev)

        Then:
            bottleneck(path) = min hop_residual along the path

        If any required metrics are missing, hard-fail: this is a controlled lab setup.
        """
        assert path is not None and len(path) >= 2, "ThroughputSliceManager: invalid path for bottleneck computation"

        edges = list(zip(path[:-1], path[1:]))
        bottleneck = float("inf")

        for (u, v) in edges:
            res_fwd = self._edge_residual_mbps(u, v)
            res_rev = self._edge_residual_mbps(v, u)

            assert res_fwd is not None, f"Missing metrics for edge {u}->{v}"
            assert res_rev is not None, f"Missing metrics for edge {v}->{u}"

            hop_res = min(float(res_fwd), float(res_rev))
            bottleneck = min(bottleneck, hop_res)

        if bottleneck == float("inf"):
            return 0.0
        return float(bottleneck)

    def _compute_widest_path_bidirectional(self, src: int, dst: int) -> Optional[List[int]]:
        """
        Compute a widest-path (max-min) route using a bidirectional residual metric,
        with deterministic tie-breaks.

        Objective (primary):
          - maximize bottleneck(path) where each hop uses:
                hop_residual(u,v) = min(residual(u,v), residual(v,u))
            and:
                bottleneck(path) = min hop_residual along the path

        Deterministic tie-breaks:
          1) maximize bottleneck
          2) minimize hop count
          3) minimize path lexicographically (stable across runs)

        Returns:
          - list of DPIDs [src, ..., dst] if a path exists
          - None if no path exists (or src/dst not in graph)
        """
        if src not in self.topo.G or dst not in self.topo.G:
            return None

        import heapq
        import math

        # best_b[v] = best bottleneck found so far for v
        # best_hops[v] = hopcount for that best bottleneck (tie-break #2)
        best_b: Dict[int, float] = {src: float("inf")}
        best_hops: Dict[int, int] = {src: 0}
        prev: Dict[int, int] = {}

        # Priority queue:
        #   - maximize bottleneck  => store -bottleneck
        #   - minimize hops       => store hops
        #   - minimize lexicographic path => handled by stable neighbor ordering + final path compare
        heap = [(-best_b[src], best_hops[src], src)]

        visited = set()

        while heap:
            neg_b, hops_u, u = heapq.heappop(heap)
            b_u = -neg_b

            if u in visited:
                continue
            visited.add(u)

            if u == dst:
                break

            # Deterministic neighbor order helps ensure stable results
            nbrs = list(self.topo.G.successors(u))
            nbrs.sort()

            for v in nbrs:
                res_fwd = self._edge_residual_mbps(u, v)
                res_rev = self._edge_residual_mbps(v, u)

                assert res_fwd is not None, f"Missing metrics for edge {u}->{v}"
                assert res_rev is not None, f"Missing metrics for edge {v}->{u}"

                hop_res = min(float(res_fwd), float(res_rev))
                assert math.isfinite(hop_res) and hop_res >= 0.0, f"Invalid hop_residual for {u}<->{v}: {hop_res}"

                cand_b = min(float(b_u), float(hop_res))
                cand_hops = int(hops_u) + 1

                old_b = best_b.get(v, -1.0)
                old_h = best_hops.get(v, 10 ** 9)

                improve = False

                # (1) better bottleneck
                if cand_b > old_b + 1e-9:
                    improve = True
                # (2) equal bottleneck -> fewer hops
                elif abs(cand_b - old_b) <= 1e-9 and cand_hops < old_h:
                    improve = True
                # (3) equal bottleneck and hops -> lexicographic path tie-break
                elif abs(cand_b - old_b) <= 1e-9 and cand_hops == old_h:
                    # Compare candidate path vs existing path lexicographically
                    cand_path = self._reconstruct_path_candidate(prev, src, u, v)
                    old_path = self._reconstruct_path_existing(prev, src, v)
                    if old_path is None or cand_path < old_path:
                        improve = True

                if improve:
                    best_b[v] = cand_b
                    best_hops[v] = cand_hops
                    prev[v] = u
                    heapq.heappush(heap, (-cand_b, cand_hops, v))

        if dst not in best_b:
            return None

        # Reconstruct final path
        path = [dst]
        cur = dst
        while cur != src:
            assert cur in prev, f"Widest-path reconstruction failed: missing predecessor for {cur}"
            cur = prev[cur]
            path.append(cur)

        path.reverse()
        return path

    def _reconstruct_path_existing(self, prev: Dict[int, int], src: int, node: int) -> Optional[List[int]]:
        """
        Reconstruct current best path to 'node' using 'prev'.
        Return None if reconstruction fails.
        """
        if node == src:
            return [src]
        if node not in prev:
            return None

        path = [node]
        cur = node
        guard = 0

        while cur != src:
            if cur not in prev:
                return None
            cur = prev[cur]
            path.append(cur)
            guard += 1
            assert guard < 10_000, "Path reconstruction guard triggered"

        path.reverse()
        return path

    def _reconstruct_path_candidate(self, prev: Dict[int, int], src: int, u: int, v: int) -> List[int]:
        """
        Build candidate path for tie-break comparison:
          path_to_u + [v]
        Assumes u is reachable (visited in algorithm), thus reconstructable.
        """
        path_u = self._reconstruct_path_existing(prev, src, u)
        assert path_u is not None, f"Candidate reconstruction failed: missing path to {u}"
        return path_u + [v]

    def _log_path_residuals(self, path: List[int], prefix: str = "Path residuals"):
        """
        Debug helper: print residual capacity per edge on the given path.

        IMPORTANT (bidirectional safety):
          For each hop (u,v) we log both residuals and the effective hop residual:
              hop_residual = min(res(u,v), res(v,u))
        """
        try:
            edges = list(zip(path[:-1], path[1:]))
        except Exception:
            return

        parts = []
        for (u, v) in edges:
            res_fwd = self._edge_residual_mbps(u, v)
            res_rev = self._edge_residual_mbps(v, u)

            if res_fwd is None or res_rev is None:
                parts.append(f"{u}<->{v}:MISSING")
                continue

            hop_res = min(float(res_fwd), float(res_rev))

            # Include both directions for clarity during demo/debugging
            parts.append(
                f"{u}->{v}:res={float(res_fwd):.2f} / {v}->{u}:res={float(res_rev):.2f} (hop={hop_res:.2f})"
            )

        self.logger.info("%s: %s", prefix, " | ".join(parts))
