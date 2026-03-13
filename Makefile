-include .env

.PHONY: venv install setup run health lambda-package \
        monitoring-install monitoring-upgrade monitoring-uninstall \
        monitoring-namespace monitoring-repo monitoring-apply monitoring-status \
        grafana-forward grafana-password prometheus-forward alertmanager-forward \
        monitoring-logs \
        argocd-install argocd-apply argocd-status argocd-forward argocd-password argocd-sync \
        debug-nodes debug-pods debug-app debug-app-logs debug-app-metrics \
        debug-app-exec debug-events debug-pending \
        node-uncordon node-pod-counts \
        app-scale-down app-scale-up app-restart app-forward \
        cluster-scale test-chat \
        create-oidc-provider create-iam-role attach-ecr-policy attach-lambda-policy iam-setup

VENV     := .venv
PYTHON   := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
UVICORN  := $(VENV)/bin/uvicorn

HOST     := 0.0.0.0
PORT     := 8000

ECR_REPO=ai-knowledge-assistant
ECR_REGISTRY=$(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
IMAGE_URI=$(ECR_REGISTRY)/$(ECR_REPO):latest

GITHUB_ORG      ?= E-equals-mcsquare
GITHUB_REPO     ?= AI-Knowledge-Assistant-RAG-Agent
IAM_ROLE_NAME   ?= github-actions-deploy

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

# ── GitHub Actions IAM / OIDC setup ──────────────────────────────────────────
# Run once to allow GitHub Actions to push to ECR without long-lived keys.
# Requires: AWS_ACCOUNT_ID and AWS_REGION set in .env (or exported in shell).
#
LAMBDA_FUNCTION_NAME ?= ai-knowledge-assistant-document-processor

# Usage:
#   make iam-setup               ← ECR + Lambda permissions (runs all steps)
#   make create-oidc-provider
#   make create-iam-role
#   make attach-ecr-policy
#   make attach-lambda-policy    ← run separately if Lambda workflow added later

# Convenience target — runs all steps (ECR + Lambda)
iam-setup: create-oidc-provider create-iam-role attach-ecr-policy attach-lambda-policy
	@echo ""
	@echo "✔  IAM setup complete."
	@echo "   Add this secret to GitHub → Settings → Secrets → Actions:"
	@echo "   AWS_ROLE_ARN = arn:aws:iam::$(AWS_ACCOUNT_ID):role/$(IAM_ROLE_NAME)"

create-oidc-provider:
	@echo "── Creating GitHub OIDC provider (safe to run if already exists) ──"
	aws iam create-open-id-connect-provider \
		--url https://token.actions.githubusercontent.com \
		--client-id-list sts.amazonaws.com \
		--thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
		2>&1 | grep -v "EntityAlreadyExists" || true
	@echo "✔  OIDC provider ready"

create-iam-role:
	@echo "── Creating IAM role: $(IAM_ROLE_NAME) ──────────────────────────"
	@printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Federated":"arn:aws:iam::$(AWS_ACCOUNT_ID):oidc-provider/token.actions.githubusercontent.com"},"Action":"sts:AssumeRoleWithWebIdentity","Condition":{"StringEquals":{"token.actions.githubusercontent.com:aud":"sts.amazonaws.com"},"StringLike":{"token.actions.githubusercontent.com:sub":"repo:$(GITHUB_ORG)/$(GITHUB_REPO):*"}}}]}' \
		> /tmp/github-actions-trust-policy.json
	aws iam create-role \
		--role-name $(IAM_ROLE_NAME) \
		--assume-role-policy-document file:///tmp/github-actions-trust-policy.json \
		--output text --query 'Role.Arn'
	@rm -f /tmp/github-actions-trust-policy.json
	@echo "✔  Role created: arn:aws:iam::$(AWS_ACCOUNT_ID):role/$(IAM_ROLE_NAME)"

attach-ecr-policy:
	@echo "── Attaching ECR push policy to $(IAM_ROLE_NAME) ───────────────"
	@printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["ecr:GetAuthorizationToken"],"Resource":"*"},{"Effect":"Allow","Action":["ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage","ecr:InitiateLayerUpload","ecr:UploadLayerPart","ecr:CompleteLayerUpload","ecr:PutImage","ecr:DescribeRepositories","ecr:CreateRepository"],"Resource":"arn:aws:ecr:$(AWS_REGION):$(AWS_ACCOUNT_ID):repository/$(ECR_REPO)"}]}' \
		> /tmp/github-actions-ecr-policy.json
	aws iam put-role-policy \
		--role-name $(IAM_ROLE_NAME) \
		--policy-name ecr-push \
		--policy-document file:///tmp/github-actions-ecr-policy.json
	@rm -f /tmp/github-actions-ecr-policy.json
	@echo "✔  ECR push policy attached"

