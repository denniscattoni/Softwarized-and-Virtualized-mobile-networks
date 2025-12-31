# controller/graph_state.py

"""
TopologyGraph: wrapper around NetworkX to store the SDN topology and per-link metrics.

Architecture 2 notes:
  - The topology is initialized from a static model (LINK_PARAMS) in the controller.
  - Link up/down is handled by the controller via OFPPortStatus by calling remove_link()/add_link().
  - This module:
      * stores directed edges,
      * stores per-edge metrics (delay_ms, capacity_mbps, used_mbps),
      * provides shortest-path computation with a caller-provided weight function.

Design choice (Architecture 2):
  - We keep per-link metrics (link_info) even when a link goes DOWN.
    remove_link() removes only the edge from the graph, not its stored metrics.
    This preserves EWMA state across link flaps and avoids cold-start effects.
"""

from __future__ import annotations

import math
import heapq
import networkx as nx
from typing import Callable, Dict, Optional, Tuple, List, Any

WeightFn = Callable[[int, int, Dict[str, Any]], float]


class TopologyGraph:
    def __init__(self, ewma_beta: float = 0.2):
        self.G = nx.DiGraph()
        self.link_info: Dict[Tuple[int, int], Dict[str, Any]] = {}

        # EWMA smoothing factor (0 < beta <= 1)
        self.ewma_beta = float(ewma_beta)
        assert 0.0 < self.ewma_beta <= 1.0, f"ewma_beta must be in (0,1], got {self.ewma_beta}"

    # ------------------------------------------------------------------
    # Internal validation helpers (hard fail)
    # ------------------------------------------------------------------
    def _require_metrics(self, u: int, v: int) -> Dict[str, Any]:
        """
        Return metrics for (u,v) and HARD-FAIL if they are missing/invalid.
        """
        key = (u, v)
        assert key in self.link_info, f"Missing link_info for edge {u}->{v}"

        info = self.link_info[key]

        # Presence checks (no fallbacks)
        assert "delay_ms" in info, f"Missing 'delay_ms' for edge {u}->{v}"
        assert "capacity_mbps" in info, f"Missing 'capacity_mbps' for edge {u}->{v}"
        assert "used_mbps" in info, f"Missing 'used_mbps' for edge {u}->{v}"

        # Type/value checks
        delay = info["delay_ms"]
        cap = info["capacity_mbps"]
        used = info["used_mbps"]

        assert delay is not None, f"'delay_ms' is None for edge {u}->{v}"
        assert cap is not None, f"'capacity_mbps' is None for edge {u}->{v}"
        assert used is not None, f"'used_mbps' is None for edge {u}->{v}"

        delay_f = float(delay)
        cap_f = float(cap)
        used_f = float(used)

        assert math.isfinite(delay_f), f"'delay_ms' not finite for edge {u}->{v}: {delay_f}"
        assert math.isfinite(cap_f), f"'capacity_mbps' not finite for edge {u}->{v}: {cap_f}"
        assert math.isfinite(used_f), f"'used_mbps' not finite for edge {u}->{v}: {used_f}"

        assert delay_f >= 0.0, f"'delay_ms' must be >= 0 for edge {u}->{v}: {delay_f}"
        assert cap_f > 0.0, f"'capacity_mbps' must be > 0 for edge {u}->{v}: {cap_f}"
        assert used_f >= 0.0, f"'used_mbps' must be >= 0 for edge {u}->{v}: {used_f}"

        # Normalize back into the dict (optional but keeps consistency)
        info["delay_ms"] = delay_f
        info["capacity_mbps"] = cap_f
        info["used_mbps"] = used_f

        return info

    # ------------------------------------------------------------------
    # Topology maintenance
    # ------------------------------------------------------------------
    def add_link(self, src_dpid: int, dst_dpid: int, delay_ms: float, capacity_mbps: float):
        """
        Add (or re-add) a directed edge with explicit metrics.
        Preserve utilization estimates if present.
        """
        assert delay_ms is not None, f"add_link: delay_ms is None for {src_dpid}->{dst_dpid}"
        assert capacity_mbps is not None, f"add_link: capacity_mbps is None for {src_dpid}->{dst_dpid}"

        delay_f = float(delay_ms)
        cap_f = float(capacity_mbps)

        assert math.isfinite(delay_f) and delay_f >= 0.0, f"add_link: invalid delay_ms={delay_f} for {src_dpid}->{dst_dpid}"
        assert math.isfinite(cap_f) and cap_f > 0.0, f"add_link: invalid capacity_mbps={cap_f} for {src_dpid}->{dst_dpid}"

        self.G.add_edge(src_dpid, dst_dpid)

        key = (src_dpid, dst_dpid)
        prev = self.link_info.get(key, {})

        prev_used = prev.get("used_mbps", 0.0)
        prev_raw = prev.get("used_mbps_raw", 0.0)
        prev_ewma = prev.get("used_mbps_ewma", prev_used)

        self.link_info[key] = {
            "delay_ms": delay_f,
            "capacity_mbps": cap_f,
            "used_mbps": float(prev_used),
            "used_mbps_raw": float(prev_raw),
            "used_mbps_ewma": float(prev_ewma),
        }

    def remove_link(self, src_dpid: int, dst_dpid: int):
        """
        DOWN operation: remove only the edge from the graph, keep metrics in link_info.
        """
        if self.G.has_edge(src_dpid, dst_dpid):
            self.G.remove_edge(src_dpid, dst_dpid)

    def update_link_usage(self, src_dpid: int, dst_dpid: int, used_mbps: float):
        """
        Update utilization using EWMA. Hard-fail if link metrics are missing.
        """
        info = self._require_metrics(src_dpid, dst_dpid)

        x = float(used_mbps)
        assert math.isfinite(x) and x >= 0.0, f"update_link_usage: invalid used_mbps={x} for {src_dpid}->{dst_dpid}"

        beta = self.ewma_beta
        prev = info.get("used_mbps_ewma", None)
        ewma = x if prev is None else (beta * x + (1.0 - beta) * float(prev))

        info["used_mbps_raw"] = x
        info["used_mbps_ewma"] = ewma
        info["used_mbps"] = ewma  # expose only smoothed value to routing

    # ------------------------------------------------------------------
    # Path computation
    # ------------------------------------------------------------------
    def compute_latency_path(self, src_dpid: int, dst_dpid: int, weight_fn: Optional[WeightFn] = None) -> Optional[List[int]]:
        """
        Compute shortest path using Dijkstra.
        Hard-fail if any traversed edge lacks required metrics.
        """
        if src_dpid not in self.G or dst_dpid not in self.G:
            return None

        def _weight(u: int, v: int, _edge_attr: Dict[str, Any]) -> float:
            info = self._require_metrics(u, v)
            if weight_fn is None:
                return float(info["delay_ms"])
            w = float(weight_fn(u, v, info))
            assert math.isfinite(w), f"weight_fn returned non-finite weight for {u}->{v}: {w}"
            return w

        try:
            return nx.shortest_path(self.G, source=src_dpid, target=dst_dpid, weight=_weight)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None

    def estimate_path_cost(self, path: List[int], weight_fn: Optional[WeightFn] = None) -> float:
        """
        Sum cost over a given path. Hard-fail if any edge lacks required metrics.
        """
        if not path or len(path) < 2:
            return 0.0

        total = 0.0
        for u, v in zip(path[:-1], path[1:]):
            info = self._require_metrics(u, v)
            if weight_fn is None:
                total += float(info["delay_ms"])
            else:
                w = float(weight_fn(u, v, info))
                assert math.isfinite(w), f"weight_fn returned non-finite weight for {u}->{v}: {w}"
                total += w

        assert math.isfinite(total), f"estimate_path_cost produced non-finite total: {total}"
        return float(total)


    # ------------------------------------------------------------------
    # Widest path (max-min) computation for throughput slicing (BIDIRECTIONAL)
    # ------------------------------------------------------------------
    def estimate_path_bottleneck_bidirectional_mbps(self, path: List[int]) -> float:
        """
        Bottleneck residual capacity along a path, using bidirectional safety:

          hop_residual(u,v) = min(residual(u,v), residual(v,u))
          bottleneck(path)  = min hop_residual along the path

        This matches the reality of download-heavy traffic where reverse direction
        (backend -> client) can dominate.
        """
        if not path or len(path) < 2:
            return 0.0

        b = float("inf")
        for u, v in zip(path[:-1], path[1:]):
            # Hard-fail if any metrics are missing
            res_fwd = self._residual_capacity_mbps(u, v)
            res_rev = self._residual_capacity_mbps(v, u)
            hop_res = min(float(res_fwd), float(res_rev))
            if hop_res < b:
                b = hop_res

        return 0.0 if b == float("inf") else float(b)

    def compute_widest_path_bidirectional(self, src_dpid: int, dst_dpid: int) -> Optional[List[int]]:
        """
        Compute a widest path (max-min) between src and dst, using bidirectional safety.

        Objective:
          maximize bottleneck(path), where:
            hop_residual(u,v) = min(residual(u,v), residual(v,u))
            bottleneck(path)  = min hop_residual along path

        Tie-breaks (deterministic):
          1) higher bottleneck
          2) fewer hops
          3) lexicographically smaller node sequence
        """
        if src_dpid not in self.G or dst_dpid not in self.G:
            return None

        best_b: Dict[int, float] = {src_dpid: float("inf")}
        best_hops: Dict[int, int] = {src_dpid: 0}
        prev: Dict[int, int] = {}

        # max-heap via negative bottleneck
        pq = [(-best_b[src_dpid], best_hops[src_dpid], src_dpid)]
        visited = set()

        def _reconstruct(prev_map: Dict[int, int], src: int, node: int) -> Optional[List[int]]:
            if node == src:
                return [src]
            if node not in prev_map:
                return None
            p = [node]
            cur = node
            guard = 0
            while cur != src:
                if cur not in prev_map:
                    return None
                cur = prev_map[cur]
                p.append(cur)
                guard += 1
                assert guard < 10_000, "Path reconstruction guard triggered"
            p.reverse()
            return p

        while pq:
            neg_b, hops_u, u = heapq.heappop(pq)
            b_u = -neg_b

            if u in visited:
                continue
            visited.add(u)

            if u == dst_dpid:
                break

            nbrs = list(self.G.successors(u))
            nbrs.sort()

            for v in nbrs:
                # Bidirectional hop residual
                r1 = self._residual_capacity_mbps(u, v)
                r2 = self._residual_capacity_mbps(v, u)
                hop_res = min(float(r1), float(r2))

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
                # (3) equal bottleneck and hops -> lexicographic tie-break
                elif abs(cand_b - old_b) <= 1e-9 and cand_hops == old_h:
                    cand_path = _reconstruct(prev, src_dpid, u)
                    old_path = _reconstruct(prev, src_dpid, v)
                    if cand_path is not None:
                        cand_path = cand_path + [v]
                        if old_path is None or cand_path < old_path:
                            improve = True

                if improve:
                    best_b[v] = cand_b
                    best_hops[v] = cand_hops
                    prev[v] = u
                    heapq.heappush(pq, (-cand_b, cand_hops, v))

        if dst_dpid not in best_b:
            return None

        # Reconstruct final path
        path = [dst_dpid]
        cur = dst_dpid
        while cur != src_dpid:
            assert cur in prev, f"Widest-path reconstruction failed: missing predecessor for {cur}"
            cur = prev[cur]
            path.append(cur)
        path.reverse()
        return path


    def _residual_capacity_mbps(self, u: int, v: int) -> float:
        """
        Residual capacity = capacity - used (EWMA-smoothed).
        Hard-fail if metrics are missing/invalid.
        """
        info = self._require_metrics(u, v)
        cap = float(info["capacity_mbps"])
        used = float(info["used_mbps"])

        res = cap - used
        if res < 0.0:
            res = 0.0
        return float(res)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------
    def has_link(self, src_dpid: int, dst_dpid: int) -> bool:
        return self.G.has_edge(src_dpid, dst_dpid)

    def get_link_metrics(self, src_dpid: int, dst_dpid: int) -> Optional[Dict[str, Any]]:
        return self.link_info.get((src_dpid, dst_dpid))
