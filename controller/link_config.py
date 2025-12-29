# controller/link_config.py

"""
Static logical topology configuration (Architecture 2).

This file does NOT perform discovery.
It describes the logical topology that the controller considers valid
for QoS routing and network slicing.

Key assumption of Architecture 2:
- the physical topology is known a priori (controlled lab / demo),
- the controller only reacts to runtime events (PortStatus, PortStats)
  to update the state of already-known links.
"""

# -------------------------------------------------------------------
# LINK_PARAMS
# -------------------------------------------------------------------
# Logical description of inter-switch links (directed graph).
#
# Each entry (src_dpid, dst_dpid) represents:
#   - a directed edge in the NetworkX graph
#   - with static baseline metrics:
#       * delay_ms         -> estimated propagation latency
#       * capacity_mbps    -> nominal link capacity
#
# Notes:
# - Links are explicitly BIDIRECTIONAL, so both (u, v) and (v, u)
#   are present.
# - This is intentional: routing is performed on a directed graph.
#
# This dictionary is used to:
#   - initialize the logical graph (TopologyGraph)
#   - restore a link when it transitions back to UP

LINK_PARAMS = {
    (2, 1): {"delay_ms": 5, "capacity_mbps": 12},
    (1, 2): {"delay_ms": 5, "capacity_mbps": 12},

    (3, 1): {"delay_ms": 5, "capacity_mbps": 12},
    (1, 3): {"delay_ms": 5, "capacity_mbps": 12},

    (4, 1): {"delay_ms": 3, "capacity_mbps": 4},
    (1, 4): {"delay_ms": 3, "capacity_mbps": 4},

    (5, 1): {"delay_ms": 10, "capacity_mbps": 7},
    (1, 5): {"delay_ms": 10, "capacity_mbps": 7},

    (2, 4): {"delay_ms": 5, "capacity_mbps": 10},
    (4, 2): {"delay_ms": 5, "capacity_mbps": 10},

    (3, 5): {"delay_ms": 5, "capacity_mbps": 7},
    (5, 3): {"delay_ms": 5, "capacity_mbps": 7},
}

# -------------------------------------------------------------------
# NEXT_HOP_PORT
# -------------------------------------------------------------------
# Static mapping used for flow installation.
#
# Each entry:
#     (current_dpid, next_dpid) -> out_port
#
# It answers the question:
#   "Which output port must I use on switch current_dpid
#    to reach switch next_dpid?"
#
# This mapping:
#   - is used ONLY when installing flows along the computed path
#     (slice_qos_app._install_latency_slice_flows)
#   - is not used for link up/down detection

NEXT_HOP_PORT = {
    # s3 <-> s1
    (3, 1): 2,   # s3-eth2
    (1, 3): 2,   # s1-eth2

    # s1 <-> s4
    (1, 4): 3,   # s1-eth3
    (4, 1): 2,   # s4-eth2

    # s1 <-> s5
    (1, 5): 4,   # s1-eth4
    (5, 1): 2,   # s5-eth2

    # s1 <-> s2
    (1, 2): 1,   # s1-eth1
    (2, 1): 2,   # s2-eth2

    # s2 <-> s4
    (2, 4): 3,   # s2-eth3
    (4, 2): 3,   # s4-eth3

    # s3 <-> s5
    (3, 5): 3,   # s3-eth3
    (5, 3): 3,   # s5-eth3
}


# -------------------------------------------------------------------
# PORT_TO_NEIGHBOR
# -------------------------------------------------------------------
# Reverse mapping used to interpret runtime events
# (OFPPortStatus, OFPPortStats).
#
# Each entry:
#     (dpid, port_no) -> neighbor_dpid
#
# It answers the question:
#   "This physical port on this switch connects to WHICH other switch?"
#
# This mapping is used to:
#   - translate EventOFPPortStatus (UP/DOWN) into:
#         * logical link (dpid -> neighbor) DOWN / UP
#   - attribute PortStats (throughput) to the correct logical link
#
# CRITICAL NOTE:
# - ONLY inter-switch ports must be listed here
# - Host-facing ports MUST NOT appear
# - Otherwise host traffic would be interpreted as link failure,
#   causing flapping and continuous recomputation

PORT_TO_NEIGHBOR = {
    # s1: inter-switch ports
    (1, 1): 2,   # s1-eth1 -> s2
    (1, 2): 3,   # s1-eth2 -> s3
    (1, 3): 4,   # s1-eth3 -> s4
    (1, 4): 5,   # s1-eth4 -> s5

    (2, 2): 1,   # s2-eth2 -> s1
    (2, 3): 4,   # s2-eth3 -> s4

    (3, 2): 1,   # s3-eth2 -> s1
    (3, 3): 5,   # s3-eth3 -> s5

    (4, 2): 1,   # s4-eth2 -> s1
    (4, 3): 2,   # s4-eth3 -> s2

    (5, 2): 1,   # s5-eth2 -> s1
    (5, 3): 3,   # s5-eth3 -> s3
}
