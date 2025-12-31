#!/usr/bin/env python3

"""
Static Mininet/Containernet topology for the slicing + NFV migration demo.

Key design choice:
- Client nodes (c1, c2) are plain Mininet Hosts.
- Server nodes (srv1, srv2) are DockerHost nodes (Containernet) running the VNF image.
  The VNF lifecycle (nginx start/stop) is controlled at runtime by the management plane.

Topology:

    c1 ---- s2 ---\     /--- s3 ---- c2
             .     \   /      .
             .      s1        .
             .     /   \      .
    srv1 -- s4 ---/     \--- s5 -- srv2
"""

from mininet.topo import Topo
from mininet.link import TCLink
from mininet.node import OVSSwitch, Host

from comnetsemu.node import DockerHost


class SlicingTopology(Topo):
    """
    Topology used by emulation/run_demo.py.

    IMPORTANT:
    - srv1/srv2 are DockerHost nodes using a custom image (stream_vnf:latest).
    - c1/c2 are plain Mininet hosts (no Docker involved).
    """

    def build(self):
        # ------------------------------------------------------------
        # Hosts
        # ------------------------------------------------------------
        # Plain hosts (clients)
        c1 = self.addHost("c1", cls=Host, ip="10.0.0.1/24")
        c2 = self.addHost("c2", cls=Host, ip="10.0.0.2/24")

        # DockerHost nodes (edge backends)
        #
        # NOTE:
        # - dimage must exist locally (built by Makefile).
        # - docker_args MUST be a dict (avoid NoneType bugs).
        srv1 = self.addHost(
            "srv1",
            cls=DockerHost,
            ip="10.0.0.3/24",
            dimage="stream_vnf:latest",
            docker_args={},
        )
        srv2 = self.addHost(
            "srv2",
            cls=DockerHost,
            ip="10.0.0.4/24",
            dimage="stream_vnf:latest",
            docker_args={},
        )

        # Switches
        s1 = self.addSwitch("s1", cls=OVSSwitch, protocols="OpenFlow13")
        s2 = self.addSwitch("s2", cls=OVSSwitch, protocols="OpenFlow13")
        s3 = self.addSwitch("s3", cls=OVSSwitch, protocols="OpenFlow13")
        s4 = self.addSwitch("s4", cls=OVSSwitch, protocols="OpenFlow13")
        s5 = self.addSwitch("s5", cls=OVSSwitch, protocols="OpenFlow13")

        # Host-to-switch links
        self.addLink(c1, s2)
        self.addLink(c2, s3)
        self.addLink(srv1, s4)
        self.addLink(srv2, s5)

        # Core links
        self.addLink(s2, s1, cls=TCLink, bw=10, delay="5ms")
        self.addLink(s3, s1, cls=TCLink, bw=10, delay="5ms")
        self.addLink(s4, s1, cls=TCLink, bw=4, delay="3ms")
        self.addLink(s5, s1, cls=TCLink, bw=10, delay="10ms")

        # Cross-links
        self.addLink(s2, s4, cls=TCLink, bw=10, delay="5ms")
        self.addLink(s3, s5, cls=TCLink, bw=10, delay="5ms")
