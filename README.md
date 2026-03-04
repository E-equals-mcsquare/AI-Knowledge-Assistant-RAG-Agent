Once the agent is running,

## Upload a document

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@/path/to/your-doc.pdf"
```

## Ask a question

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "How do we handle database failover?"}'
```

## Health check

```bash
curl http://localhost:8000/health
```
