# When

Paste a five-field cron expression. Get a plain reading and the next run times.

Live: https://when.hermz.xyz

## Limits

- Five fields, or @hourly @daily @weekly @monthly @yearly
- No seconds field, no @reboot
- Crontab cron, not systemd OnCalendar
- 8 next runs, 60 explains per IP per hour

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8881
```

Health: `GET /health`

MIT. Built on holy-hermes.
