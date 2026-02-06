# controller/flow_utils.py

"""
Utility functions for building OpenFlow matches and installing/removing flows.

Architecture:
- Topology is static (known a priori), but flow programming is unchanged.
- We use cookies to delete ONLY the flows installed by the slicing app.

Design goals:
- Keep matching consistent with slice_config.py.
- Avoid silent behavior: if you forget a cookie, fail fast.
"""

from typing import Optional


IPV4_ETH_TYPE = 0x0800
COOKIE_MASK_ALL = 0xFFFFFFFFFFFFFFFF


# -------------------------------------------------------------------
# Match builders
# -------------------------------------------------------------------
def build_match_for_slice(parser, slice_conf):
    """
    Build the FORWARD match for a slice (client -> server).

    Expected slice_conf structure:
      slice_conf["match"] may include:
        - ipv4_src (optional but recommended)
        - ipv4_dst (optional but recommended)
        - ip_proto (optional, but required if you use tcp_dst / tcp_src)
        - tcp_dst (optional, valid only if ip_proto == 6)
        - tcp_src (optional, valid only if ip_proto == 6)
    """
    mf = slice_conf.get("match", {})
    kwargs = {"eth_type": IPV4_ETH_TYPE}

    if "ipv4_src" in mf:
        kwargs["ipv4_src"] = mf["ipv4_src"]
    if "ipv4_dst" in mf:
        kwargs["ipv4_dst"] = mf["ipv4_dst"]
    if "ip_proto" in mf:
        kwargs["ip_proto"] = mf["ip_proto"]

    # TCP ports only if TCP
    if "tcp_dst" in mf or "tcp_src" in mf:
        proto = mf.get("ip_proto", None)
        assert proto == 6, f"tcp_* fields require ip_proto=6 (TCP). Got ip_proto={proto}"
        if "tcp_dst" in mf:
            kwargs["tcp_dst"] = mf["tcp_dst"]
        if "tcp_src" in mf:
            kwargs["tcp_src"] = mf["tcp_src"]

    return parser.OFPMatch(**kwargs)


def build_reverse_match_for_slice(parser, slice_conf):
    """
    Build the REVERSE match for a slice (server -> client).

    Reverse logic:
      - swap ipv4_src and ipv4_dst
      - if forward used tcp_dst = server_port, reverse typically matches tcp_src = server_port

    NOTE:
      This assumes "server port is fixed" (iperf server port) and the client port is ephemeral.
    """
    mf = slice_conf.get("match", {})
    kwargs = {"eth_type": IPV4_ETH_TYPE}

    if "ipv4_src" in mf:
        kwargs["ipv4_dst"] = mf["ipv4_src"]
    if "ipv4_dst" in mf:
        kwargs["ipv4_src"] = mf["ipv4_dst"]
    if "ip_proto" in mf:
        kwargs["ip_proto"] = mf["ip_proto"]

    # Reverse TCP: match tcp_src = forward tcp_dst (server source port)
    if "tcp_dst" in mf:
        proto = mf.get("ip_proto", None)
        assert proto == 6, f"tcp_dst requires ip_proto=6 (TCP). Got ip_proto={proto}"
        kwargs["tcp_src"] = mf["tcp_dst"]

    return parser.OFPMatch(**kwargs)


# -------------------------------------------------------------------
# Action builder
# -------------------------------------------------------------------
def build_actions_for_slice(parser, out_port: int, slice_conf):
    """
    Build actions list for the slice:
      - optional SetQueue(queue_id)
      - Output(out_port)
    """
    assert out_port is not None, "out_port must not be None"

    actions = []
    queue_id = slice_conf.get("queue_id", None)

    # Only set queue if user explicitly configured it
    if queue_id is not None:
        actions.append(parser.OFPActionSetQueue(int(queue_id)))

    actions.append(parser.OFPActionOutput(int(out_port)))
    return actions


# -------------------------------------------------------------------
# Flow programming helpers
# -------------------------------------------------------------------
def add_flow(
    datapath,
    priority: int,
    match,
    actions,
    idle_timeout: int = 0,
    hard_timeout: int = 0,
    cookie: Optional[int] = None,
):
    """
    Add a flow entry with APPLY_ACTIONS instruction.

    Cookie policy:
      - cookie MUST be provided (non-None) so flows can be removed deterministically.
      - This prevents stale flows when paths change.
    """
    assert cookie is not None, "add_flow: cookie must be provided (do not rely on cookie=0)"

    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser

    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

    mod = parser.OFPFlowMod(
        datapath=datapath,
        cookie=int(cookie),
        priority=int(priority),
        match=match,
        instructions=inst,
        idle_timeout=int(idle_timeout),
        hard_timeout=int(hard_timeout),
    )
    datapath.send_msg(mod)


def delete_flows_for_cookie(datapath, cookie: int, cookie_mask: int = COOKIE_MASK_ALL, table_id: Optional[int] = None):
    """
    Delete flows that match the given cookie (masked).

    This is the preferred method to remove only the flows installed by your app.

    table_id:
      - if None: applies to all tables
      - else: restrict deletion to a specific table
    """
    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser

    kwargs = dict(
        datapath=datapath,
        command=ofproto.OFPFC_DELETE,
        out_port=ofproto.OFPP_ANY,
        out_group=ofproto.OFPG_ANY,
        cookie=int(cookie),
        cookie_mask=int(cookie_mask),
        match=parser.OFPMatch(),
    )

    if table_id is not None:
        kwargs["table_id"] = int(table_id)

    mod = parser.OFPFlowMod(**kwargs)
    datapath.send_msg(mod)


def delete_flows_for_match(datapath, match, table_id: Optional[int] = None):
    """
    Optional helper: delete flows that match a match structure (broad).
    Use carefully: match-only deletes can remove flows from other apps.
    """
    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser

    kwargs = dict(
        datapath=datapath,
        command=ofproto.OFPFC_DELETE,
        out_port=ofproto.OFPP_ANY,
        out_group=ofproto.OFPG_ANY,
        match=match,
    )
    if table_id is not None:
        kwargs["table_id"] = int(table_id)

    mod = parser.OFPFlowMod(**kwargs)
    datapath.send_msg(mod)
