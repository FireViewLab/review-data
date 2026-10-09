"""빈 서버에도 수집 대상을 마련한다. 리뷰는 기존 스케줄러와 워커에 맡긴다."""

import logging
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from review_data.core.base import BaseCollector
from review_data.core.db.repository import CollectionJobRepository, ProductRepository
from review_data.core.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeedPlan:
    per_keyword: int
    keywords: dict[str, tuple[str, ...]]
    products: dict[str, tuple[str, ...]]
    # 지정 상품마다 같은 판매처의 상품을 몇 개까지 더 찾을지. 검색이 안 되는 플랫폼용.
    expand: dict[str, int] = field(default_factory=dict)


def load_seed_file(path: Path, registry: Mapping[str, type[BaseCollector]]) -> SeedPlan:
    """실행 전에 전체 파일을 검증해 오타 때문에 일부만 저장되는 것을 막는다."""
    with path.open("rb") as file:
        data = tomllib.load(file)
    unknown = data.keys() - {"per_keyword", "keywords", "products", "expand"}
    if unknown:
        raise ValueError(f"지원하지 않는 시드 종류: {', '.join(sorted(unknown))}")
    limit = data.get("per_keyword", 20)
    if type(limit) is not int or limit <= 0:
        raise ValueError("per_keyword 는 양의 정수여야 합니다.")

    def section(name: str) -> dict[str, tuple[str, ...]]:
        entries = data.get(name, {})
        if not isinstance(entries, dict):
            raise ValueError(f"{name} 는 플랫폼별 테이블이어야 합니다.")
        result = {}
        for platform, values in entries.items():
            if platform not in registry:
                raise ValueError(f"등록되지 않은 플랫폼: {platform}")
            if not isinstance(values, list) or not values:
                raise ValueError(f"{name}.{platform} 는 비어 있지 않은 목록이어야 합니다.")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name}.{platform} 에는 빈 값이 아닌 문자열만 넣으세요.")
            result[platform] = tuple(dict.fromkeys(value.strip() for value in values))
        return result

    products = section("products")
    expand = data.get("expand", {})
    if not isinstance(expand, dict):
        raise ValueError("expand 는 플랫폼별 테이블이어야 합니다.")
    for platform, count in expand.items():
        if platform not in products:
            raise ValueError(f"expand.{platform} 는 products.{platform} 가 있어야 합니다.")
        if type(count) is not int or count <= 0:
            raise ValueError(f"expand.{platform} 는 양의 정수여야 합니다.")
    return SeedPlan(limit, section("keywords"), products, dict(expand))


@dataclass
class SeedResult:
    saved: int = 0
    created: int = 0
    skipped: bool = False
    errors: list[str] = field(default_factory=list)


class SeedingService:
    def __init__(
        self,
        registry: Mapping[str, type[BaseCollector]],
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def run(self, plan: SeedPlan, *, dry_run: bool = False) -> dict[str, SeedResult]:
        """종류별 처리를 나눠 새 시드 종류가 기존 검색 흐름을 바꾸지 않게 한다."""
        results = {
            platform: SeedResult(skipped=platform in self.settings.excluded_platforms())
            for platform in dict.fromkeys([*plan.keywords, *plan.products])
        }
        searched = False
        for platform, keywords in plan.keywords.items():
            result = results[platform]
            if result.skipped:
                continue
            for keyword in keywords:
                logger.info("[%s] 검색: %s (최대 %d개)", platform, keyword, plan.per_keyword)
                if dry_run:
                    continue
                try:
                    # 실패한 키워드의 브라우저 상태가 다음 검색까지 전파되지 않게 정리한다.
                    async with self.registry[platform](settings=self.settings) as collector:
                        if searched:
                            await collector.polite_wait()
                        searched = True
                        result.saved += await self._seed_keyword(
                            collector, keyword, plan.per_keyword
                        )
                except Exception as exc:  # noqa: BLE001 - 다른 키워드도 시도하고 실패를 보고한다
                    result.errors.append(f"검색 {keyword}: {exc}")
                    logger.exception("[%s] 검색 실패: %s", platform, keyword)
        for platform, product_ids in plan.products.items():
            result = results[platform]
            if result.skipped:
                continue
            for product_id in product_ids:
                logger.info("[%s] 상품 예약: %s", platform, product_id)
                if dry_run:
                    continue
                try:
                    result.created += await self._seed_product(platform, product_id)
                except Exception as exc:  # noqa: BLE001 - 다른 상품 예약은 계속한다
                    result.errors.append(f"상품 {product_id}: {exc}")
                    logger.exception("[%s] 상품 예약 실패: %s", platform, product_id)
            if platform in plan.expand and not dry_run:
                await self._expand(platform, product_ids, plan.expand[platform], result)
        return results

    async def _expand(
        self, platform: str, product_ids: tuple[str, ...], limit: int, result: SeedResult
    ) -> None:
        """지정 상품에서 같은 판매처의 상품을 더 찾아 예약한다.

        네이버처럼 검색 수단이 없는 플랫폼은 상품 하나를 알아야 다른 상품을 알 수 있다.
        """
        for product_id in product_ids:
            logger.info("[%s] 관련 상품 찾기: %s (최대 %d개)", platform, product_id, limit)
            try:
                async with self.registry[platform](settings=self.settings) as collector:
                    related = await collector.related_products(product_id, limit=limit)
                for related_id in related[:limit]:
                    result.created += await self._seed_product(platform, related_id)
            except Exception as exc:  # noqa: BLE001 - 다른 상품의 확장은 계속한다
                result.errors.append(f"관련 상품 {product_id}: {exc}")
                logger.exception("[%s] 관련 상품 찾기 실패: %s", platform, product_id)

    async def _seed_keyword(self, collector: BaseCollector, keyword: str, limit: int) -> int:
        products = await collector.search_products(keyword, limit=limit)
        # 네트워크 요청 동안 DB 트랜잭션을 열어 두지 않는다. 키워드마다 실패를 격리한다.
        async with self.session_factory() as session:
            repo = ProductRepository(session)
            saved = set()
            for product in products[:limit]:
                if product.platform != collector.platform:
                    raise ValueError("검색 결과의 플랫폼이 collector 와 다릅니다.")
                # 검색 결과는 상세 수집보다 정보가 적다. 이미 있는 상품을 덮어쓰면 상세
                # 정보가 지워지고 수집 시각만 새로워져 재수집이 밀린다. 새 상품만 넣는다.
                if await repo.insert_search_product(product):
                    saved.add(product.product_id)
            await session.commit()
        return len(saved)

    async def _seed_product(self, platform: str, product_id: str) -> bool:
        async with self.session_factory() as session:
            # 이미 수집된 상품은 스케줄러가 갱신한다. 여기서 또 예약하면 시드를 다시
            # 실행할 때마다 전체를 재수집하게 된다.
            if await ProductRepository(session).get(platform, product_id) is not None:
                return False
            _, created = await CollectionJobRepository(session).create_or_get_active(
                platform, product_id, requested_by="seed"
            )
            await session.commit()
        return created
