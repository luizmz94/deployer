import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

# Configuration defaults
RATE_LIMIT_WINDOW_SECONDS = 60
TAIL_LIMIT = 2000

STACK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SENSITIVE_RE = re.compile(
    r"(?im)^\s*([^\s:=]*(?:secret|token|password|passwd|pwd|key)[^:=]*?)\s*[:=]\s*([^\n\r]+)"
)


class Settings:
    def __init__(self) -> None:
        self.deploy_secret = os.environ.get("DEPLOY_SECRET", "").encode()
        self.stacks_root = Path(os.environ.get("STACKS_ROOT", "/stacks"))
        self.rate_limit_per_min = int(os.environ.get("RATE_LIMIT_PER_MIN", "10"))
        self.status_timeout = int(os.environ.get("STATUS_TIMEOUT", "60"))
        self.config_timeout = int(os.environ.get("CONFIG_TIMEOUT", "120"))
        self.pull_timeout = int(os.environ.get("PULL_TIMEOUT", "600"))
        self.up_timeout = int(os.environ.get("UP_TIMEOUT", "600"))

        if not self.deploy_secret:
            raise SystemExit("DEPLOY_SECRET must be set (fail-closed).")

        if not self.stacks_root.exists():
            raise SystemExit(f"Stacks root not found: {self.stacks_root}")


settings = Settings()


# Structured JSON logger
logger = logging.getLogger("deployer")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def log_event(event: Dict[str, Any]) -> None:
    payload = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    logger.info(json.dumps(payload, default=str))


# Rate limiter
request_buckets: Dict[str, deque] = defaultdict(deque)
rate_limit_lock = Lock()


app = FastAPI(title="Deployer", version="1.0.0")


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with rate_limit_lock:
        bucket = request_buckets[client_ip]
        window_start = now - RATE_LIMIT_WINDOW_SECONDS
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_per_min:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
            )
        bucket.append(now)

    response = await call_next(request)
    return response


def validate_stack_name(name: str) -> str:
    if not STACK_NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid stack name",
        )
    return name


def get_stack_path(stack: str) -> Path:
    stack_path = (settings.stacks_root / stack).resolve()
    try:
        settings_root = settings.stacks_root.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stacks root missing",
        )

    if settings_root not in stack_path.parents and stack_path != settings_root:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid stack path",
        )
    if not stack_path.exists() or not stack_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="stack not found",
        )
    compose_file = stack_path / "docker-compose.yml"
    if not compose_file.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="docker-compose.yml missing",
        )
    return stack_path


def compute_signature(data: bytes) -> str:
    return hmac.new(settings.deploy_secret, data, hashlib.sha256).hexdigest()


def verify_signature(signature_header: str, data: bytes) -> None:
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing signature",
        )
    expected = compute_signature(data)
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )


def get_docker_env(stack_path: Path) -> Dict[str, str]:
    cfg_dir = stack_path / ".docker"
    cfg_file = cfg_dir / "config.json"
    if cfg_file.exists():
        return {"DOCKER_CONFIG": str(cfg_dir)}
    return {}


def run_command(name: str, args: List[str], cwd: Path, timeout: int) -> Dict[str, Any]:
    return run_command_env(name, args, cwd, timeout, env=None)


def run_command_env(
    name: str, args: List[str], cwd: Path, timeout: int, env: Optional[Dict[str, str]]
) -> Dict[str, Any]:
    started = time.time()
    try:
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=proc_env,
        )
        duration_ms = int((time.time() - started) * 1000)
        def sanitize_output(text: str) -> str:
            return SENSITIVE_RE.sub(lambda m: f"{m.group(1)}: ***", text)

        output_combined = sanitize_output((proc.stdout or "") + (proc.stderr or ""))
        tail = output_combined[-TAIL_LIMIT:]
        result = {
            "name": name,
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "tail": tail,
        }
        log_event(
            {
                "event": "step",
                "stack": cwd.name,
                "step": name,
                "ok": result["ok"],
                "exit_code": proc.returncode,
                "duration_ms": duration_ms,
            }
        )
        return result
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.time() - started) * 1000)
        log_event(
            {
                "event": "step_timeout",
                "stack": cwd.name,
                "step": name,
                "duration_ms": duration_ms,
            }
        )
        return {
            "name": name,
            "ok": False,
            "exit_code": None,
            "duration_ms": duration_ms,
            "tail": f"timeout after {timeout}s: {exc}",
        }


def build_response(stack: str, steps: List[Dict[str, Any]], started_at: datetime) -> JSONResponse:
    finished_at = datetime.now(timezone.utc)
    ok = all(step.get("ok") for step in steps)
    content = {
        "ok": ok,
        "stack": stack,
        "steps": steps,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
    }
    status_code = status.HTTP_200_OK if ok else status.HTTP_500_INTERNAL_SERVER_ERROR
    return JSONResponse(status_code=status_code, content=content)


async def perform_deploy(stack: str) -> JSONResponse:
    validate_stack_name(stack)
    stack_path = get_stack_path(stack)
    docker_env = get_docker_env(stack_path)
    started_at = datetime.now(timezone.utc)
    log_event(
        {
            "event": "deploy_start",
            "stack": stack,
            "docker_config": docker_env.get("DOCKER_CONFIG"),
        }
    )

    steps: List[Dict[str, Any]] = []
    status_result = await asyncio.to_thread(
        run_command_env,
        "status",
        ["docker", "compose", "ps", "--status=running", "--services"],
        stack_path,
        settings.status_timeout,
        docker_env,
    )
    services = [line.strip() for line in status_result.get("tail", "").splitlines() if line.strip()]
    if status_result["ok"] and not services:
        status_result["ok"] = False
        status_result["tail"] = (status_result.get("tail") or "") + "\nNo running services found; aborting deploy."
    steps.append(status_result)

    if not steps[-1]["ok"]:
        log_event(
            {
                "event": "deploy_done",
                "stack": stack,
                "ok": False,
            }
        )
        return build_response(stack, steps, started_at)

    steps.append(
        await asyncio.to_thread(
            run_command_env,
            "config",
            ["docker", "compose", "config"],
            stack_path,
            settings.config_timeout,
            docker_env,
        )
    )

    if steps[-1]["ok"]:
        steps.append(
            await asyncio.to_thread(
                run_command_env,
                "pull",
                ["docker", "compose", "pull"],
                stack_path,
                settings.pull_timeout,
                docker_env,
            )
        )

    if steps[-1]["ok"]:
        steps.append(
            await asyncio.to_thread(
                run_command_env,
                "up",
                ["docker", "compose", "up", "-d", "--remove-orphans"],
                stack_path,
                settings.up_timeout,
                docker_env,
            )
        )

    log_event(
        {
            "event": "deploy_done",
            "stack": stack,
            "ok": all(step.get("ok") for step in steps),
        }
    )
    return build_response(stack, steps, started_at)


@app.api_route("/health", methods=["GET", "POST"])
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/deploy/{stack}")
async def deploy_stack(stack: str, request: Request):
    body = await request.body()
    raw_for_signature = body if body else stack.encode()
    verify_signature(request.headers.get("X-Signature", ""), raw_for_signature)
    return await perform_deploy(stack)


@app.post("/deploy")
async def deploy_body(request: Request):
    body = await request.body()
    verify_signature(request.headers.get("X-Signature", ""), body)

    try:
        payload = json.loads(body.decode() or "{}")
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid JSON payload",
        )

    stack = payload.get("stack")
    if not stack:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stack is required",
        )

    return await perform_deploy(str(stack))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log_event({"event": "error", "error": str(exc)})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"ok": False, "detail": "internal server error"},
    )
