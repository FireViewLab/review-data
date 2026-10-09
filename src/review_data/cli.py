"""
로컬 수집/테스트용 CLI.

    crawler list                              등록된 collector 목록
    crawler collect                           대화형으로 선택해 상품 수집
    crawler reviews <platform> <product_id>   특정 상품의 리뷰 수집
    crawler worker                            수집 job 을 처리하는 워커 실행
    crawler serve                             결과 확인용 FastAPI 서버 실행
"""

import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Annotated

import questionary
import typer

from review_data.core.base import BaseCollector
from review_data.core.db.base import create_engine, create_session_factory, session_scope
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.discovery import discover
from review_data.core.exceptions import CollectorError, NotSupportedError
from review_data.core.service.product_discovery import ProductDiscoveryService
from review_data.core.service.scheduling import SchedulingService
from review_data.core.service.seeding import SeedingService, load_seed_file
from review_data.core.settings import ENV_FILE, env_file_exists, get_settings
from review_data.worker import collection_worker

app = typer.Typer(help="커머스 리뷰 수집 도구", no_args_is_help=True)


@app.command("status")
def status_command():
    """수집·분석 큐, 만료 lease, 리뷰0건 원인, 신규 발굴 위치를 JSON으로 확인합니다."""
    _require_env()
    asyncio.run(_status())


async def _status():
    from review_data.core.service.operations import operational_status

    engine = create_engine()
    try:
        async with create_session_factory(engine)() as session:
            result = await operational_status(session)
            settings = get_settings()
            result["discovery_settings"] = {
                "enabled": settings.discovery_enabled,
                "interval_seconds": settings.discovery_interval_seconds,
                "max_collection_pending": settings.schedule_max_pending,
                "max_analysis_pending": settings.discovery_max_analysis_pending,
            }
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await engine.dispose()


def _require_env() -> None:
    # 컨테이너는 .env 파일 없이 환경변수로 설정을 주입하므로, 접속 정보가 이미 있으면 통과시킨다.
    if not env_file_exists() and "DATABASE_URL" not in os.environ:
        typer.secho(f"\n❌ .env 파일이 없습니다: {ENV_FILE}", fg=typer.colors.RED, bold=True)
        typer.echo("   프로젝트 루트에서 아래 명령을 실행하세요:\n")
        typer.echo("     cp .env.example .env\n")
        raise typer.Exit(1)


def _load_registry() -> dict[str, type[BaseCollector]]:
    registry, failures = discover()
    for failure in failures:
        typer.secho(f"\n⚠️  [{failure.package}] 로드 실패", fg=typer.colors.YELLOW, bold=True)
        typer.echo(failure.traceback_text)
    return registry


@app.command("list")
def list_collectors() -> None:
    """등록된 collector 목록을 출력합니다."""
    _require_env()
    registry = _load_registry()

    if not registry:
        typer.echo("\n등록된 collector 가 없습니다.")
        typer.echo("collectors/_template/collector.py 를 복사해 자기 폴더에 만드세요.\n")
        raise typer.Exit()

    typer.echo("\n등록된 collector:")
    for name, cls in sorted(registry.items()):
        needs = (
            f"  (필요 설정: {', '.join(s.upper() for s in cls.required_settings)})"
            if cls.required_settings
            else ""
        )
        typer.echo(f"  • {name:<12} {cls.label or ''}{needs}")
    typer.echo()


@app.command()
def collect(
    keyword: str = typer.Option(None, "--keyword", "-k", help="검색 키워드"),
    limit: int = typer.Option(20, "--limit", "-n", help="수집 개수"),
    save: bool = typer.Option(True, "--save/--no-save", help="결과를 DB 에 저장"),
) -> None:
    """대화형으로 플랫폼을 선택해 상품을 수집합니다."""
    _require_env()
    registry = _load_registry()

    if not registry:
        typer.secho("사용 가능한 collector 가 없습니다.", fg=typer.colors.RED)
        raise typer.Exit(1)

    selected = questionary.checkbox(
        "수집할 플랫폼을 선택하세요 (스페이스: 선택 / 엔터: 확정)",
        choices=sorted(registry.keys()),
    ).ask()

    if not selected:
        typer.echo("선택된 플랫폼이 없습니다.")
        raise typer.Exit()

    if not keyword:
        keyword = questionary.text("검색 키워드를 입력하세요").ask()
    if not keyword:
        typer.echo("키워드가 없습니다.")
        raise typer.Exit()

    asyncio.run(_collect_products(registry, selected, keyword, limit, save))


