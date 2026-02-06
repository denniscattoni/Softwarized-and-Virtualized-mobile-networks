IMAGE := stream_vnf:latest
VIDEO := app/content/video.bin

.PHONY: all clean

# ----------------------------------------
# Default target
# ----------------------------------------
all:
	@echo "*** Preparing demo payload"
	@if [ ! -f $(VIDEO) ]; then \
		mkdir -p app/content; \
		dd if=/dev/urandom of=$(VIDEO) bs=1M count=20 status=none; \
	fi

	@echo "*** Preparing certificates"
	@cd app/certs && \
	FILES=$$(ls -A) && \
	if [ "$$FILES" = "gen_certs.sh" ]; then \
		./gen_certs.sh; \
	else \
		find . -mindepth 1 ! -name gen_certs.sh -exec rm -rf {} +; \
		./gen_certs.sh; \
	fi

	@echo "*** Building Docker image $(IMAGE)"
	@sudo docker build --no-cache --pull -t $(IMAGE) app/
	@sudo docker image prune -f

	@echo "*** Starting NFV slicing demo"
	@sudo python3 emulation/run_demo.py


# ----------------------------------------
# Running RYU Manager controller
# ----------------------------------------
ryu:
	@echo "Running RYU Manager controller"
	@ryu-manager controller/slice_qos_app.py


# ----------------------------------------
# Executing C1 resource request
# ----------------------------------------
r:
	@echo "*** Executing C1 resource request"
	@python3 app/client_https_chunks.py --url https://10.0.0.100:8443/video.bin --chunk-bytes 1048576 --timeout 2 --retries 200 --backoff 0.3




########################################################################################################################
# ----------------------------------------
# Executing C2 pervasive requests
# ----------------------------------------
pr:
	@echo "*** Executing C2 iperf3 pervasive requests"
	@iperf3 -c 10.0.0.3 -p 5001 -t 120 -P 5 > ./logs/iperf3_srv1.log 2>&1 &


# ----------------------------------------
# Starting low-latency iperf3 server
# ----------------------------------------
server:
	@echo "*** Starting iperf3 server on srv1"
	@docker exec -d srv1 iperf3 -s -p 5001 &
########################################################################################################################




# ----------------------------------------
# Running manual migration of srv2
# ----------------------------------------
1:
	@echo "*** Manual migration of srv2 towards srv1:"
	@curl -X POST "http://127.0.0.1:9090/service/switch?to=srv1"


# ----------------------------------------
# Running manual migration of srv1
# ----------------------------------------
2:
	@echo "*** Manual migration of srv1 towards srv2:"
	@curl -X POST "http://127.0.0.1:9090/service/switch?to=srv2"


# ----------------------------------------
# srvX location status
# ----------------------------------------
s:
	@echo "*** srvX location status:"
	@curl "http://127.0.0.1:9090/service/status"


# ----------------------------------------
# Cleanup
# ----------------------------------------
clean:
	@echo "*** Cleaning demo environment"
	@sudo mn -c >/dev/null 2>&1 || true
	@sudo docker rm -f srv1 srv2 >/dev/null 2>&1 || true
	@rm -f $(VIDEO)
	@cd app/certs && \
	FILES=$$(ls -A) && \
	if [ "$$FILES" != "gen_certs.sh" ]; then \
		find . -mindepth 1 ! -name gen_certs.sh -exec rm -rf {} +; \
	fi