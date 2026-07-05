# Callspire Gateway for MikoPBX

One-command installer for a **Callspire** telephony stack on Linux:

1. **MikoPBX** (latest Docker image)
2. **Callspire PBX Gateway** (FastAPI — CDR, originate, admin, WebRTC, Kommo/AmoCRM)
3. **Callspire Web Softphone** (browser UI at `/softphone/`)

Everything ships in this repository — no external git clones at install time.

**Repository:** [github.com/Intteger157/Callspire.Gateway-for-MikoPBX](https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX)

## Quick install

On a fresh **Ubuntu 22.04+ / Debian 12+** server (root or sudo):

```bash
git clone https://github.com/Intteger157/Callspire.Gateway-for-MikoPBX.git
cd Callspire.Gateway-for-MikoPBX
sudo bash install.sh
```

Or one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/Intteger157/Callspire.Gateway-for-MikoPBX/main/install.sh | sudo bash
```

Interactive mode:

```bash
sudo bash install.sh --interactive
```

Default gateway admin: **admin / admin** — you must change the password on first login at `/admin`.

## What gets installed

| Component | Location | Service |
|-----------|----------|---------|
| MikoPBX Docker | container `mikopbx`, `--net=host` | `docker restart mikopbx` |
| PBX Gateway + Kommo | `/opt/callspire/pbx-gateway` | `pbx-gateway.service` |
| Web softphone (built SPA) | `/opt/callspire/web-softphone/dist` | served at `/softphone/` |
| Config | `/etc/callspire/config.yaml` | — |
| Env | `/etc/callspire/stack.env` | install metadata |

After install the script prints URLs, ports, and credentials.

## Repository layout

```
Callspire.Gateway-for-MikoPBX/
├── install.sh
├── gateway/              # Full FastAPI gateway (CDR, admin, Kommo)
├── web-softphone/dist/   # Prebuilt browser softphone SPA
├── packages/gateway-web-softphone/
├── compose/mikopbx.yml
├── systemd/pbx-gateway.service
└── scripts/
```

## Rebuild web softphone dist

Source lives in the separate `callspire-web-softphone` project. After UI changes:

```bash
cd callspire-web-softphone/softphone-web
npm ci && npm run build
rsync -a dist/ /path/to/Callspire.Gateway-for-MikoPBX/web-softphone/dist/
```

## Update

```bash
sudo callspire-update
```

## License

MIT — see [LICENSE](LICENSE).
