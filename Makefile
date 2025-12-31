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

	@echo "*** Building Docker image $(IMAGE)"
	@sudo docker build -t $(IMAGE) app/

	@echo "*** Starting NFV slicing demo"
	@sudo python3 emulation/run_demo.py

# ----------------------------------------
# Cleanup
# ----------------------------------------
clean:
	@echo "*** Cleaning demo environment"
	@sudo mn -c >/dev/null 2>&1 || true
	@sudo docker rm -f srv1 srv2 >/dev/null 2>&1 || true
	@rm -f $(VIDEO)
