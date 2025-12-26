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
        # (this is a logical threshold: will compare the sum of per-link delays against it).
        "max_delay_ms": 30,

        "queue_id": 0,

        # Logical path endpoints at the switch level:
        # ingress = first switch where this slice enters the SDN domain,
        # egress  = last switch before the destination host.
        "ingress_switch": 3,
        "egress_switch": 4,

        #"ingress_host_port": 3,  # s3-eth3 -> c2
        #"egress_host_port": 3,   # s4-eth3 -> srv1
        "ingress_host_port": 1,  # s3-eth1 -> c2
        "egress_host_port": 1,   # s4-eth1 -> srv1

        # Current path (list of DPIDs); will be updated by the controller
        "current_path": None,
    }
}