@app.command()
def reviews(
    platform: str = typer.Argument(help="플랫폼 식별자 (crawler list 로 확인)"),
    product_id: str = typer.Argument(help="해당 플랫폼의 상품 ID"),
    limit: int = typer.Option(50, "--limit", "-n", help="수집 개수"),
    save: bool = typer.Option(True, "--save/--no-save", help="결과를 DB 에 저장"),
) -> None:
    """특정 상품의 리뷰를 수집합니다."""
    _require_env()
    registry = _load_registry()

    collector_cls = registry.get(platform)
    if collector_cls is None:
        typer.secho(
            f"'{platform}' collector 가 없습니다. 사용 가능: {sorted(registry)}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)

    asyncio.run(_collect_reviews(collector_cls, platform, product_id, limit, save))


async def _collect_products(
    registry: dict[str, type[BaseCollector]],
    platforms: list[str],
    keyword: str,
    limit: int,
    do_save: bool,
) -> None:
    for name in platforms:
        typer.secho(f"\n▶ [{name}] 상품 수집", fg=typer.colors.CYAN, bold=True)
        try:
            async with registry[name]() as collector:
                products = await collector.search_products(keyword, limit=limit)
            typer.echo(f"  {len(products)}건 수집")
            if do_save and products:
                async with session_scope() as session:
                    repo = ProductRepository(session)
                    for product in products:
                        await repo.upsert(product)
                typer.echo(f"  DB 저장 완료 ({len(products)}건)")
        except NotSupportedError as exc:
            typer.secho(f"  건너뜀: {exc}", fg=typer.colors.YELLOW)
        except CollectorError as exc:
            typer.secho(f"  실패: {exc}", fg=typer.colors.RED)
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"  예외: {type(exc).__name__}: {exc}", fg=typer.colors.RED)


async def _collect_reviews(
    collector_cls: type[BaseCollector],
    platform: str,
    product_id: str,
    limit: int,
    do_save: bool,
) -> None:
    typer.secho(f"\n▶ [{platform}] 리뷰 수집 (product_id={product_id})", fg=typer.colors.CYAN)
    try:
        async with collector_cls() as collector:
            items = await collector.get_reviews(product_id, limit=limit)
            # reviews 는 products 를 FK 로 참조하므로, 저장 전에 상품도 함께 확보한다.
            product = await collector.get_product(product_id) if do_save and items else None
        typer.echo(f"  {len(items)}건 수집")
        if do_save and items:
            async with session_scope() as session:
                await ProductRepository(session).upsert(product)
                await ReviewRepository(session).upsert_many(platform, product_id, items)
                await ProductRepository(session).mark_reviews_collected(platform, product_id)
                from review_data.core.db.analysis_repository import AnalysisRepository

                await AnalysisRepository(session).enqueue(platform, product_id, get_settings())
            typer.echo(f"  DB 저장 완료 ({len(items)}건)")
    except NotSupportedError as exc:
        typer.secho(f"  건너뜀: {exc}", fg=typer.colors.YELLOW)
    except CollectorError as exc:
        typer.secho(f"  실패: {exc}", fg=typer.colors.RED)
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"  예외: {type(exc).__name__}: {exc}", fg=typer.colors.RED)


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="job 을 하나만 처리하고 종료합니다"),
    poll_interval: float = typer.Option(
        5.0, "--poll-interval", help="처리할 job 이 없을 때 다음 확인까지 대기(초)"
    ),
) -> None:
    """수집 job 을 처리하는 워커를 실행합니다.

    API 는 TTL 이 지난 요청에 job 만 만들어두고 바로 응답합니다. 실제 크롤링은 이
    워커가 합니다. 워커를 띄우지 않으면 job 이 pending 으로 쌓이기만 합니다.
    """
    _require_env()
    # 워커는 오래 떠 있으므로 진행 상황과 실패가 보여야 한다.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    try:
        asyncio.run(_run_worker(once, poll_interval))
    except KeyboardInterrupt:
        typer.echo("\n워커를 종료합니다.")


