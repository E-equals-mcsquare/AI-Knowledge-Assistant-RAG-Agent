# ArgoCD Setup — GitOps on EKS

GitOps continuous delivery for the AI Knowledge Assistant using **ArgoCD**.
Any push to the `k8s/` directory on `main` automatically syncs to the EKS cluster.

## Architecture

```
GitHub (main branch)
└── k8s/
    ├── deployment.yaml
    ├── service.yaml
    ├── configmap.yaml
    ├── service-account.yaml
    ├── service-monitor.yaml
    └── argocd-app.yaml

ArgoCD (argocd namespace)
└── watches k8s/ every 3 minutes
    └── auto-syncs → EKS default namespace
```

Data flow:
```
git push → GitHub → ArgoCD detects drift → kubectl apply → EKS cluster
```

---

## Prerequisites

```bash
# Configure kubectl for EKS
aws eks update-kubeconfig --region ap-south-1 --name ai-knowledge-cluster
kubectl get nodes
```

---

## Step 1 — Install ArgoCD

```bash
make argocd-install
```

This creates the `argocd` namespace and installs ArgoCD from the official stable manifests.
Wait ~2 minutes for all pods to start.

---

## Step 2 — Push the ArgoCD Application manifest

The `k8s/argocd-app.yaml` file must be in GitHub before ArgoCD can read it:

```bash
git add k8s/argocd-app.yaml k8s/.argoignore
git commit -m "Add ArgoCD Application manifest"
git push origin main
```

---

## Step 3 — Register the Application

```bash
make argocd-apply
```

This applies `k8s/argocd-app.yaml` to the cluster. ArgoCD will immediately begin syncing the `k8s/` directory from GitHub.

---

## Step 4 — Verify Status

```bash
make argocd-status
```

Expected output:
```
── ArgoCD Pods ───────────────────────────────────────
argocd-application-controller-xxx    Running
argocd-repo-server-xxx               Running
argocd-server-xxx                    Running
argocd-redis-xxx                     Running

── Application Sync Status ───────────────────────────
NAME                      SYNC STATUS   HEALTH STATUS
ai-knowledge-assistant    Synced        Healthy
```

---

## Step 5 — Access the ArgoCD UI

```bash
make argocd-forward     # serves on https://localhost:8080
make argocd-password    # prints the admin password
```

- **URL:** https://localhost:8080 (accept the self-signed certificate)
- **Username:** `admin`
- **Password:** output of `make argocd-password`

In the UI you'll see:
- The `ai-knowledge-assistant` app card
- Sync status: **Synced** / **OutOfSync**
- Health status: **Healthy** / **Degraded**
- A visual graph of all deployed Kubernetes resources

---

## How Auto-Sync Works

| Event | What ArgoCD does |
|---|---|
| `git push` changes a manifest | Detects drift within 3 min → auto-applies |
| Manual `kubectl apply` overrides a resource | `selfHeal: true` reverts it back to git state |
| A manifest is deleted from git | `prune: true` deletes the resource from the cluster |
| Pod crashes / restarts | Kubernetes handles it; ArgoCD monitors health |

---

## GitOps Workflow (day-to-day)

```bash
# 1. Edit a k8s manifest locally (e.g. bump image tag or change env var)
vim k8s/deployment.yaml

# 2. Push to main
git add k8s/deployment.yaml
git commit -m "Update deployment image to v1.2.3"
git push origin main

# 3. ArgoCD auto-syncs within ~3 minutes — no kubectl needed
```

To trigger an immediate sync without waiting:
```bash
make argocd-sync
```

---

## Makefile Reference

| Command | Description |
|---|---|
| `make argocd-install` | Install ArgoCD in the argocd namespace |
| `make argocd-apply` | Register the Application with ArgoCD |
| `make argocd-status` | Show pods and sync status |
| `make argocd-forward` | Port-forward ArgoCD UI → https://localhost:8080 |
| `make argocd-password` | Print the initial admin password |
| `make argocd-sync` | Trigger an immediate manual sync |

---

## Tear Down

```bash
kubectl delete -f k8s/argocd-app.yaml
kubectl delete namespace argocd
```
