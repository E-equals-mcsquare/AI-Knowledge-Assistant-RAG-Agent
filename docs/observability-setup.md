# Observability Setup — Prometheus & Grafana on EKS

Full observability for the AI Knowledge Assistant RAG service using **kube-prometheus-stack**.

## Architecture

```
EKS Cluster
├── default namespace
│   ├── ai-knowledge-assistant pod   (FastAPI + /metrics endpoint)
│   └── ai-knowledge-assistant-service  (LoadBalancer, port 80 → 8000)
│
└── monitoring namespace
    └── kube-prometheus-stack (Helm release: "monitoring")
        ├── Prometheus          — scrapes /metrics every 15s via ServiceMonitor
        ├── Grafana             — dashboards + visualization
        ├── Alertmanager        — alerts routing
        ├── node-exporter       — CPU, memory, disk per EC2 node (DaemonSet)
        └── kube-state-metrics  — pod/deployment/node state from K8s API

Data flow:
  FastAPI /metrics ←── ServiceMonitor ←── Prometheus ←── Grafana dashboards
```

## Custom Metrics Exposed

| Metric | Type | Description |
|---|---|---|
| `rag_requests_total` | Counter | Total /chat requests by status (success/error) |
| `rag_request_latency_seconds` | Histogram | End-to-end request latency |
| `vector_search_latency_seconds` | Histogram | Pinecone retrieval latency |
| `llm_generation_latency_seconds` | Histogram | OpenAI generation latency |
| `llm_tokens_total` | Counter | Token usage by type (prompt/completion) |

---

## Prerequisites

```bash
# Helm (macOS)
brew install helm

# Configure kubectl for EKS
aws eks update-kubeconfig --region ap-south-1 --name ai-knowledge-cluster
kubectl get nodes
```

---

## Step 1 — Rebuild and Push Docker Image

Code changes were made to `requirements.txt`, `app/main.py`, `app/api/routes/chat.py`,
and the new `app/core/metrics.py`. A new image is required.

```bash
make docker-login
make docker-build
make docker-push
```

---

## Step 2 — Deploy Updated Image to EKS

```bash
kubectl apply -f k8s/service.yaml
kubectl rollout restart deployment/ai-knowledge-assistant
kubectl rollout status deployment/ai-knowledge-assistant
```

---

## Step 3 — Verify /metrics is Live

```bash
curl http://<EXTERNAL-IP>/metrics
```

Expected: Prometheus-format output containing `rag_requests_total`, `http_requests_total`, etc.

---

## Step 4 — Install kube-prometheus-stack

```bash
make monitoring-install
```

Wait ~3 minutes for all pods to start, then verify:

```bash
make monitoring-status
```

Expected pods (all `Running`):

```
monitoring-grafana-xxx
monitoring-kube-prometheus-stack-prometheus-xxx
monitoring-kube-prometheus-stack-alertmanager-xxx
monitoring-kube-prometheus-stack-operator-xxx
monitoring-kube-state-metrics-xxx
monitoring-prometheus-node-exporter-xxx   # one per node
```

---

## Step 5 — Apply ServiceMonitor

```bash
make monitoring-apply
```

This registers the FastAPI service as a Prometheus scrape target via `k8s/service-monitor.yaml`.

---

## Step 6 — Access Grafana

```bash
make grafana-forward      # serves on http://localhost:3000
make grafana-password     # prints the admin password
```

- **URL:** http://localhost:3000
- **Username:** `admin`
- **Password:** output of `make grafana-password`

---

## Step 7 — Access Prometheus UI

```bash
make prometheus-forward   # serves on http://localhost:9090
```

Go to **Status → Targets** and confirm `default/ai-knowledge-assistant` shows state **UP**.

If it shows **DOWN**, check:

```bash
make monitoring-logs
```

---

## Step 8 — Build Grafana Dashboards

In Grafana: **+ → Dashboard → Add panel**

### Suggested Panels

| Panel Title | PromQL |
|---|---|
| Requests per second | `rate(rag_requests_total[1m])` |
| Error rate | `rate(rag_requests_total{status="error"}[1m])` |
| p95 end-to-end latency | `histogram_quantile(0.95, rate(rag_request_latency_seconds_bucket[5m]))` |
| p95 vector search latency | `histogram_quantile(0.95, rate(vector_search_latency_seconds_bucket[5m]))` |
| p95 LLM latency | `histogram_quantile(0.95, rate(llm_generation_latency_seconds_bucket[5m]))` |
| Token usage / hour | `increase(llm_tokens_total[1h])` |
| Pod CPU usage | `rate(container_cpu_usage_seconds_total{pod=~"ai-knowledge-assistant.*"}[5m])` |
| Pod memory | `container_memory_working_set_bytes{pod=~"ai-knowledge-assistant.*"}` |

---

## Makefile Reference

| Command | Description |
|---|---|
| `make monitoring-install` | Install kube-prometheus-stack via Helm |
| `make monitoring-upgrade` | Upgrade existing installation |
| `make monitoring-uninstall` | Remove monitoring stack and namespace |
| `make monitoring-apply` | Apply ServiceMonitor + Service manifests |
| `make monitoring-status` | Show pods, services, and ServiceMonitors |
| `make grafana-forward` | Port-forward Grafana → http://localhost:3000 |
| `make grafana-password` | Print Grafana admin password |
| `make prometheus-forward` | Port-forward Prometheus → http://localhost:9090 |
| `make alertmanager-forward` | Port-forward Alertmanager → http://localhost:9093 |
| `make monitoring-logs` | Tail Prometheus pod logs |

---

## Tear Down (end of week)

```bash
make monitoring-uninstall
```
