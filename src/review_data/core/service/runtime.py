"""DB heartbeat와 읽기 전용 Linux 자원 계측."""

import asyncio
import contextlib
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)


@asynccontextmanager
async def runtime_heartbeat(factory, worker_id: str, role: str):
    async def pulse():
        while True:
            try:
                async with factory() as session:
                    await session.execute(
                        text("""
                        INSERT INTO worker_heartbeats(worker_id,role,last_seen_at,stopped_at)
                        VALUES (:worker_id,:role,now(),NULL)
                        ON CONFLICT(worker_id) DO UPDATE
                        SET last_seen_at=now(),stopped_at=NULL
                    """),
                        {"worker_id": worker_id, "role": role},
                    )
                    await session.execute(
                        text("""
                        DELETE FROM worker_heartbeats WHERE last_seen_at < now()-interval '7 days'
                    """)
                    )
                    await session.commit()
            except Exception:
                logger.warning("운영 heartbeat 저장 실패")
            await asyncio.sleep(10)

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(Exception):
            async with factory() as session:
                await session.execute(
                    text("""
                    UPDATE worker_heartbeats SET stopped_at=now() WHERE worker_id=:worker_id
                """),
                    {"worker_id": worker_id},
                )
                await session.commit()


class ResourceSampler:
    """누적 CPU tick의 차이로 측정한다. 최초 표본은 사용률을 알 수 없다."""

    def __init__(self, proc: Path | None = None):
        self.proc = proc or Path("/host/proc" if Path("/host/proc/stat").exists() else "/proc")
        self.previous = None

    def sample(self):
        result = {
            "scope": "host" if str(self.proc) == "/host/proc" else "local",
            "cpu_percent": None,
            "memory": None,
            "load": None,
            "disk": None,
        }
        try:
            # guest/guest_nice는 user/nice에 이미 포함된다.
            ticks = [int(x) for x in (self.proc / "stat").read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(ticks), ticks[3] + ticks[4]
            if self.previous and total > self.previous[0]:
                result["cpu_percent"] = round(
                    100 * (1 - (idle - self.previous[1]) / (total - self.previous[0])), 1
                )
            self.previous = total, idle
            info = {}
            for line in (self.proc / "meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                info[key] = int(value.strip().split()[0]) * 1024
            total_mem = info["MemTotal"]
            available = info["MemAvailable"]
            result["memory"] = {
                "total": total_mem,
                "used": total_mem - available,
                "percent": round(100 * (total_mem - available) / total_mem, 1),
            }
            result["load"] = [float(x) for x in (self.proc / "loadavg").read_text().split()[:3]]
            result["cpu_count"] = (
                sum(
                    line.startswith("cpu") and line[3:4].isdigit()
                    for line in (self.proc / "stat").read_text().splitlines()
                )
                or os.cpu_count()
            )
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            disk = shutil.disk_usage("/app" if Path("/app").exists() else ".")
            result["disk"] = {
                "total": disk.total,
                "used": disk.used,
                "percent": round(100 * disk.used / disk.total, 1),
                "scope": "API 파일시스템",
            }
        except OSError:
            pass
        return result
