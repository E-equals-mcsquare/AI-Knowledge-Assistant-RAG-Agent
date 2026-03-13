# GitHub Actions IAM Setup (OIDC)

One-time setup to allow GitHub Actions to authenticate to AWS without storing long-lived access keys.

---

## Step 1 — Create the OIDC Identity Provider in AWS

This only needs to be done once per AWS account.

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

---

## Step 2 — Create the IAM Role

```bash
aws iam create-role \
  --role-name github-actions-deploy \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {
          "Federated": "arn:aws:iam::XXXXXXXXXX:oidc-provider/token.actions.githubusercontent.com"
        },
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
          "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
          },
          "StringLike": {
            "token.actions.githubusercontent.com:sub": "repo:<github-org>/AI-Knowledge-Assistant-RAG-Agent:*"
          }
        }
      }
    ]
  }'
```

---

## Step 3 — Attach ECR Permissions

```bash
aws iam put-role-policy \
  --role-name github-actions-deploy \
  --policy-name ecr-push \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "ecr:GetAuthorizationToken"
        ],
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:DescribeRepositories",
          "ecr:CreateRepository"
        ],
        "Resource": "arn:aws:ecr:ap-south-1:XXXXXXXXXXX:repository/ai-knowledge-assistant"
      }
    ]
  }'
```

---

## Step 4 — Add Secret to GitHub

```
GitHub repo → Settings → Secrets and variables → Actions → New repository secret

Name:  AWS_ROLE_ARN
Value: arn:aws:iam::XXXXXXXXXX:role/github-actions-deploy
```

---

## Optional: Add GitHub Variables

Instead of hardcoding in the workflow, set these under **Settings → Variables → Actions**:

| Variable       | Value                                         |
| -------------- | --------------------------------------------- |
| `AWS_REGION`   | `ap-south-1`                                  |
| `ECR_REGISTRY` | `XXXXXXXXXX.dkr.ecr.ap-south-1.amazonaws.com` |
| `ECR_REPO`     | `ai-knowledge-assistant`                      |

---

## Optional: ArgoCD Immediate Sync

To enable the commented-out ArgoCD sync step in the workflow:

1. In ArgoCD UI → **Settings → Accounts** → create a service account or use `admin`
2. Generate a token: `argocd account generate-token --account admin`
3. Expose ArgoCD server externally (LoadBalancer or Ingress) or use a self-hosted GitHub runner inside the cluster
4. Add to GitHub Secrets:
   - `ARGOCD_SERVER` — e.g. `argocd.yourdomain.com` (no `https://`)
   - `ARGOCD_TOKEN` — token from step 2
5. Uncomment the "Trigger ArgoCD sync" step in `.github/workflows/deploy.yml`