attach-lambda-policy:
	@echo "── Attaching Lambda deploy policy to $(IAM_ROLE_NAME) ──────────"
	@printf '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["lambda:UpdateFunctionCode","lambda:PublishVersion","lambda:GetFunction","lambda:GetFunctionConfiguration"],"Resource":"arn:aws:lambda:$(AWS_REGION):$(AWS_ACCOUNT_ID):function:$(LAMBDA_FUNCTION_NAME)"}]}' \
		> /tmp/github-actions-lambda-policy.json
	aws iam put-role-policy \
		--role-name $(IAM_ROLE_NAME) \
		--policy-name lambda-deploy \
		--policy-document file:///tmp/github-actions-lambda-policy.json
	@rm -f /tmp/github-actions-lambda-policy.json
	@echo "✔  Lambda deploy policy attached"

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

# ── ArgoCD (GitOps) ───────────────────────────────────────────────────────────

ARGOCD_NS      := argocd
ARGOCD_PORT    := 8080
ARGOCD_VERSION := stable

argocd-install:
	kubectl create namespace $(ARGOCD_NS) --dry-run=client -o yaml | kubectl apply -f -
	kubectl apply --server-side --force-conflicts -n $(ARGOCD_NS) -f https://raw.githubusercontent.com/argoproj/argo-cd/$(ARGOCD_VERSION)/manifests/install.yaml
	@echo "Waiting for ArgoCD pods to be ready..."
	kubectl wait --for=condition=available --timeout=120s deployment/argocd-server -n $(ARGOCD_NS)
	@echo "✔  ArgoCD installed"
	@echo "   make argocd-apply    → register the app"
	@echo "   make argocd-forward  → https://localhost:$(ARGOCD_PORT)"

argocd-apply:
	kubectl apply -f k8s/argocd-app.yaml
	@echo "✔  ArgoCD Application applied — auto-sync active"

argocd-status:
	@echo "── ArgoCD Pods ───────────────────────────────────────────"
	kubectl get pods -n $(ARGOCD_NS)
	@echo ""
	@echo "── Application Sync Status ───────────────────────────────"
	kubectl get application ai-knowledge-assistant -n $(ARGOCD_NS) 2>/dev/null || echo "Application not found — run: make argocd-apply"

argocd-forward:
	@echo "ArgoCD UI → https://localhost:$(ARGOCD_PORT)  (accept self-signed cert)"
	kubectl port-forward svc/argocd-server $(ARGOCD_PORT):443 -n $(ARGOCD_NS)

argocd-password:
	@kubectl get secret argocd-initial-admin-secret -n $(ARGOCD_NS) \
		-o jsonpath="{.data.password}" | base64 --decode
	@echo ""

argocd-sync:
	kubectl patch application ai-knowledge-assistant -n $(ARGOCD_NS) \
		--type merge -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"revision":"HEAD"}}}'
	@echo "✔  Manual sync triggered"

# ── Troubleshooting & Debugging ──────────────────────────────────────────────

CLUSTER_NAME   := ai-knowledge-cluster
NODEGROUP_NAME := rag-workers
APP_DEPLOY     := ai-knowledge-assistant

