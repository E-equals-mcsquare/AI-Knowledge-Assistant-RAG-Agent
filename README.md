Once the agent is running,

## Upload a document

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@/Users/souvikmajumder/Documents/AI_Knowledge_Assistant/AI-Knowledge-Assistant-RAG-Agent/documents/Database_Failover_Guide.pdf"
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
