"""docker-compose.yml: 모든 서비스의 로그에 크기·개수 제한이 걸려 있다."""

from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_every_service_rotates_logs():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    missing = [
        name
        for name, service in services.items()
        if not {"max-size", "max-file"} <= set((service.get("logging") or {}).get("options", {}))
    ]
    assert services and missing == []
