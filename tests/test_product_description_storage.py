"""상품 소개의 보존·갱신과 상세/목록 응답을 실제 DB에서 검증한다."""

import importlib

import pytest
from fastapi.testclient import TestClient

from review_data.core.db.models import ProductRow
from review_data.core.db.repository import ProductRepository, ReviewRepository
from review_data.core.models import Product, Review
from review_data.core.settings import Settings, get_settings


def product(**values):
    return Product(
        platform="descplat", product_id="p", name="상품", url="https://example.com", **values
    )


async def test_product_description_defaults(session):
    await ProductRepository(session).upsert(product())
    row = await session.get(ProductRow, ("descplat", "p"))
    assert row.description_text is None
    assert row.description_images == []


@pytest.mark.parametrize(
    "update,expected_text,expected_images",
    [
        ({}, "기존 소개", ["https://example.com/old.jpg"]),
        ({"description_text": ""}, "기존 소개", ["https://example.com/old.jpg"]),
        ({"description_text": "새 소개"}, "새 소개", ["https://example.com/old.jpg"]),
        ({"description_images": ["https://example.com/new.jpg"]},
         "기존 소개", ["https://example.com/new.jpg"]),
    ],
)
async def test_product_upsert_preserves_missing_description_fields(
    session, update, expected_text, expected_images
):
    repo = ProductRepository(session)
    await repo.upsert(product(
        description_text="기존 소개", description_images=["https://example.com/old.jpg"]
    ))
    await repo.upsert(product(**update))
    row = await session.get(ProductRow, ("descplat", "p"))
    assert row.description_text == expected_text
    assert row.description_images == expected_images


async def test_product_description_api_detail_only(session_factory, monkeypatch):
    from review_data.api import v1

    module = importlib.import_module("review_data.api.app")
    settings = Settings(_env_file=None, database_url=get_settings().database_url)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(v1, "get_settings", lambda: settings)
    async with session_factory() as session:
        repo = ProductRepository(session)
        await repo.upsert(product(
            description_text="상품 소개", description_images=["https://example.com/detail.jpg"]
        ))
        await ReviewRepository(session).upsert_many("descplat", "p", [
            Review(platform="descplat", product_id="p", review_id="r", content="검증용")
        ])
        await repo.mark_reviews_collected("descplat", "p")
        await session.commit()

    with TestClient(module.app) as client:
        response = client.get("/api/v1/descplat/products/p")
        assert response.status_code == 200
        detail = response.json()["product"]
        assert detail["description_text"] == "상품 소개"
        assert detail["description_images"] == ["https://example.com/detail.jpg"]
        response = client.get("/api/v1/catalog")
        assert response.status_code == 200
        catalog_product = response.json()["items"][0]["product"]
        assert "description_text" not in catalog_product
        assert "description_images" not in catalog_product
