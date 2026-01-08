import asyncio
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Set

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("orchestrator.core.config")

REQUEST_ID_HEADER = os.getenv("ORCHESTRATOR_REQUEST_ID_HEADER", "X-Request-ID")
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
connection_ctx: ContextVar[Dict[str, Any]] = ContextVar("connection_ctx", default={})

CONFIG_ROOT = Path(__file__).resolve().parent.parent.parent / "config" / "environments"
# Assuming structure: orchestrator/core/config.py -> parent=core -> parent=orchestrator -> parent=root
# Actually: orchestrator/core/config.py. Parent is core. Parent.parent is orchestrator. Parent.parent.parent is root. Correct.

DEFAULT_ENVIRONMENT = "dev"
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}

class RequestIdLoggingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get("-")
        return True

def setup_logging():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s :: %(message)s",
    )
    _request_id_filter = RequestIdLoggingFilter()
    root_logger = logging.getLogger()
    root_logger.addFilter(_request_id_filter)
    for handler in root_logger.handlers:
        handler.addFilter(_request_id_filter)
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addFilter(_request_id_filter)

def _resolve_env_reference(raw_value: Optional[str]) -> Optional[str]:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return raw_value
    if raw_value.startswith("env:"):
        env_name = raw_value[4:].strip()
        if not env_name:
            raise RuntimeError("Empty environment variable reference in header config")
        env_value = os.getenv(env_name)
        if env_value is None:
            raise RuntimeError(
                f"Environment variable '{env_name}' required for header is not set"
            )
        return env_value
    return raw_value


class RetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1)
    backoff_initial_seconds: float = Field(default=0.2, ge=0.0)
    backoff_max_seconds: float = Field(default=5.0, ge=0.1)


class CircuitBreakerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, ge=1.0)


class RegionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    timeout_seconds: float = Field(default=10.0, ge=0.1)
    verify_tls: bool = False
    retry: RetrySettings = Field(default_factory=RetrySettings)
    circuit_breaker: Optional[CircuitBreakerSettings] = None
    default_headers: Dict[str, str] = Field(default_factory=dict)

    def resolved_headers(self) -> Dict[str, str]:
        resolved: Dict[str, str] = {}
        for header, raw_value in self.default_headers.items():
            value = _resolve_env_reference(raw_value)
            if value is not None:
                resolved[header] = value
        return resolved


class ServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_region: str = "default"
    regions: Dict[str, RegionSettings]

    def update_region(
        self,
        region_name: str,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        verify_tls: Optional[bool] = None,
    ) -> None:
        if region_name not in self.regions:
            raise KeyError(f"Region '{region_name}' not defined for service")
        region_cfg = self.regions[region_name]
        updates: Dict[str, Any] = {}
        if base_url is not None:
            updates["base_url"] = base_url
        if timeout_seconds is not None:
            updates["timeout_seconds"] = timeout_seconds
        if verify_tls is not None:
            updates["verify_tls"] = verify_tls
        if updates:
            self.regions[region_name] = region_cfg.model_copy(update=updates)


class EnvConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    env_name: str
    dry_run_default: bool
    services: Dict[str, ServiceSettings]

    def _normalize_service(self, service: str) -> str:
        return service.lower()

    def _resolve_region(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[RegionSettings, str]:
        service_key = self._normalize_service(service)
        if service_key not in self.services:
            raise KeyError(f"Unknown service '{service}'")
        service_cfg = self.services[service_key]
        env_region = os.getenv(f"{service_key.upper()}_REGION")
        region_name = (region or env_region or service_cfg.default_region).lower()
        if region_name not in service_cfg.regions:
            logger.warning(
                "Region '%s' not defined for service '%s'; using default '%s'",
                region_name,
                service_key,
                service_cfg.default_region,
            )
            region_name = service_cfg.default_region
        return service_cfg.regions[region_name], region_name

    def resolve_service_region(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[RegionSettings, str]:
        return self._resolve_region(service, region)

    def get_service_region(self, service: str, region: Optional[str] = None) -> RegionSettings:
        region_cfg, _ = self._resolve_region(service, region)
        return region_cfg

    def service_base_url(self, service: str, region: Optional[str] = None) -> str:
        return self.get_service_region(service, region).base_url

    def http_client_kwargs(
        self, service: str, region: Optional[str] = None
    ) -> Tuple[Dict[str, Any], str]:
        region_cfg, region_name = self._resolve_region(service, region)
        base_headers = region_cfg.resolved_headers()
        headers = base_headers if base_headers else None
        return (
            {
                "base_url": region_cfg.base_url,
                "timeout": region_cfg.timeout_seconds,
                "verify": region_cfg.verify_tls,
                "headers": headers,
            },
            region_name,
        )

    def override_default_region(
        self,
        service: str,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        verify_tls: Optional[bool] = None,
    ) -> None:
        service_key = self._normalize_service(service)
        if service_key not in self.services:
            raise KeyError(f"Unknown service '{service}'")
        service_cfg = self.services[service_key]
        service_cfg.update_region(
            service_cfg.default_region,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            verify_tls=verify_tls,
        )

    @property
    def isp_base_url(self) -> str:
        return self.service_base_url("isp")

    @property
    def geogrid_base_url(self) -> str:
        return self.service_base_url("geogrid")


def _load_environment_from_file(env_name: str) -> Dict[str, Any]:
    config_path = CONFIG_ROOT / f"{env_name}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found for env '{env_name}' at {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_env_config() -> EnvConfig:
    env_name = os.getenv("ORCHESTRATOR_ENV", DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT
    try:
        data = _load_environment_from_file(env_name)
    except FileNotFoundError:
        logger.warning(
            "Environment '%s' not found. Falling back to '%s'",
            env_name,
            DEFAULT_ENVIRONMENT,
        )
        env_name = DEFAULT_ENVIRONMENT
        data = _load_environment_from_file(env_name)

    data["env_name"] = env_name
    config = EnvConfig.model_validate(data)

    override_map = {
        "isp": os.getenv("ISP_BASE_URL"),
        "geogrid": os.getenv("GEOGRID_BASE_URL"),
    }
    for service_name, base_override in override_map.items():
        if base_override:
            config.override_default_region(service_name, base_url=base_override)

    dry_run_env = os.getenv("DRY_RUN")
    if dry_run_env is not None:
        config.dry_run_default = dry_run_env.lower() in {"1", "true", "yes", "on"}

    return config


SETTINGS = load_env_config()
APP_USER_HEADER = os.getenv("ORCHESTRATOR_USER_HEADER", "X-Orchestrator-User")

@dataclass
class RuntimeState:
    dry_run: bool = SETTINGS.dry_run_default


runtime_state = RuntimeState()

def get_settings() -> EnvConfig:
    return SETTINGS

def get_runtime_state() -> RuntimeState:
    return runtime_state

@dataclass
class CircuitBreakerState:
    failure_count: int = 0
    opened_at: Optional[float] = None


_circuit_states: Dict[str, CircuitBreakerState] = {}
_circuit_locks: Dict[str, asyncio.Lock] = {}


def get_circuit_state(key: str) -> CircuitBreakerState:
    state = _circuit_states.get(key)
    if state is None:
        state = CircuitBreakerState()
        _circuit_states[key] = state
    return state


def get_circuit_lock(key: str) -> asyncio.Lock:
    lock = _circuit_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _circuit_locks[key] = lock
    return lock