async def _run_worker(once: bool, poll_interval: float) -> None:
    # 여러 워커가 같은 job 을 두고 경합할 때 누가 소유자인지 구분할 수 있어야 한다.
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    engine = create_engine()
    session_factory = create_session_factory(engine)
    try:
        if once:
            processed = await collection_worker.run_once(session_factory, worker_id)
            typer.echo("job 1건 처리 완료" if processed else "처리할 job 이 없습니다.")
            return
        typer.secho(f"워커 시작 (id={worker_id}). Ctrl+C 로 종료합니다.", fg=typer.colors.CYAN)
        await collection_worker.run_forever(session_factory, worker_id, poll_interval=poll_interval)
    finally:
        await engine.dispose()


@app.command()
def seed(
    file: Annotated[Path, typer.Option("--file", help="기본 수집 대상 TOML 파일")] = Path(
        "seeds.toml"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="요청·저장 없이 실행 계획만 출력합니다"),
) -> None:
    """조회가 없는 빈 서버에도 상품과 수집 대상을 마련합니다."""
    _require_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    try:
        asyncio.run(_run_seed(file, dry_run))
    except (OSError, ValueError) as exc:
        typer.echo(f"시드 파일 오류: {exc}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("\n시드를 종료합니다.")


async def _run_seed(file: Path, dry_run: bool) -> None:
    registry = _load_registry()
    plan = load_seed_file(file, registry)
    engine = create_engine()
    session_factory = create_session_factory(engine)
    try:
        results = await SeedingService(registry, session_factory).run(plan, dry_run=dry_run)
        for platform, result in results.items():
            if result.skipped:
                typer.echo(f"[{platform}] 제외 플랫폼: 건너뜀")
                continue
            if dry_run:
                for keyword in plan.keywords.get(platform, ()):
                    typer.echo(f"[{platform}] 검색 예정: {keyword} (최대 {plan.per_keyword}개)")
                for product_id in plan.products.get(platform, ()):
                    typer.echo(f"[{platform}] 예약 예정: {product_id}")
                if platform in plan.expand:
                    typer.echo(
                        f"[{platform}] 지정 상품마다 관련 상품 "
                        f"최대 {plan.expand[platform]}개 예약 예정"
                    )
            typer.echo(
                f"[{platform}] 저장 {result.saved}건, 예약 {result.created}건, "
                f"실패 {len(result.errors)}건"
            )
            for error in result.errors:
                typer.echo(f"  {error}")
    finally:
        await engine.dispose()


@app.command()
def scheduler(
    once: bool = typer.Option(False, "--once", help="한 번만 예약하고 종료합니다"),
    interval: float | None = typer.Option(
        None,
        "--interval",
        min=1.0,
        help="예약 주기(초). 비우면 SCHEDULE_INTERVAL_SECONDS 를 씁니다",
    ),
) -> None:
    """낡은 상품을 찾아 수집 job 을 주기적으로 예약합니다.

    조회가 없어도 DB 에 있는 상품을 다시 수집하게 합니다. 실제 크롤링은 워커가 하므로
    `crawler worker` 가 함께 떠 있어야 합니다.
    """
    _require_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    try:
        asyncio.run(_run_scheduler(once, interval))
    except KeyboardInterrupt:
        typer.echo("\n스케줄러를 종료합니다.")


async def _run_scheduler(once: bool, interval: float | None) -> None:
    logger = logging.getLogger("review_data.scheduler")
    wait = interval if interval is not None else get_settings().schedule_interval_seconds
    engine = create_engine()
    session_factory = create_session_factory(engine)
    settings = get_settings()
    registry = _load_registry() if settings.discovery_enabled else {}
    discovery_service = ProductDiscoveryService(session_factory, registry, settings)
    try:
        while True:
            try:
                result = await discovery_service.run_once()
                if result["attempted"]:
                    logger.info("신규 발굴 %s", result)
            except Exception:
                if once:
                    raise
                logger.exception("신규 발굴 오류. 기존 상품 갱신은 계속합니다.")
            try:
                async with session_factory() as session:
                    result = await SchedulingService(session).run_once()
                    await session.commit()
                logger.info("예약 %d건 (예약 전 대기 %d건)", result.created, result.pending_before)
            except Exception:  # noqa: BLE001 - DB 가 잠깐 끊겨도 다음 주기에 다시 시도한다
                if once:
                    raise
                logger.exception("예약 중 오류. 다음 주기에 다시 시도합니다.")
            if once:
                return
            await asyncio.sleep(wait)
    finally:
        await engine.dispose()


@app.command()
def serve(
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(True, "--reload/--no-reload"),
) -> None:
    """수집 결과를 확인할 수 있는 FastAPI 서버를 실행합니다."""
    _require_env()
    import uvicorn

    uvicorn.run("review_data.api.app:app", host="127.0.0.1", port=port, reload=reload)


@app.command("analysis-worker")
def analysis_worker_command(
    once: bool = typer.Option(False, "--once"),
    poll_interval: float = typer.Option(5, "--poll-interval", min=0.1),
) -> None:
    """저장된 리뷰를 분석하는 독립 워커를 실행합니다."""
    _require_env()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run_analysis(once, poll_interval, backfill=False, force=False))
    except KeyboardInterrupt:
        typer.echo("분석 워커를 종료합니다.")


