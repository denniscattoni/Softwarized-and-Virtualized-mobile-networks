# Network Slice Setup Optimization

This repository contains the design, implementation, and experimental evaluation of a **QoS-aware SDN-based network slicing system** for softwarized and virtualized mobile networks.

The project focuses on dynamic optimization of network slices with **low latency** and **high throughput** requirements, by leveraging **Software Defined Networking (SDN)**, **Network Function Virtualization (NFV)**, and **Multi-access Edge Computing (MEC)** principles.  
Traffic steering, QoS monitoring, and service migration decisions are centrally managed by an SDN controller with a global view of the network.

The work was carried out as part of the **Softwarized and Virtualized Mobile Networks** course (Master’s degree in Computer Science, University of Trento, A.Y. 2025/2026).

For a complete description of the system design, use cases and experimental results, refer to the final project report available ([`here`](./resources/Softwarized_and_Virtualized_mobile_networks.pdf)).


<br>
<img alt="" src="./resources/net_slice.png" width="100%"><br>


## Project Overview

The proposed system implements a **QoS-aware network slicing architecture** over an emulated SDN infrastructure, following a clear separation between **northbound**, **control**, and **southbound** components.

Each slice is associated with a specific service profile and optimization objective:

- **Low-latency slice**: optimized for delay-sensitive traffic using latency-oriented path computation.
- **High-throughput slice**: optimized for bandwidth-intensive services using widest-path routing and dynamic service migration.

The architecture is structured as follows:

- **Northbound layer**: defines high-level service intent and slice requirements, including QoS objectives, traffic classification rules, and optimization policies. This layer abstracts application needs from the underlying network implementation.

- **Control plane**: implemented using the **Ryu SDN Framework**, the control plane maintains a global view of the network topology and runtime state. It is responsible for QoS monitoring, path computation, slice-specific decision logic, and triggering service migration when required.

- **Southbound layer**: handles direct interaction with the data plane through **OpenFlow v1.3**, translating control decisions into concrete forwarding rules, queue assignments, and packet processing actions on the switches.

The **data plane** is implemented using **Open vSwitch**, providing programmable forwarding behavior and traffic isolation mechanisms.  

The system demonstrates how **routing optimization and service placement** can be jointly orchestrated to satisfy heterogeneous Quality of Service (QoS) requirements under dynamic network conditions, link failures and varying traffic demands.



<br>



## Getting Started

This section describes the required environment and the steps needed to run the project in a controlled emulation setup.

### Prerequisites

To run the project, the following environment and tools are required:

- **Vagrant** and **VirtualBox**
- **ComNetsEmu** (Vagrant-based installation recommended)
- **Python 3**
- **NetworkX** Python library
- **Docker** (installed inside the ComNetsEmu virtual machine)


---

> [!WARNING]
> The use of the **ComNetsEmu virtual machine** is strongly recommended in order to ensure compatibility and reproducibility of the experiments.

---


### Setup Instructions

