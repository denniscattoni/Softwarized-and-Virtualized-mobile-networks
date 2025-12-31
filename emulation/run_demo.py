#!/usr/bin/env python3

"""
run_demo.py: end-to-end runner for the slicing + NFV migration demo.

Responsibilities:
- Instantiate the Containernet network from emulation/topology.py
- Connect to remote Ryu controller
- Start the REST management plane (NFV orchestrator mini-service)
- Apply default placement (srv2 ON, srv1 OFF)
- Enter Mininet CLI (interactive demo)

Usage:
  Terminal 1:
    ryu-manager controller.slice_qos_app

  Terminal 2:
    sudo python3 emulation/run_demo.py
"""

from __future__ import annotations

import os
import importlib.util

from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink

from comnetsemu.net import Containernet

from topology import SlicingTopology


# -------------------------------------------------------------------
# Dynamic import helper (no packages, no __init__.py)
# -------------------------------------------------------------------
def _import_module_from_path(name: str, path: str):
    """
    Import a Python module from an explicit file path.

    Design goals:
    - Avoid package layout constraints (__init__.py).
    - Keep the repo structure clean for a "clone & run" demo.
    - Be deterministic: fail fast if the module cannot be loaded.
    """
    assert os.path.isfile(path), f"Module file not found: {path}"

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load module spec: name={name} path={path}"

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run():
    # Resolve repo root (one level above emulation/)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    # Load service_manager.py without requiring app/ to be a Python package.
    svc_path = os.path.join(repo_root, "app", "service_manager.py")
    svc_mod = _import_module_from_path("service_manager", svc_path)

    ServiceManager = svc_mod.ServiceManager
    start_rest_server = svc_mod.start_rest_server

    topo = SlicingTopology()

    net = Containernet(
        topo=topo,
        controller=None,
        switch=OVSSwitch,
        link=TCLink,
        autoSetMacs=True,
        autoStaticArp=True,
    )

    # Remote Ryu controller (same as your current workflow)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6633)

    net.start()
    info("\n*** Network started.\n")

    c1 = net.get("c1")
    c1.cmd("arp -d 10.0.0.100 >/dev/null 2>&1 || true")
    c1.cmd("arp -s 10.0.0.100 02:00:00:00:00:64")
    info("*** Installed static ARP on c1 for VIP 10.0.0.100 -> 02:00:00:00:00:64\n")

    # Grab DockerHost nodes
    srv1 = net.get("srv1")
    srv2 = net.get("srv2")

    # Start management plane (REST)
    # NOTE: this server runs in the same Python process as the network runner,
    # so it can safely call srvX.cmd(...) without mnexec/PIDs.
    svc_mgr = ServiceManager(
        srv1,
        srv2,
        port=8080,
        content_dir="/var/www/html",
        resource="video.bin",
    )

    start_rest_server(svc_mgr, host="127.0.0.1", port=9090)
    info("*** Service management API listening on http://127.0.0.1:9090\n")

    # Default placement for the demo:
    # - remote backend (srv2) ON
    # - closest backend (srv1) OFF
    svc_mgr.stop("srv1")
    svc_mgr.start("srv2")
    info("*** Default service placement: srv2=ON srv1=OFF\n")

    # Optional sanity: show MACs (useful to copy into slice_config.py if needed)
    info(f"*** c1 MAC:  {c1.MAC()}\n")
    info(f"*** srv1 MAC: {srv1.MAC()}\n")
    info(f"*** srv2 MAC: {srv2.MAC()}\n")

    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
