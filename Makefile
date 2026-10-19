PY := .venv/bin/python
CLUSTER ?= honk-demo
NS ?= shop
DEMO := sed -e 's/namespace: shop/namespace: $(NS)/' -e 's/^  name: shop$$/  name: $(NS)/' deploy/demo.yaml

.PHONY: check lint type test install demo-cluster demo-apply demo-reset demo-down

check: lint type test

lint:
	.venv/bin/ruff check src tests

type:
	.venv/bin/mypy src

test:
	$(PY) -m pytest -q

install:
	uv sync

# A local kind cluster with a few deliberately broken workloads to point an agent at.
demo-cluster:
	kind create cluster --name $(CLUSTER) --config deploy/kind.yaml
	kubectl --context kind-$(CLUSTER) apply -f deploy/honk-rbac.yaml

demo-apply:
	$(DEMO) | kubectl --context kind-$(CLUSTER) apply -f -

demo-reset:
	$(DEMO) | kubectl --context kind-$(CLUSTER) delete -f - --ignore-not-found
	$(DEMO) | kubectl --context kind-$(CLUSTER) apply -f -

demo-down:
	kind delete cluster --name $(CLUSTER)
