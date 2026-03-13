-include .env

.PHONY: venv install setup run health lambda-package

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