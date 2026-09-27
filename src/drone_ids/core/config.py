"""
Drone IDS - Simple Configuration Module
"""
import yaml
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    """Simple configuration manager."""
    
    _instance: Optional['Config'] = None
    _config: Dict[str, Any] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def load(self, config_path: str = None) -> 'Config':
        if config_path is None:
            # Config is at project root: /home/vishal/Documents/drone_sim/drone_ids/config/
            config_path = Path(__file__).parent.parent.parent.parent / "config" / "ids_config.yaml"
        with open(config_path, 'r') as f:
            self._config = yaml.safe_load(f)
        return self
    
    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value
    
    def section(self, name: str) -> Dict[str, Any]:
        return self._config.get(name, {})


config = Config()