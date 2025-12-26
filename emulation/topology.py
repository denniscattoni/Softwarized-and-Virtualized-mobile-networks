#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info


class SlicingTopology(Topo):
    """
    Topology:

        c1 ---- s2 ---\     /--- s3 ---- c2
                 .     \   /      .
                 .      s1        .
                 .     /   \      .
        srv1 -- s4 ---/     \--- s5 -- srv2

    """

    # TODO: add packet loss and make it more "real"
    def build(self):
        # Hosts
        c1 = self.addHost('c1', ip='10.0.0.1/24')
        c2 = self.addHost('c2', ip='10.0.0.2/24')
        srv1 = self.addHost('srv1', ip='10.0.0.3/24')
        srv2 = self.addHost('srv2', ip='10.0.0.4/24')

        # Switches
        s1 = self.addSwitch('s1', cls=OVSSwitch, protocols='OpenFlow13')
        s2 = self.addSwitch('s2', cls=OVSSwitch, protocols='OpenFlow13')
        s3 = self.addSwitch('s3', cls=OVSSwitch, protocols='OpenFlow13')
        s4 = self.addSwitch('s4', cls=OVSSwitch, protocols='OpenFlow13')
        s5 = self.addSwitch('s5', cls=OVSSwitch, protocols='OpenFlow13')

        # Host-to-switch links
        self.addLink(c1, s2)
        self.addLink(c2, s3)
        self.addLink(srv1, s4)
        self.addLink(srv2, s5)

        # Core links (required)
        self.addLink(s2, s1, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s3, s1, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s4, s1, cls=TCLink, bw=4, delay='3ms')
        self.addLink(s5, s1, cls=TCLink, bw=10, delay='10ms')

        # Cross-links
        self.addLink(s2, s4, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s3, s5, cls=TCLink, bw=10, delay='5ms')


def run():
    topo = SlicingTopology()

    net = Mininet(
        topo=topo,
        controller=None,  # use remote controller (RYU)
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True
    )

    # Connection to remote RYU controller
    ryu = net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6633
    )

    net.start()
    info("\n*** Network started.\n")
    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
