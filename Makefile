-include .env

.PHONY: venv install setup run health lambda-package \
        monitoring-install monitoring-upgrade monitoring-uninstall \
        monitoring-namespace monitoring-repo monitoring-apply monitoring-status \
        grafana-forward grafana-password prometheus-forward alertmanager-forward \
        monitoring-logs

VENV     := .venv
PYTHON   := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UVICORN  := $(VENV)/bin/uvicorn

HOST     := 0.0.0.0
PORT     := 8000

ECR_REPO=ai-knowledge-assistant
ECR_REGISTRY=$(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_URI=$(ECR_REGISTRY)/$(ECR_REPO):latest

# ── Environment ──────────────────────────────────────────────────────────────

venv:
	python3 -m venv $(VENV)
	@echo "Virtualenv created. Run: source $(VENV)/bin/activate"

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

setup:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo ".env created from .env.example — add your OPENAI_API_KEY"; \
	else \
		echo ".env already exists, skipping"; \
	fi

# ── Run ──────────────────────────────────────────────────────────────────────

run:
	$(UVICORN) app.main:app --reload --host $(HOST) --port $(PORT)

# ── Health check ─────────────────────────────────────────────────────────────

health:
	curl -s http://$(HOST):$(PORT)/health | python3 -m json.tool

# ── Lambda packaging ─────────────────────────────────────────────────────────
# Produces lambda-package.zip ready to upload to AWS Lambda.
#
# Zip structure (handler must be at root):
#   document_processor.py   ← Lambda handler  (Lambda config: document_processor.handler)
#   app/                    ← shared services reused from the FastAPI app
#   <pip packages>          ← installed at root so Python can import them
#
# boto3 is excluded — already provided by the Lambda runtime.
#
# WHY DOCKER?
#   pydantic-core and other packages ship compiled C extensions (.so files).
#   Installing on macOS produces macOS/arm64 binaries that crash on Lambda
#   (Linux x86_64). Building inside public.ecr.aws/lambda/python:3.13 —
#   the exact image Lambda uses — guarantees correct binaries every time.

LAMBDA_BUILD := .lambda-build
LAMBDA_ZIP   := lambda-package.zip
LAMBDA_IMAGE := public.ecr.aws/lambda/python:3.14

lambda-package:
	@echo "── Cleaning previous build ──────────────────────────────"
	rm -rf $(LAMBDA_BUILD) $(LAMBDA_ZIP)
	mkdir -p $(LAMBDA_BUILD)

	@echo "── Installing deps inside Lambda Docker image ────────────"
	docker run --rm \
		--platform linux/amd64 \
		--entrypoint "" \
		-v "$(CURDIR)/lambda/requirements.txt:/requirements.txt:ro" \
		-v "$(CURDIR)/$(LAMBDA_BUILD):/out" \
		$(LAMBDA_IMAGE) \
		pip install -r /requirements.txt --target /out --upgrade

	@echo "── Copying handler + app source ─────────────────────────"
	cp lambda/document_processor.py $(LAMBDA_BUILD)/document_processor.py
	cp -r app $(LAMBDA_BUILD)/app

	@echo "── Zipping ──────────────────────────────────────────────"
	cd $(LAMBDA_BUILD) && zip -r ../$(LAMBDA_ZIP) . -x "*.pyc" -x "*/__pycache__/*"

	@echo "── Done ─────────────────────────────────────────────────"
	@echo "Upload $(LAMBDA_ZIP) to Lambda."
	@echo "Set handler to:  document_processor.handler"
	@du -sh $(LAMBDA_ZIP)

# - Docker targets
docker-login:
	aws ecr get-login-password --region $(AWS_REGION) | \
	docker login --username AWS --password-stdin $(ECR_REGISTRY)
	
docker-build:
	docker buildx build --platform linux/amd64,linux/arm64 \
	-t ai-knowledge-assistant:latest .

docker-run:
	docker run -d --name ai-knowledge-assistant -p $(PORT):$(PORT) --env-file .env ai-knowledge-assistant:latest

docker-push:
	docker tag ai-knowledge-assistant:latest ${ECR_REGISTRY}/ai-knowledge-assistant:latest
	docker push ${ECR_REGISTRY}/ai-knowledge-assistant:latest

docker-build-push: docker-login docker-build docker-push

# ── Observability (kube-prometheus-stack) ─────────────────────────────────────

HELM_RELEASE      := monitoring
MONITORING_NS     := monitoring
GRAFANA_PORT      := 3000
PROMETHEUS_PORT   := 9090
ALERTMANAGER_PORT := 9093
GRAFANA_PASSWORD  ?= admin123
RETENTION         ?= 7d

monitoring-namespace:
	kubectl create namespace $(MONITORING_NS) --dry-run=client -o yaml | kubectl apply -f -

monitoring-repo:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update

monitoring-install: monitoring-namespace monitoring-repo
	helm install $(HELM_RELEASE) prometheus-community/kube-prometheus-stack \
		--namespace $(MONITORING_NS) \
		--set grafana.adminPassword=$(GRAFANA_PASSWORD) \
		--set prometheus.prometheusSpec.retention=$(RETENTION) \
		--wait
	@echo "✔  kube-prometheus-stack installed"
	@echo "   make grafana-forward    → http://localhost:$(GRAFANA_PORT)"
	@echo "   make prometheus-forward → http://localhost:$(PROMETHEUS_PORT)"

monitoring-upgrade:
	helm upgrade $(HELM_RELEASE) prometheus-community/kube-prometheus-stack \
		--namespace $(MONITORING_NS) \
		--set grafana.adminPassword=$(GRAFANA_PASSWORD) \
		--set prometheus.prometheusSpec.retention=$(RETENTION) \
		--reuse-values

monitoring-uninstall:
	helm uninstall $(HELM_RELEASE) --namespace $(MONITORING_NS)
	kubectl delete namespace $(MONITORING_NS)

monitoring-apply:
	kubectl apply -f k8s/service.yaml
	kubectl apply -f k8s/service-monitor.yaml
	@echo "✔  ServiceMonitor applied — Prometheus will scrape /metrics in ~30s"

monitoring-status:
	@echo "── Pods ──────────────────────────────────────────────────"
	kubectl get pods -n $(MONITORING_NS)
	@echo ""
	@echo "── Services ──────────────────────────────────────────────"
	kubectl get svc -n $(MONITORING_NS)
	@echo ""
	@echo "── ServiceMonitors ───────────────────────────────────────"
	kubectl get servicemonitor -n $(MONITORING_NS)

grafana-forward:
	@echo "Grafana → http://localhost:$(GRAFANA_PORT)  (user: admin)"
	kubectl port-forward svc/$(HELM_RELEASE)-grafana $(GRAFANA_PORT):80 -n $(MONITORING_NS)

prometheus-forward:
	@echo "Prometheus → http://localhost:$(PROMETHEUS_PORT)"
	kubectl port-forward svc/$(HELM_RELEASE)-kube-prometheus-prometheus $(PROMETHEUS_PORT):9090 -n $(MONITORING_NS)

alertmanager-forward:
	@echo "Alertmanager → http://localhost:$(ALERTMANAGER_PORT)"
	kubectl port-forward svc/$(HELM_RELEASE)-kube-prometheus-alertmanager $(ALERTMANAGER_PORT):9093 -n $(MONITORING_NS)

grafana-password:
	@kubectl get secret $(HELM_RELEASE)-grafana -n $(MONITORING_NS) \
		-o jsonpath="{.data.admin-password}" | base64 --decode
	@echo ""

monitoring-logs:
	kubectl logs -n $(MONITORING_NS) -l app.kubernetes.io/name=prometheus --tail=50

port-forward-pod:
	kubectl port-forward pod/ai-knowledge-assistant-dd8fc6b84-m4b6b 8001:8000

scale-down-deployment:
	kubectl scale deployment/ai-knowledge-assistant --replicas=0

scale-up-deployment:
	kubectl scale deployment/ai-knowledge-assistant --replicas=1

restart-pod: scale-down-deployment scale-up-deployment
	kubectl rollout restart deployment ai-knowledge-assistant

scale-up-nodegroup:
	aws eks update-nodegroup-config \
	--cluster-name ai-knowledge-cluster \
	--nodegroup-name rag-workers \
	--scaling-config minSize=1,maxSize=4,desiredSize=4 \
	--region $(AWS_REGION)


test-chat:
	curl -s -X POST $(EXTERNAL_IP)/chat \
	-H "Content-Type: application/json" \
	-d '{"question": "What is the scaling strategy in Kubernetes?"}' | python3 -m json.tool
