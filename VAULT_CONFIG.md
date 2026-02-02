# Configuração do Deployer com Vault

## Variáveis de Ambiente

### Obrigatórias
- `DEPLOY_SECRET`: Secret para autenticação do webhook
- `STACKS_ROOT`: Diretório raiz das stacks (padrão: /stacks)

### Opcionais - Vault AppRole
Se configuradas, o deployer buscará secrets automaticamente do Vault:

```bash
VAULT_ADDR=https://vault.redware.io
VAULT_ROLE_ID=
VAULT_SECRET_ID=
```

Se NÃO configuradas, o deployer funcionará normalmente sem integração com Vault.

## Padrão de Nomenclatura

Para facilitar o mapeamento entre stacks e paths do Vault:

**Path no Vault:** `prd/thread_db`  
**Nome da stack/pasta:** `prd-thread_db`  
**Nome do container:** `prd-thread_db`

O deployer faz automaticamente `replace('-', '/')` para encontrar o path correto no Vault.

### Exemplos

| Stack/Pasta       | Path no Vault      |
|-------------------|--------------------|
| `prd-thread_db`   | `prd/thread_db`    |
| `prd-thread_hasura` | `prd/thread_hasura` |
| `others-deployer` | `others/deployer`  |
| `stg-api`         | `stg/api`          |

## Exemplo de stack.env

```bash
# Obrigatório
DEPLOY_SECRET=seu_secret_aqui

# Opcional - Vault (deixe vazio para desabilitar)
VAULT_ADDR=https://vault.redware.io
VAULT_ROLE_ID=
VAULT_SECRET_ID=
```

## Fluxo com Vault Habilitado

1. Webhook recebe request de deploy para "prd-thread_db"
2. Converte stack para path: `prd-thread_db` → `prd/thread_db`
3. Lê `/stacks/prd-thread_db/docker-compose.yml`
4. Identifica variáveis: `${POSTGRES_PASSWORD}`, `${DB_USER}`, etc
5. Busca secrets em `prd/thread_db` no Vault
6. Filtra apenas as que o compose precisa
7. Injeta via `docker_env`
8. Executa `docker compose up` com as ENVs corretas

## Build e Deploy

```bash
# Build
docker build -t gitredware/deployer:latest .
docker push gitredware/deployer:latest

# Deploy
docker compose pull
docker compose up -d
```