@app.command("analysis-backfill")
def analysis_backfill(force: bool = typer.Option(False, "--force")) -> None:
    """기존 리뷰에 분석 작업을 예약합니다. --force는 같은 입력도 다시 분석합니다."""
    _require_env()
    asyncio.run(_run_analysis(True, 5, backfill=True, force=force))


async def _run_analysis(once: bool, poll_interval: float, *, backfill: bool, force: bool):
    from sqlalchemy import select

    from review_data.core.db.analysis_repository import AnalysisRepository
    from review_data.core.db.models import AnalysisJob, ProductRow
    from review_data.worker import analysis_worker

    settings = get_settings()
    if not settings.ai_analysis_enabled and (once or backfill):
        typer.echo("분석 연결이 비활성화되어 있습니다.")
        return
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        if backfill:
            async with factory() as session:
                products = (
                    await session.execute(
                        select(ProductRow.platform, ProductRow.product_id).where(
                            ProductRow.reviews_last_collected_at.is_not(None)
                        )
                    )
                ).all()
            count = 0
            for platform, product_id in products:
                async with factory() as session:
                    previous = await session.scalar(
                        select(AnalysisJob.id)
                        .where(
                            AnalysisJob.platform == platform, AnalysisJob.product_id == product_id
                        )
                        .order_by(AnalysisJob.id.desc())
                        .limit(1)
                    )
                    current = await AnalysisRepository(session).enqueue(
                        platform, product_id, settings, force=force
                    )
                    await session.commit()
                    count += current is not None and current != previous
            typer.echo(f"분석 작업 {count}건 예약 완료")
        else:
            worker_id = f"analysis-{socket.gethostname()}-{os.getpid()}"
            if once:
                processed = await analysis_worker.run_once(factory, worker_id, settings=settings)
                typer.echo(
                    "분석 작업 1건 처리 완료" if processed else "처리할 분석 작업이 없습니다."
                )
            else:
                await analysis_worker.run_forever(
                    factory, worker_id, settings=settings, poll_interval=poll_interval
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    app()
