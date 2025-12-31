# controller/throughput_slice.py

"""
ThroughputSliceManager: slice-specific logic for the high-throughput slice.

This module MUST NOT define a RyuApp.
It contains only policy/decision logic and flow programming for the "throughput" slice.

Design goals:
- Deterministic and debuggable (hard-fail on missing config).
- Max-min (widest-path) routing using bottleneck residual throughput.
- SDN VIP NAT so the client targets a stable VIP regardless of backend location.

Pipeline:
- Step 1: VIP + SDN NAT flows
- Step 2: Widest-path + QoS check (route-first)
- Step 3: Service migration via REST (service_manager.py)
"""

from __future__ import annotations

from typing import Dict, List, Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flow_utils import build_match_for_slice, add_flow, delete_flows_for_cookie

COOKIE_THROUGHPUT = 0xB2_000001
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


class ThroughputSliceManager:
    """
    Policy:
      - Bootstrap: compute and install widest path toward active backend (default remote=srv2).
      - Operational: recompute only when:
          * QoS violation on current path, or
          * link-down affects current path.

    Recovery-safe:
      - If no path exists: current_path=None, delete flows, state=DISCONNECTED.
      - On link-up: orchestrator triggers recompute only if DISCONNECTED.

    Step 3 (migration):
      - If active backend is REMOTE (srv2) and even the BEST possible path cannot satisfy QoS,
        trigger REST switch to CLOSEST (srv1), then reinstall flows toward srv1 (VIP unchanged).
      - No cooldown/streak: migration is immediate and one-way in this demo (no auto migrate back).
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
        assert "ingress_host_port" in self.slice_conf, "ThroughputSliceManager: missing ingress_host_port"
        assert "min_throughput_mbps" in self.slice_conf, "ThroughputSliceManager: missing min_throughput_mbps"
        assert "remote_backend" in self.slice_conf, "ThroughputSliceManager: missing remote_backend"
        assert "closest_backend" in self.slice_conf, "ThroughputSliceManager: missing closest_backend"
        assert "vip_ip" in self.slice_conf and "vip_mac" in self.slice_conf, "ThroughputSliceManager: missing VIP params"
        assert "client_ip" in self.slice_conf and "client_mac" in self.slice_conf, "ThroughputSliceManager: missing client identity"

        # Step 3 REST API config
        assert "service_manager_url" in self.slice_conf, "ThroughputSliceManager: missing service_manager_url"
        assert "migration_enabled" in self.slice_conf, "ThroughputSliceManager: missing migration_enabled"

    # ------------------------------------------------------------------
    # Public entry points used by the orchestrator
    # ------------------------------------------------------------------
    def recompute_path(self, reason: str = "unspecified"):
        """
        Compute and (re)install widest-path toward the ACTIVE backend.

        Widest-path objective:
          maximize bottleneck residual capacity, using bidirectional safety:
            hop_residual(u,v) = min(residual(u,v), residual(v,u))

        If no path:
          - if active is REMOTE and migration is enabled -> try migrate to CLOSEST and recompute
          - else DISCONNECTED (clear flows)
        """
        backend = self._get_active_backend_conf()
        ingress = int(self.slice_conf["ingress_switch"])
        egress = int(backend["egress_switch"])
        thr_min = float(self.slice_conf["min_throughput_mbps"])

        path = self.topo.compute_widest_path_bidirectional(ingress, egress)

        # -------------------- NO PATH --------------------
        if path is None:
            if self._is_remote_active() and self._migration_enabled():
                self.logger.warning(
                    "Throughput: no path to REMOTE backend (%s). Trying migration to CLOSEST. [reason=%s]",
                    backend.get("name", "?"), reason,
                )
                if self._migrate_to("closest", reason=f"no_path_to_remote:{reason}"):
                    return self.recompute_path(reason="after_migration_no_path")

            self.logger.warning(
                "Throughput DISCONNECTED: no path from %s to %s. Clearing current_path and deleting slice flows. "
                "[backend=%s reason=%s]",
                ingress, egress, backend.get("name", "?"), reason,
            )
            self.slice_conf["current_path"] = None
            self.slice_conf["state"] = "DISCONNECTED"
            self.delete_flows()
            return

        bottleneck = self.topo.estimate_path_bottleneck_bidirectional_mbps(path)

        self.logger.info(
            "Throughput recompute: selected path=%s bottleneck=%.2f Mbps (min=%.2f Mbps) backend=%s state=%s [reason=%s]",
            path, bottleneck, thr_min, backend.get("name", "?"), self.slice_conf.get("state", "?"), reason
        )

        # -------------------- BEST PATH STILL UNSATISFIED --------------------
        if bottleneck < thr_min:
            self.logger.warning(
                "Throughput: BEST path to backend=%s violates QoS (bottleneck=%.2f < %.2f).",
                backend.get("name", "?"), bottleneck, thr_min
            )
            self._log_path_residuals(path, prefix="Throughput best-path residuals (QoS unsatisfied)")

            # Immediate migration trigger only when REMOTE is active
            if self._is_remote_active() and self._migration_enabled():
                self.logger.warning("Throughput: triggering migration to CLOSEST (no cooldown).")
                if self._migrate_to("closest", reason=f"qos_unsatisfied_best_path:{reason}"):
                    return self.recompute_path(reason="after_migration_qos_unsatisfied")

            # If closest already active (or migration disabled), keep best route anyway.

        # -------------------- INSTALL FLOWS IF PATH CHANGED --------------------
        old_path = self.slice_conf.get("current_path")
        if old_path == path:
            return

        self._log_path_residuals(path, prefix="Throughput installing new path")
        self.delete_flows()
        self.install_flows(path, backend)
        self.slice_conf["current_path"] = path

        self.slice_conf["state"] = "NORMAL_CLOSEST" if self.slice_conf.get("active_backend") == "closest" else "NORMAL_REMOTE"

    def check_qos(self):
        """
        Evaluate QoS on the CURRENT path; if violated -> recompute.

        QoS: bottleneck_residual_mbps(path) >= min_throughput_mbps
        (bidirectional safety metric).
        """

        # Don't poll ServiceManager until all switches registered
        if self.slice_conf.get("current_path") is None:
            return

        # Intercept if someone performed manual migration of the service and resync
        # If a manual placement change is detected, this will:
        #   - update active_backend/state
        #   - delete old flows
        #   - recompute/install new flows
        if self._sync_backend_from_service_manager():
            return

        path = self.slice_conf.get("current_path")
        if not path:
            return

        thr_min = float(self.slice_conf["min_throughput_mbps"])
        b = self.topo.estimate_path_bottleneck_bidirectional_mbps(path)

        if b >= thr_min:
            return

        self.logger.warning(
            "Throughput QoS VIOLATION: path=%s bottleneck=%.2f Mbps < %.2f Mbps. Recomputing.",
            path, b, thr_min
        )
        self._log_path_residuals(path, prefix="Throughput QoS violation on current path")
        self.recompute_path(reason="qos_violation")

    def on_path_affected(self, reason: str = "path_affected"):
        self.logger.warning("Current throughput path affected. Recomputing. [reason=%s]", reason)
        self.recompute_path(reason=reason)

    def delete_flows(self):
        for _, dp in self.datapaths.items():
            delete_flows_for_cookie(dp, cookie=self.cookie, cookie_mask=COOKIE_MASK_ALL)

    # ------------------------------------------------------------------
    # Flow programming (VIP NAT + routing)
    # ------------------------------------------------------------------
    def install_flows(self, path: List[int], backend_conf: Dict[str, Any]):
        """
        Install bidirectional slice rules with VIP NAT:

          FORWARD:  c1 -> VIP:8080  (DNAT VIP->backend on backend edge switch)
          REVERSE:  backend -> c1   (SNAT backend->VIP on ingress edge switch)

        IMPORTANT:
          reverse match includes backend_ip, so after migration flows MUST be reinstalled.
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
            fwd_match = build_match_for_slice(parser, self.slice_conf)

            if dpid == egress_switch:
                actions = [
                    parser.OFPActionSetField(ipv4_dst=backend_ip),
                    parser.OFPActionSetField(eth_dst=backend_mac),
                    parser.OFPActionOutput(int(egress_host_port)),
                ]
                add_flow(dp, priority=prio, match=fwd_match, actions=actions,
                         idle_timeout=0, hard_timeout=0, cookie=self.cookie)
                continue

            if idx >= len(path) - 1:
                continue

            next_dpid = path[idx + 1]
            out_port = self.next_hop_port.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s", dpid, next_dpid)
                continue

            actions = [parser.OFPActionOutput(int(out_port))]
            add_flow(dp, priority=prio, match=fwd_match, actions=actions,
                     idle_timeout=0, hard_timeout=0, cookie=self.cookie)

        # ---------- REVERSE (backend -> client) ----------
        rev_path = list(reversed(path))
        for idx, dpid in enumerate(rev_path):
            dp = self.datapaths.get(dpid)
            if dp is None:
                continue

            parser = dp.ofproto_parser
            rev_match = self._build_reverse_match_for_backend(parser, backend_ip, backend_tcp_port, client_ip)

            if dpid == ingress:
                actions = [
                    parser.OFPActionSetField(ipv4_src=vip_ip),
                    parser.OFPActionSetField(eth_src=vip_mac),
                    parser.OFPActionSetField(eth_dst=client_mac),
                    parser.OFPActionOutput(int(ingress_host_port)),
                ]
                add_flow(dp, priority=prio, match=rev_match, actions=actions,
                         idle_timeout=0, hard_timeout=0, cookie=self.cookie)
                continue

            if idx >= len(rev_path) - 1:
                continue

            next_dpid = rev_path[idx + 1]
            out_port = self.next_hop_port.get((dpid, next_dpid))
            if out_port is None:
                self.logger.warning("No NEXT_HOP_PORT mapping for %s -> %s (reverse)", dpid, next_dpid)
                continue

            actions = [parser.OFPActionOutput(int(out_port))]
            add_flow(dp, priority=prio, match=rev_match, actions=actions,
                     idle_timeout=0, hard_timeout=0, cookie=self.cookie)

    # ------------------------------------------------------------------
    # Step 3: Migration helpers
    # ------------------------------------------------------------------
    def _migration_enabled(self) -> bool:
        return bool(self.slice_conf.get("migration_enabled", False))

    def _is_remote_active(self) -> bool:
        return self.slice_conf.get("active_backend", "remote") == "remote"

    def _migrate_to(self, target: str, reason: str) -> bool:
        """
        Trigger NFV migration via REST and update local state.

        target: "closest" or "remote"
        """
        assert target in ("closest", "remote"), f"Throughput: invalid migration target={target}"

        if self.slice_conf.get("active_backend") == target:
            return True

        to_backend = "srv1" if target == "closest" else "srv2"

        self.logger.warning(
            "MIGRATION: switching service to %s (target=%s). VIP unchanged. [reason=%s]",
            to_backend, target, reason
        )

        self.slice_conf["state"] = "MIGRATING"

        ok = self._rest_switch_backend(to_backend)
        if not ok:
            self.logger.warning("MIGRATION FAILED: REST switch to %s failed.", to_backend)
            self.slice_conf["state"] = "NORMAL_CLOSEST" if self.slice_conf.get("active_backend") == "closest" else "NORMAL_REMOTE"
            return False

        self.slice_conf["active_backend"] = target
        self.slice_conf["state"] = "NORMAL_CLOSEST" if target == "closest" else "NORMAL_REMOTE"

        self.logger.warning("MIGRATION OK: active_backend=%s (REST switched to %s)", target, to_backend)
        return True

    def _rest_switch_backend(self, to_backend: str) -> bool:
        """
        POST /service/switch?to=srv1|srv2
        Stdlib only (no requests).
        """
        base = str(self.slice_conf["service_manager_url"]).rstrip("/")
        qs = urlencode({"to": to_backend})
        url = f"{base}/service/switch?{qs}"

        try:
            req = Request(url, method="POST")
            with urlopen(req, timeout=2) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
                if resp.status != 200:
                    self.logger.warning("ServiceManager REST error: status=%s body=%s", resp.status, body)
                    return False
                self.logger.info("ServiceManager REST: %s", body)
                return True
        except Exception as e:
            self.logger.warning("ServiceManager REST exception: %s", e)
            return False

    def _sync_backend_from_service_manager(self) -> bool:
        """
        Detect manual service placement changes by polling ServiceManager /service/status.

        If placement differs from slice_conf['active_backend'], reconcile by:
          - updating active_backend/state
          - clearing current_path
          - deleting old flows
          - recomputing + installing new flows toward the detected backend

        Returns True if a change was detected and handled (i.e., we triggered a resync).
        """

        base = str(self.slice_conf["service_manager_url"]).rstrip("/")
        url = f"{base}/service/status"

        # Fetch status
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=2) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
                status = getattr(resp, "status", None)
                if status is not None and int(status) != 200:
                    self.logger.warning("ServiceManager status error: status=%s body=%s", status, body)
                    return False
        except Exception as e:
            self.logger.warning("ServiceManager status exception: %s", e)
            return False

        # Expected body example:
        #   "srv1=ON srv2=OFF"
        srv1_on = "srv1=ON" in body
        srv2_on = "srv2=ON" in body

        # Ambiguous / invalid states
        # - both OFF: nothing to serve; don't thrash flows here
        # - both ON: ambiguous placement (could be transitional); avoid oscillations
        if srv1_on and srv2_on:
            self.logger.warning(
                "ServiceManager reports BOTH backends ON (ambiguous): '%s'. Skipping sync.", body
            )
            return False

        if (not srv1_on) and (not srv2_on):
            self.logger.warning(
                "ServiceManager reports BOTH backends OFF (no service): '%s'. Skipping sync.", body
            )
            return False

        # Exactly one ON -> infer placement
        detected = "closest" if srv1_on else "remote"
        current = self.slice_conf.get("active_backend", "remote")

        if detected == current:
            return False

        # Reconcile controller state + dataplane
        self.logger.warning(
            "MANUAL PLACEMENT DETECTED from ServiceManager: %s (was %s). Reconciling flows.",
            detected, current
        )

        self.slice_conf["active_backend"] = detected
        self.slice_conf["state"] = "NORMAL_CLOSEST" if detected == "closest" else "NORMAL_REMOTE"

        # Clear dataplane state and force a clean reinstall toward the new backend
        self.slice_conf["current_path"] = None
        self.recompute_path(reason="manual_switch_detected")
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_active_backend_conf(self) -> Dict[str, Any]:
        ab = self.slice_conf.get("active_backend", "remote")
        if ab == "remote":
            return self.slice_conf["remote_backend"]
        if ab == "closest":
            return self.slice_conf["closest_backend"]
        raise AssertionError(f"ThroughputSliceManager: invalid active_backend={ab}")

    def _build_reverse_match_for_backend(self, parser, backend_ip: str, backend_port: int, client_ip: str):
        return parser.OFPMatch(
            eth_type=0x0800,
            ipv4_src=str(backend_ip),
            ipv4_dst=str(client_ip),
            ip_proto=6,
            tcp_src=int(backend_port),
        )

    def _log_path_residuals(self, path: List[int], prefix: str = "Path residuals"):
        if not self.slice_conf.get("log_metrics", False):
            return

        parts = []
        for (u, v) in zip(path[:-1], path[1:]):
            m_fwd = self.topo.get_link_metrics(u, v)
            m_rev = self.topo.get_link_metrics(v, u)
            if m_fwd is None or m_rev is None:
                parts.append(f"{u}<->{v}:MISSING")
                continue

            res_fwd = float(m_fwd["capacity_mbps"]) - float(m_fwd["used_mbps"])
            res_rev = float(m_rev["capacity_mbps"]) - float(m_rev["used_mbps"])
            if res_fwd < 0.0:
                res_fwd = 0.0
            if res_rev < 0.0:
                res_rev = 0.0

            hop_res = min(res_fwd, res_rev)
            parts.append(f"{u}->{v}:res={res_fwd:.2f} / {v}->{u}:res={res_rev:.2f} (hop={hop_res:.2f})")

        self.logger.info("%s: %s", prefix, " | ".join(parts))
