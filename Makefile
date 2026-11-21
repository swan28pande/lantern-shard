IMAGE ?= br-sel
SAMPLE ?= $(CURDIR)/../sample/sample
PLATFORM ?=
RUN_FLAGS := --rm $(PLATFORM) --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

build:
	docker build $(PLATFORM) -q --tag $(IMAGE) .

sample: build
	/usr/bin/time -p docker run $(RUN_FLAGS) $(IMAGE) > out.jsonl 2> err.log; tail -3 err.log
	python3 ../sample/score.py $(SAMPLE) $(SAMPLE)/answers.json out.jsonl
