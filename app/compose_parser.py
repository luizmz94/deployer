import re
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger("deployer")

def extract_env_vars_from_compose(compose_file: Path) -> Set[str]:
    """
    Lê docker-compose.yml e extrai todas as variáveis no formato ${VAR_NAME}
    
    Exemplo:
      GOOGLE_CLIENT_ID: "${GOOGLE_CLIENT_ID}"
      
    Retorna: {"GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", ...}
    """
    try:
        with open(compose_file, 'r') as f:
            content = f.read()
        
        # Regex para encontrar ${VAR_NAME} ou $VAR_NAME
        pattern = r'\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)'
        matches = re.findall(pattern, content)
        
        # Flatten tuplas e remover vazios
        var_names = {var for match in matches for var in match if var}
        
        return var_names
    except Exception as e:
        logger.error(f"Failed to parse compose file {compose_file}: {e}")
        return set()
