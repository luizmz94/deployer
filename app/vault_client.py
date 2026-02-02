import hvac
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("deployer")

class VaultClient:
    def __init__(self, addr: str, role_id: str, secret_id: str):
        self.addr = addr
        self.role_id = role_id
        self.secret_id = secret_id
        self.client = hvac.Client(url=addr)
        self._token = None
        self._token_expires = None
        
    def authenticate(self) -> None:
        """Autentica via AppRole e armazena o token"""
        try:
            response = self.client.auth.approle.login(
                role_id=self.role_id,
                secret_id=self.secret_id
            )
            self._token = response['auth']['client_token']
            ttl = response['auth']['lease_duration']
            self._token_expires = datetime.now() + timedelta(seconds=ttl - 60)
            self.client.token = self._token
            logger.info(f"Vault authenticated successfully")
        except Exception as e:
            logger.error(f"Vault authentication failed: {e}")
            raise
    
    def _ensure_authenticated(self) -> None:
        """Garante que está autenticado, renovando se necessário"""
        if not self._token or (self._token_expires and datetime.now() >= self._token_expires):
            self.authenticate()
    
    def get_secrets(self, path: str) -> Dict[str, str]:
        """Busca secrets de um path no KV v2"""
        self._ensure_authenticated()
        try:
            response = self.client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point='kv'
            )
            return response['data']['data']
        except Exception as e:
            logger.warning(f"No secrets found at {path}: {e}")
            return {}
    
    def get_all_secrets_for_stack(self, stack_name: str, paths: List[str]) -> Dict[str, str]:
        """Busca secrets de múltiplos paths e mescla em um único dict"""
        all_secrets = {}
        for path in paths:
            secrets = self.get_secrets(path)
            all_secrets.update(secrets)
            if secrets:
                logger.info(f"[{stack_name}] Loaded {len(secrets)} secrets from {path}")
        return all_secrets
