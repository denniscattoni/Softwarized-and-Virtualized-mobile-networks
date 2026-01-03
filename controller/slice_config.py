# controller/slice_config.py

"""
Slice configuration and static QoS parameters.
This module contains only policy / configuration data.
"""


SLICES = {
    "latency": {
        # Traffic identification for the low-latency slice
        "match": {
            "ipv4_src": "10.0.0.2",
            "ipv4_dst": "10.0.0.3",
            "ip_proto": 6,              # TCP
            "tcp_dst": 5001,
        },

        "objective": "min_delay",

        # Maximum acceptable estimated latency for the path (in ms)
        "max_delay_ms": 30,

        "queue_id": 0,

        # Logical path endpoints at the switch level:
        # ingress = first switch where this slice enters the SDN domain,
        # egress  = last switch before the destination host.
        "ingress_switch": 3,
        "egress_switch": 4,

        "ingress_host_port": 1,  # s3-eth1 -> c2
        "egress_host_port": 1,   # s4-eth1 -> srv1

        # Current path (list of DPIDs); will be updated by the controller
        "current_path": None,
    },

    "throughput": {
        # Debug logging
        "log_metrics": True,

        # Traffic identification for the high-throughput slice (client -> VIP)
        "match": {
            "ipv4_src": "10.0.0.1",      # c1
            "ipv4_dst": "10.0.0.100",    # VIP
            "ip_proto": 6,               # TCP
            "tcp_dst": 8080,             # HTTP on 8080
        },

        "objective": "max_throughput",

        # Minimum acceptable bottleneck throughput (in Mbps) along the selected path.
        # The controller maximizes the bottleneck residual capacity and checks it against this threshold.
        "min_throughput_mbps": 6.0,

        # VIP / VMAC for SDN-based NAT and transparent service migration
        "vip_ip": "10.0.0.100",
        "vip_mac": "02:00:00:00:00:64",     # locally-administered unicast MAC

        # Client endpoint identity (used for L2 rewriting on reverse path)
        "client_ip": "10.0.0.1",
        "client_mac": "00:00:00:00:00:01",

        # Remote backend (srv2) identity
        "remote_backend": {
            "name": "srv2",
            "ip": "10.0.0.4",
            "mac": "00:00:00:00:00:04",
            "tcp_port": 8080,
            "egress_switch": 5,             # s5 is attached to srv2
            "egress_host_port": 1,          # s5-eth1 -> srv2 (host-facing)
        },

        # Closest backend (srv1) identity (used later for migration)
        "closest_backend": {
            "name": "srv1",
            "ip": "10.0.0.3",
            "mac": "00:00:00:00:00:03",
            "tcp_port": 8080,
            "egress_switch": 4,             # s4 is attached to srv1
            "egress_host_port": 1,          # s4-eth1 -> srv1 (host-facing)
        },

        # Ingress switch for the throughput slice (client-side edge)
        "ingress_switch": 2,                # s2 is attached to c1
        "ingress_host_port": 1,             # s2-eth1 -> c1 (host-facing)

        # Runtime state (updated by the controller)
        "active_backend": "remote",         # "remote" or "closest"
        "state": "NORMAL_REMOTE",           # NORMAL_REMOTE, NORMAL_CLOSEST, MIGRATING, DISCONNECTED
        "current_path": None,

        # NFV management plane (REST) for migration
        "service_manager_url": "http://127.0.0.1:9090",
        "migration_enabled": True,
    },
}
