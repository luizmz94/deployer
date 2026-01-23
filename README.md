# Deployer - Atualizador de stacks Docker Compose via webhook

Microserviço FastAPI que recebe webhooks assinados e executa `docker compose config/pull/up` dentro de `/stacks/<stack>` (montado de `/opt/stacks`). Pensado para substituir o Watchtower com checagens de segurança e de estado antes de atualizar.

## Requisitos e segurança
- Necessário Docker com Docker Compose v2 no host (exposto pelo socket `docker.sock`).
- A imagem já traz `docker` CLI + `docker compose` plugin instalados para falar com o socket do host.
- Fail-closed: se `DEPLOY_SECRET` estiver vazio ou `STACKS_ROOT` não existir, o container nem inicia.
- Só roda deploy de stacks que existam em `/stacks/<stack>` **e já tenham serviços em execução** (checagem prévia via `docker compose ps --status=running`).
- HMAC obrigatório no header `X-Signature` com `hex(hmac_sha256(DEPLOY_SECRET, raw_body))`.
- Rate limit simples: 10 req/min por IP (ajustável via env).
- Logs estruturados em stdout (`event`, `stack`, `step`, `ok`, `exit_code`, `duration_ms`).

## Arquitetura de execução
- O container monta `/var/run/docker.sock` para conversar com o Docker do host.
- Os stacks do host `/opt/stacks` são montados como `/stacks` (read-only é suficiente).
- Se precisar de auth de registry diferente por stack, coloque um `.docker/config.json` dentro do diretório do stack; o app usará `DOCKER_CONFIG=/stacks/<stack>/.docker`.
- Cada deploy roda no diretório do stack (`cwd=/stacks/<stack>`):
  0. Checagem de status: `docker compose ps --status=running --services` (se nada rodando, aborta)
  1. `docker compose config`
  2. `docker compose pull`
  3. `docker compose up -d --remove-orphans`
- Timeouts: status 60s, config 120s, pull 600s, up 600s (ajustáveis via env).

## Variáveis de ambiente (.env)
Copie `.env.example` para `.env` e ajuste:
```
DEPLOY_SECRET=troque_este_valor
STACKS_ROOT=/stacks
RATE_LIMIT_PER_MIN=10
STATUS_TIMEOUT=60
CONFIG_TIMEOUT=120
PULL_TIMEOUT=600
UP_TIMEOUT=600
```
> Não versionar secrets: `.env` e `secrets/` ficam só no host. Use `.gitignore` no repositório dos stacks para evitar commit de `.env`, `secrets/` etc.

## Local test
```
deployer git:(main) export $(grep -v '^#' .env | xargs) && uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Build e subida
```
docker compose build
docker compose up -d
```
- Serviço escuta em `:8080` (mapeado no host). Healthcheck: `GET /health`.
- Ajuste o publish da porta ou proteja atrás de proxy/firewall conforme sua exposição.

## Assinatura HMAC
- Header: `X-Signature`
- Valor: `hex(hmac_sha256(DEPLOY_SECRET, raw_body))`
- `/deploy/{stack}`: se o body estiver vazio, assine exatamente o texto do stack. Ex.: `printf 'media' | openssl dgst -sha256 -hmac "$DEPLOY_SECRET" -binary | xxd -p -c256`
- `/deploy` (JSON): assine o corpo bruto enviado. Ex.:
```
payload='{"stack":"media"}'
sig=$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$DEPLOY_SECRET" -binary | xxd -p -c256)
curl -X POST http://host:8080/deploy \
  -H "Content-Type: application/json" \
  -H "X-Signature: $sig" \
  -d "$payload"
```

## Endpoints
- `GET /health` (ou `POST /health`) -> 200 OK.
- `POST /deploy/{stack}` -> dispara deploy; body pode ser vazio; assinar body vazio como o nome do stack.
- `POST /deploy` com JSON `{ "stack": "media" }` (alternativa).

Resposta JSON:
```
{
  "ok": true,
  "stack": "media",
  "steps": [
    {"name": "status", "ok": true, "duration_ms": 50, "tail": "..."},
    {"name": "config", "ok": true, "duration_ms": 350, "tail": "..."},
    {"name": "pull",   "ok": true, "duration_ms": 8200, "tail": "..."},
    {"name": "up",     "ok": true, "duration_ms": 1200, "tail": "..."}
  ],
  "started_at": "2024-01-01T12:00:00Z",
  "finished_at": "2024-01-01T12:01:00Z"
}
```

## Expondo para o GitHub Actions
Recomendação: usar Cloudflare Tunnel para evitar abrir a porta publicamente.
1. Crie um túnel no Cloudflare apontando para `http://localhost:8080` no host onde o deployer roda.
2. Proteja com Cloudflare Access (mTLS ou token header) se possível.
3. No pipeline do GitHub, chame o endpoint do túnel HTTPS.

Alternativa direta (menos recomendada): publicar 443/https atrás de Nginx/Caddy com Let’s Encrypt **e** restringir firewall aos IPs do GitHub Actions. Os blocos mudam ao longo do tempo; consulte periodicamente `https://api.github.com/meta` e atualize regras de firewall ou listas do proxy.

## Exemplo de workflow (GitHub Actions)
Workflow simples que assina o payload e falha se `ok=false`.
```
name: Deploy Stack
on:
  workflow_dispatch:
    inputs:
      stack:
        description: "Nome do stack"
        required: true
        default: "media"

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Preparar payload e assinatura
        id: sign
        run: |
          payload=$(jq -nc --arg stack "${{ github.event.inputs.stack }}" '{stack:$stack}')
          sig=$(printf '%s' "$payload" | openssl dgst -sha256 -hmac "$DEPLOY_SECRET" -binary | xxd -p -c256)
          echo "payload=$payload" >> "$GITHUB_OUTPUT"
          echo "sig=$sig" >> "$GITHUB_OUTPUT"
        env:
          DEPLOY_SECRET: ${{ secrets.DEPLOY_SECRET }}

      - name: Chamar deployer
        id: call
        run: |
          response=$(curl -sS -w '\n%{http_code}' -X POST "${{ secrets.DEPLOYER_URL }}/deploy" \
            -H "Content-Type: application/json" \
            -H "X-Signature: ${{ steps.sign.outputs.sig }}" \
            -d "${{ steps.sign.outputs.payload }}")

          body=$(echo "$response" | head -n1)
          status=$(echo "$response" | tail -n1)
          echo "Response: $body"
          echo "HTTP: $status"

          ok=$(echo "$body" | jq -r '.ok')
          if [ "$ok" != "true" ]; then
            echo "Deploy falhou" >&2
            exit 1
          fi
        shell: bash
```
- `DEPLOY_SECRET` e `DEPLOYER_URL` devem estar em `Actions Secrets`.
- `DEPLOYER_URL` deve apontar para o HTTPS do túnel/proxy.

## Gitignore sugerido para o repositório dos stacks
```
.env
.env.*
secrets/
*/secrets/
```

## Saúde e observabilidade
- Healthcheck no compose já chama `GET /health`.
- Logs estruturados em stdout; agregadores podem coletar diretamente do container.

## Troubleshooting
- 401: assinatura HMAC ausente ou incorreta.
- 409: stack sem serviços rodando (checagem de status).
- 404: stack não existe em `/stacks`.
- 429: rate limit.
- 500: algum comando falhou (ver `steps[].tail`).