1. **Install ComNetsEmu**  
   
    Follow the official ComNetsEmu documentation and set up the Vagrant-based virtual machine as explained ([`here`](https://git.comnets.net/public-repo/comnetsemu#installation)).


2. **Clone the repository**
   
    From inside the ComNetsEmu shared directory, run:

   ```bash
   cd /comnetsemu
   git clone <REPOSITORY_URL>
   ```
   
3. **Start the virtual machine**
   
   ```bash
   vagrant up comnetsemu
   vagrant ssh comnetsemu
   ```

4. **Install NetworkX**  
    
    After the ComNetsEmu virtual machine has been correctly launched, install the Python **NetworkX** library.  
    Follow the official NetworkX installation instructions available [`here`](https://networkx.org/documentation/stable/install.html).


5. **Run the system**

   After the ComNetsEmu virtual machine has been correctly launched, move to the project directory:
   ```bash
   cd /comnetsemu/Softwarized-and-Virtualized-mobile-networks
   ```

   The project provides a `Makefile` that automates the workflow, including:
   - SDN controller startup
   - Mininet topology instantiation
   - Docker-based service deployment
   - TLS certificate generation

   It is **strongly** recommended to use `tmux` with three terminals:
   - **T1**: used for the SDN controller logs
   - **T2**: Mininet topology and traffic generation / link failure events
   - **T3**: Management-plane interaction (service migration)


---

> [!IMPORTANT]
> For further details on the available use cases, as well as on the configuration flags used to enable or disable individual slices and service migration, refer to the complete project report available ([`here`](./resources/Softwarized_and_Virtualized_mobile_networks.pdf)).

---
    
<br>


## Used Technologies

The project integrates multiple technologies and networking principles in order to emulate a realistic softwarized network environment:

- **Software Defined Networking**
  - Ryu SDN Controller
  - OpenFlow v1.3
  - Open vSwitch (OVS)

- **Network Emulation**
  - Mininet
  - ComNetsEmu

- **Network Function Virtualization**
  - Docker-based backend services
  - Containerized NGINX HTTPS streaming service

- **QoS Monitoring and Optimization**
  - NetworkX for topology modeling and Dijkstra path computation
  - EWMA-based link utilization estimation
  - Latency-oriented shortest path algorithms
  - Throughput-oriented widest path algorithms

- **On-demand streaming service**
  - HTTPS communication secured with TLS 1.3
  - Server authentication using Ed25519 certificates
  - Authenticated encryption with AES-256-GCM



<br>



## Network Slicing Model

The system supports multiple **network slices** with heterogeneous Quality of Service (QoS) requirements.  
Each slice is associated with a specific service profile and optimization objective, and traffic is consistently classified and handled across the SDN domain.

The implemented slices are:

- **Low-Latency Slice**  
  This slice is designed for delay-sensitive traffic. Its primary objective is to minimize end-to-end latency by selecting forwarding paths with the lowest estimated delay.  
  Traffic is mapped to high-priority queues to reduce queuing delay under contention, and path recomputation is triggered when latency constraints are violated or when link failures occur.

- **High-Throughput Slice**  
  This slice targets bandwidth-intensive services and aims to maximize sustained throughput. Path selection is formulated as a widest-path problem, maximizing the minimum residual capacity along the path.  
  When routing optimization alone is insufficient to satisfy throughput requirements, the controller may trigger **service migration** toward a more suitable backend node, while preserving a stable service endpoint through SDN-based NAT mechanisms.

Slices operate concurrently over shared network resources, with independent monitoring and optimization logic, enabling realistic evaluation of multi-slice interactions and resource contention.



<br>



## Results

The experimental evaluation demonstrates the effectiveness of the proposed SDN/NFV-based control architecture in enforcing the predefined QoS requirements through network slicing.

Key observations include:

- **Latency compliance** for delay-sensitive traffic in the low-latency slice, achieved through latency-oriented path computation and high-priority queueing.
- **Adaptive throughput optimization** for bandwidth-intensive services, using widest-path routing based on dynamically estimated residual capacities.
- **Seamless service migration** for the high-throughput slice, enabled by SDN-based VIP/VMAC NAT mechanisms, with no changes required at the client side.
- **Robust reaction to network events**, including link failures and recovery, through dynamic path recomputation and service relocation.
- **Correct coexistence of multiple slices** over shared network resources, with independent monitoring and decision logic per slice.

Overall, the controller successfully adapts routing and service placement decisions in response to changing network conditions, while maintaining stable and predictable behavior across all evaluated scenarios.  
For further details, refer to the final report available ([`here`](./resources/Softwarized_and_Virtualized_mobile_networks.pdf)).


<br>


## Conclusion

This project demonstrates how **Software Defined Networking**, combined with **Network Function Virtualization** and **Multi-access Edge Computing** principles, can be effectively used to implement **QoS-aware network slicing** in softwarized networks.

By leveraging a centralized SDN controller with a global view of the network, the system is able to dynamically adapt routing decisions and service placement in response to congestion, failures, and heterogeneous service requirements. The integration of throughput-oriented optimization with service migration.

Although the system is implemented in a controlled emulation environment, the proposed design closely reflects real-world SDN/NFV architectures and provides a solid foundation for exploring advanced topics such as scalable slice orchestration, tighter QoE integration, and automated policy-driven network management.



---

> [!IMPORTANT]
> It is important to note that security was not the primary focus of this project. Security considerations were mainly addressed in the design of the on-demand streaming service traffic, where common best practices were adopted.  

---

<br>


# Credits

* Dennis Cattoni - Università degli studi di Trento (Unitn), Trento – Italy
  <br> 📨 dennis.cattoni@studenti.unitn.it
* Marco Lasagna - Università degli studi di Trento (Unitn), Trento – Italy
  <br> 📨 marco.lasagna@studenti.unitn.it
* Andrea Eugenio Cesaretti - Università degli studi di Trento (Unitn), Trento – Italy
  <br> 📨 andrea.cesaretti@studenti.unitn.it






