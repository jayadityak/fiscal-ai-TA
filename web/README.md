# Frontend

Read-only review interface for the committed FiscalAI artifacts.

```bash
python3 ../scripts/export_frontend.py
npm install
npm run dev
```

The browser performs no extraction, arithmetic, or LLM calls. It renders the
generated payload in `app/data.generated.json`.
