# Screenshots checklist

Save every screenshot in `docs/screenshots/` with the file name given. Take them on the real running
stack: start with `scripts\start-all.ps1` at least 30 minutes earlier so Grafana and the AIRA baselines
have history. Use `Win + Shift + S` (Snipping Tool) and include the browser address bar.

| # | File name | What to show | How to get there |
|---|---|---|---|
| 1 | `01-shop.png` | Astronomy Shop home page with products | http://localhost:8080/ |
| 2 | `02-loadgen.png` | Locust statistics: requests/s, users, few failures | http://localhost:8080/loadgen/ |
| 3 | `03-grafana-normal.png` | Grafana "Demo Dashboard" (or "Spanmetrics Demo Dashboard") under normal load | http://localhost:8080/grafana/ → Dashboards |
| 4 | `04-flags-ui.png` | Feature flag UI with one fault flag switched on | http://localhost:8080/feature/ (after `inject-fault.ps1 -Scenario payment-failure`) |
| 5 | `05-grafana-fault.png` | The same Grafana dashboard during the fault: error spike for payment/checkout | Grafana, time range "Last 15 minutes", 1–2 min after injecting |
| 6 | `06-jaeger-error.png` | Jaeger trace with a red error span (payment `Charge` "Invalid token") | http://localhost:8080/jaeger/ui/ → Service `checkout` → Tags `error=true` → open a trace |
| 7 | `07-prometheus-pending.png` | Prometheus Alerts page with an AIRA alert **pending** (yellow) | http://localhost:9090/alerts, about 60–90 s after injecting |
| 8 | `08-prometheus-firing.png` | The same alert **firing** (red), expanded to show labels and annotations | http://localhost:9090/alerts |
| 9 | `09-alertmanager.png` | Alertmanager showing the alert group (alertname + service_name) | http://localhost:9093/ |
| 10 | `10-aira-console-firing.png` | AIRA console: firing incident with metrics evidence **and** the Triage Agent summary | http://localhost:8000/ |
| 11 | `11-aira-console-resolved.png` | The same incident shown **resolved** with its duration | http://localhost:8000/ after `clear-fault.ps1` |
| 12 | `12-service-down.png` | AIRA console with an `AiraServiceDown` incident ("LAST REPORT … AGO - the container is not running") | `inject-fault.ps1 -Scenario service-down` |
| 13 | `13-high-cpu-grafana.png` | ad container CPU spike (~400 % of a core) | Grafana or Prometheus graph of `rate(container_cpu_usage_nanoseconds_total{container_name="ad"}[1m]) / 1e7` |
| 14 | `14-status-script.png` | Terminal output of `scripts\status.ps1` with everything `[OK]` | PowerShell |
| 15 | `15-start-script.png` | Terminal output of `scripts\start-all.ps1` ending with "AIRA stack is up and healthy" | PowerShell |
| 16 | `16-api-incident.png` | Raw JSON of one incident (evidence + agent_output) | http://localhost:8000/incidents/1 (Firefox/Edge show formatted JSON) |
| 17 | `17-gpu-model.png` | `ollama ps` showing `qwen2.5:7b-instruct` 100 % GPU | PowerShell |

Tips
* Take 7, 8, 9 and 10 during the same fault so the timestamps line up.
* For a clean console, run `scripts\reset-incidents.ps1` before the screenshot session.
* After the session, run `scripts\clear-all-faults.ps1`.