# -- Node diagnostics ---------------------------------------------------------

debug-nodes:
	@echo "── Node status ───────────────────────────────────────────"
	kubectl get nodes -o wide
	@echo ""
	@echo "── Pod count per node ────────────────────────────────────"
	kubectl get pods -A -o wide | awk '{print $$8}' | sort | uniq -c | sort -rn

node-pod-counts:
	kubectl get pods -A -o wide | awk '{print $$8}' | sort | uniq -c | sort -rn

node-uncordon:
	@echo "Cordoned nodes:"
	kubectl get nodes | grep SchedulingDisabled
	@read -p "Enter node name to uncordon: " NODE; kubectl uncordon $$NODE

# -- Pod diagnostics ----------------------------------------------------------

debug-pods:
	@echo "── All pods (default namespace) ──────────────────────────"
	kubectl get pods -o wide
	@echo ""
	@echo "── All pods (all namespaces) ─────────────────────────────"
	kubectl get pods -A -o wide

debug-pending:
	@echo "── Pending pods across all namespaces ────────────────────"
	kubectl get pods -A --field-selector=status.phase=Pending
	@echo ""
	@echo "── Events for pending pods ───────────────────────────────"
	kubectl get events -A --field-selector=reason=FailedScheduling --sort-by='.lastTimestamp' | tail -20

debug-events:
	kubectl get events -A --sort-by='.lastTimestamp' | tail -30

# -- App diagnostics ----------------------------------------------------------

debug-app:
	@echo "── Deployment status ─────────────────────────────────────"
	kubectl get deployment $(APP_DEPLOY) -o wide
	@echo ""
	@echo "── Pod details ───────────────────────────────────────────"
	kubectl describe pod -l app=$(APP_DEPLOY) | tail -30

debug-app-logs:
	kubectl logs -l app=$(APP_DEPLOY) --tail=50 --follow

debug-app-metrics:
	@POD=$$(kubectl get pod -l app=$(APP_DEPLOY) -o jsonpath='{.items[0].metadata.name}'); \
	echo "Fetching /metrics from $$POD"; \
	kubectl exec -it $$POD -- python3 -c \
		"import urllib.request; print(urllib.request.urlopen('http://localhost:8000/metrics').read().decode()[:3000])"

debug-app-exec:
	@POD=$$(kubectl get pod -l app=$(APP_DEPLOY) -o jsonpath='{.items[0].metadata.name}'); \
	echo "Opening shell in $$POD"; \
	kubectl exec -it $$POD -- /bin/sh

# -- App lifecycle ------------------------------------------------------------

app-scale-down:
	kubectl scale deployment/$(APP_DEPLOY) --replicas=0

app-scale-up:
	kubectl scale deployment/$(APP_DEPLOY) --replicas=1
	kubectl rollout status deployment/$(APP_DEPLOY)

app-restart: app-scale-down app-scale-up

app-forward:
	@echo "App → http://localhost:8001"
	@POD=$$(kubectl get pod -l app=$(APP_DEPLOY) -o jsonpath='{.items[0].metadata.name}'); \
	kubectl port-forward $$POD 8001:8000

# -- Cluster scaling ----------------------------------------------------------

cluster-scale:
	@read -p "Enter desired node count: " N; \
	aws eks update-nodegroup-config \
		--cluster-name $(CLUSTER_NAME) \
		--nodegroup-name $(NODEGROUP_NAME) \
		--scaling-config minSize=1,maxSize=$$N,desiredSize=$$N \
		--region $(AWS_REGION)
	@echo "Scaling in progress — run: kubectl get nodes -w"

# -- Smoke test ---------------------------------------------------------------

test-chat:
	@echo "Sending test chat request to $(EXTERNAL_IP)..."
	curl -s -X POST http://$(EXTERNAL_IP)/chat \
		-H "Content-Type: application/json" \
		-d '{"question": "What is this document about?"}' | python3 -m json.tool
