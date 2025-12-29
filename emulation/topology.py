# emulation/topology.py

#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel, info


VIP_IP = "10.0.0.100"
VIP_MAC = "02:00:00:00:00:64"
HTTP_PORT = 8080


class SlicingTopology(Topo):
    """
    Topology:

        c1 ---- s2 ---\     /--- s3 ---- c2
                 .     \   /      .
                 .      s1        .
                 .     /   \      .
        srv1 -- s4 ---/     \--- s5 -- srv2

    """

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
        self.addLink(s2, s1, cls=TCLink, bw=12, delay='5ms')
        self.addLink(s3, s1, cls=TCLink, bw=12, delay='5ms')
        self.addLink(s4, s1, cls=TCLink, bw=4, delay='3ms')
        self.addLink(s5, s1, cls=TCLink, bw=7, delay='10ms')

        # Cross-links
        self.addLink(s2, s4, cls=TCLink, bw=10, delay='5ms')
        self.addLink(s3, s5, cls=TCLink, bw=7, delay='5ms')


def _start_demo_http_server(host, port: int):
    """
    Minimal demo service:
      - serves a dummy "video" file via HTTP on the given port
      - multithreaded: enables real parallel downloads (curl &)
      - used to validate VIP NAT and routing logic before container migration
    """
    host.cmd("mkdir -p /tmp/streaming_demo")
    host.cmd("dd if=/dev/urandom of=/tmp/streaming_demo/video.bin bs=1M count=16 2>/dev/null")

    # Kill any previous demo server on the same port (best-effort)
    host.cmd(f"pkill -f 'ThreadingHTTPServer.*{port}' 2>/dev/null || true")
    host.cmd(f"pkill -f 'http.server {port}' 2>/dev/null || true")

    # Multi-threaded HTTP server (one thread per request)
    # This avoids request serialization typical of the default single-thread demo server.
    host.cmd(
        "cd /tmp/streaming_demo && "
        f"nohup python3 -c '"
        "from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler; "
        f"ThreadingHTTPServer((\"0.0.0.0\", {port}), SimpleHTTPRequestHandler).serve_forever()"
        f"' >/tmp/http_{host.name}.log 2>&1 &"
    )



def run():
    topo = SlicingTopology()

    net = Mininet(
        topo=topo,
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True
    )

    # Connection to remote RYU controller
    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6633
    )

    net.start()
    info("\n*** Network started.\n")

    # -------------------------------------------------------------------
    # VIP ARP setup (client must resolve VIP_IP -> VIP_MAC)
    # -------------------------------------------------------------------
    c1 = net.get("c1")
    c1.cmd(f"arp -s {VIP_IP} {VIP_MAC}")

    # -------------------------------------------------------------------
    # Demo backend services (both ON for pipeline Step 1/2)
    # -------------------------------------------------------------------
    srv1 = net.get("srv1")
    srv2 = net.get("srv2")

    _start_demo_http_server(srv1, HTTP_PORT)
    _start_demo_http_server(srv2, HTTP_PORT)

    info(f"*** VIP demo ready: client targets http://{VIP_IP}:{HTTP_PORT}/video.bin\n")
    info("*** NOTE: container start/stop migration will be added in pipeline Step 3.\n")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    run()
