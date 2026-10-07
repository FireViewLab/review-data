import asyncio
import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from review_data.core.analysis_stream import (
    MAX_FRAME_BYTES,
    AnalysisStreamClient,
    AnalysisStreamError,
)

URL = "https://analysis.example/experimental/analyze?contract=v0.5"
REVIEWS = [{"review_id": "r1", "content": "실제 리뷰", "rating": 5, "written_at": None}]
META = {
    "request_id": "7",
    "ai_job_id": "ai-job-1",
    "platform": "naver",
    "product_id": "p1",
    "review_count": 1,
    "contract_version": "v0.5",
    "model_version": "m1",
    "policy_version": "p1",
}
DONE = {"request_id": "7", "ai_job_id": "ai-job-1", "result_count": 1}
RESULT = {
    "request_id": "7",
    "review_id": "r1",
    "rti": 0,
    "level": None,
    "text_score": -1,
    "behavior_score": None,
    "network_score": 99.5,
    "reasons": [],
}


def event(name, data):
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def complete(result=None, meta=None, done=None):
    return (
        event("meta", META if meta is None else meta)
        + event("result", RESULT if result is None else result)
        + event("done", DONE if done is None else done)
    )


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


@pytest.fixture
def mock_http(monkeypatch):
    real_client = httpx.AsyncClient
    clients, requests, streams = [], [], []

    def setup(
        body=b"",
        *,
        status=200,
        content_type="text/event-stream",
        chunks=None,
        error=None,
        handler_error=None,
        stream=None,
    ):
        stream = stream or ByteStream([body] if chunks is None else chunks, error)
        streams.append(stream)

        def handler(request):
            requests.append(request)
            if handler_error:
                raise handler_error
            return httpx.Response(
                status,
                headers={"content-type": content_type, "location": URL + "&redirect=1"},
                stream=stream,
            )

        def factory(**kwargs):
            client = real_client(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return stream

    setup.clients = clients
    setup.requests = requests
    setup.streams = streams
    return setup


async def analyze(client=None, reviews=None, **kwargs):
    return await (client or AnalysisStreamClient(URL)).analyze(
        "naver",
        "p1",
        REVIEWS if reviews is None else reviews,
        7,
        "hash123",
        **kwargs,
    )


async def test_request_and_normalized_result(mock_http):
    versions = {**META, "model_version": "m1", "policy_version": "p2"}
    stream = mock_http(complete(meta=versions), content_type="text/event-stream; charset=utf-8")
    reviews = [
        {
            **REVIEWS[0],
            "rating": Decimal("4.5"),
            "written_at": datetime(2026, 10, 7, 12, 0),
            "unused": "omit",
        }
    ]
    client = AnalysisStreamClient(
        URL, token="private-token", timeout=17, model_version="m1", policy_version="p2"
    )
    result = await analyze(client, reviews)
    assert result.model_version == "m1"
    assert result.policy_version == "p2"
    assert result.results == [
        {
            k: v
            for k, v in {
                **RESULT,
                "rti": Decimal(0),
                "text_score": None,
                "network_score": Decimal("99.5"),
            }.items()
            if k != "request_id"
        }
    ]
    request = mock_http.requests[0]
    assert str(request.url) == URL
    assert request.method == "POST"
    assert json.loads(request.content) == {
        "platform": "naver",
        "product_id": "p1",
        "reviews": [
            {
                "review_id": "r1",
                "content": "실제 리뷰",
                "rating": 4.5,
                "written_at": "2026-10-07T12:00:00",
            }
        ],
    }
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["x-internal-token"] == "private-token"
    assert "authorization" not in request.headers
    assert request.headers["x-request-id"] == "7"
    assert request.headers["idempotency-key"] == "7"
    assert "x-analysis-job-id" not in request.headers
    assert "x-input-hash" not in request.headers
    assert mock_http.clients[0].timeout.read == 17
    assert mock_http.clients[0].follow_redirects is False
    assert mock_http.clients[0].is_closed and stream.closed


async def test_crlf_multiline_comments_bom_and_single_byte_chunks(mock_http):
    body = (
        b"\xef\xbb\xbf: comment\r\nid: 1\r\nretry: 1000\r\n\r\n"
        + event("meta", META).replace(b"data: {", b"data: {\ndata: ").replace(b"\n", b"\r\n")
        + event("heartbeat", {"request_id": "7"})
        + event("progress", {"request_id": "7", "stage": "analyzing", "processed": 0})
        + event("result", RESULT)
        + event("done", DONE)
        + b"\n\r\n"
    )
    mock_http(chunks=[body[i : i + 1] for i in range(len(body))])
    assert len((await analyze()).results) == 1
    assert "x-internal-token" not in mock_http.requests[0].headers


async def test_order_precision_and_equivalent_duplicate(mock_http):
    first = event("result", RESULT)
    duplicate = event("result", {**RESULT, "text_score": None})
    second = event("result", {**RESULT, "review_id": "r2"}).replace(
        b'"rti": 0', b'"rti": 0.12345678901234567890123456789'
    )
    mock_http(
        event("meta", {**META, "review_count": 2})
        + second
        + first
        + duplicate
        + event("done", {**DONE, "result_count": 2})
    )
    result = await analyze(reviews=[REVIEWS[0], {**REVIEWS[0], "review_id": "r2"}])
    assert [item["review_id"] for item in result.results] == ["r1", "r2"]
    assert result.results[1]["rti"] == Decimal("0.12345678901234567890123456789")


@pytest.mark.parametrize("score", [None, -1, 0, 100, 0.5])
async def test_allowed_score_values(mock_http, score):
    mock_http(complete(result={**RESULT, "rti": score}))
    value = (await analyze()).results[0]["rti"]
    assert value == (None if score is None or score == -1 else Decimal(str(score)))


@pytest.mark.parametrize("field", ["rti", "text_score", "behavior_score", "network_score"])
@pytest.mark.parametrize("value", [True, False, "0", -2, -0.5, 101, float("nan"), float("inf")])
async def test_invalid_scores(mock_http, field, value):
    stream = mock_http(complete(result={**RESULT, field: value}))
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert exc.value.retryable is False
    assert stream.closed and mock_http.clients[0].is_closed


@pytest.mark.parametrize(
    "patch",
    [
        {"level": "unknown"},
        {"level": True},
        {"reasons": None},
        {"reasons": "reason"},
        {"reasons": [1]},
        {"review_id": "outside"},
        {"review_id": None},
    ],
)
async def test_invalid_result_fields(mock_http, patch):
    mock_http(complete(result={**RESULT, **patch}))
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize("field", list(RESULT))
async def test_missing_result_fields(mock_http, field):
    mock_http(complete(result={key: value for key, value in RESULT.items() if key != field}))
    with pytest.raises(AnalysisStreamError):
        await analyze()


@pytest.mark.parametrize("level", [None, "safe", "warn", "danger"])
async def test_levels_and_reasons(mock_http, level):
    mock_http(complete(result={**RESULT, "level": level, "reasons": ["근거", ""]}))
    assert (await analyze()).results[0]["level"] == level


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"request_id": "8"},
        {"request_id": True},
        {"request_id": 7},
        {**META, "model_version": 1},
    ],
)
async def test_invalid_meta(mock_http, meta):
    mock_http(complete(meta=meta))
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize("field", ["model_version", "policy_version"])
@pytest.mark.parametrize("value", [None, "other", "", 1])
async def test_configured_versions_must_match(mock_http, field, value):
    mock_http(complete(meta={**META, field: value}))
    with pytest.raises(AnalysisStreamError):
        await analyze(AnalysisStreamClient(URL, **{field: "expected"}))


async def test_unconfigured_versions_are_preserved(mock_http):
    mock_http(complete(meta={**META, "model_version": "server-model"}))
    result = await analyze()
    assert result.model_version == "server-model" and result.policy_version == "p1"


@pytest.mark.parametrize(
    "done",
    [
        {},
        {**DONE, "request_id": "8"},
        {**DONE, "request_id": True},
        {**DONE, "result_count": 0},
        {**DONE, "result_count": 2},
        {**DONE, "result_count": True},
        {**DONE, "result_count": "1"},
    ],
)
async def test_invalid_done(mock_http, done):
    mock_http(complete(done=done))
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


async def test_missing_result(mock_http):
    mock_http(event("meta", META) + event("done", DONE))
    with pytest.raises(AnalysisStreamError, match="count or results"):
        await analyze()


async def test_conflicting_duplicate(mock_http):
    mock_http(
        event("meta", META)
        + event("result", RESULT)
        + event("result", {**RESULT, "rti": 10})
        + event("done", DONE)
    )
    with pytest.raises(AnalysisStreamError, match="conflicting duplicate"):
        await analyze()


@pytest.mark.parametrize("name", ["result", "done", "progress", "heartbeat", "error", "unknown"])
async def test_meta_is_first_event(mock_http, name):
    mock_http(event(name, {}))
    with pytest.raises(AnalysisStreamError, match="first SSE event"):
        await analyze()


@pytest.mark.parametrize(
    "suffix", [event("meta", META), event("unknown", {}), b"data: {}\n\n", b"event: result\n\n"]
)
async def test_repeated_unknown_or_invalid_event(mock_http, suffix):
    mock_http(event("meta", META) + suffix)
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b'{"a":1,"a":2}', b'"\xff"'])
async def test_invalid_json_or_encoding(mock_http, raw):
    mock_http(event("meta", META) + b"event: result\ndata: " + raw + b"\n\n")
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize("retryable", [False, True, None, "true"])
async def test_error_event(mock_http, retryable):
    mock_http(
        event("meta", META)
        + event(
            "error",
            {
                "request_id": "7",
                "ai_job_id": "ai-job-1",
                "retryable": retryable,
                "message": "private details",
            },
        )
    )
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert exc.value.retryable is (retryable is True)
    assert "private details" not in str(exc.value)


@pytest.mark.parametrize(
    "suffix", [event("progress", {}), event("done", DONE), b": heartbeat\n\n", b"id: 4\n\n"]
)
async def test_frame_after_done_is_rejected(mock_http, suffix):
    mock_http(complete() + suffix)
    with pytest.raises(AnalysisStreamError, match="after done") as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize(
    "body",
    [
        b"",
        event("meta", META),
        event("meta", META) + event("result", RESULT),
        complete() + b"data: unfinished",
        complete().removesuffix(b"\n\n"),
    ],
)
async def test_incomplete_eof(mock_http, body):
    mock_http(body)
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert exc.value.retryable


@pytest.mark.parametrize("prefix", [b"data: ", b": "])
@pytest.mark.parametrize("newline", [False, True])
async def test_oversized_frame_across_chunks(mock_http, prefix, newline):
    body = prefix + b"x" * MAX_FRAME_BYTES + (b"\n\n" if newline else b"")
    stream = mock_http(chunks=[body[:400000], body[400000:800000], body[800000:]])
    with pytest.raises(AnalysisStreamError, match="exceeds 1 MiB") as exc:
        await analyze()
    assert not exc.value.retryable and stream.closed


async def test_exact_frame_limit_and_separate_frame_budgets(mock_http):
    comment = b":" + b"x" * (MAX_FRAME_BYTES - 3) + b"\n\n"
    mock_http(comment + comment + complete())
    assert len((await analyze()).results) == 1


@pytest.mark.parametrize(
    "status,retryable",
    [(429, True), (500, True), (503, True), (401, False), (422, False), (400, False), (302, False)],
)
async def test_http_status_and_no_redirect(mock_http, status, retryable):
    stream = mock_http(status=status)
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert exc.value.retryable is retryable
    assert len(mock_http.requests) == 1
    assert stream.closed and mock_http.clients[0].is_closed


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
async def test_content_type_required(mock_http, content_type):
    mock_http(complete(), content_type=content_type)
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


@pytest.mark.parametrize("phase", ["connect", "stream", "after_done"])
async def test_timeouts_and_resource_cleanup(mock_http, phase):
    error = httpx.ReadTimeout("private timeout details")
    stream = mock_http(
        body=complete() if phase == "after_done" else event("meta", META),
        error=error if phase != "connect" else None,
        handler_error=error if phase == "connect" else None,
    )
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert exc.value.retryable
    assert "private timeout details" not in str(exc.value)
    assert mock_http.clients[0].is_closed
    if phase != "connect":
        assert stream.closed


async def test_cancellation_closes_response(mock_http):
    waiting = asyncio.Event()

    class WaitingStream(ByteStream):
        async def __aiter__(self):
            yield event("meta", META)
            waiting.set()
            await asyncio.Event().wait()

    stream = WaitingStream([])
    mock_http(stream=stream)
    task = asyncio.create_task(analyze())
    await asyncio.wait_for(waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and mock_http.clients[0].is_closed


@pytest.mark.parametrize(
    "reviews",
    [
        [],
        [{}],
        [{**REVIEWS[0], "content": " "}],
        [REVIEWS[0], REVIEWS[0]],
        [{**REVIEWS[0], "rating": True}],
        [{**REVIEWS[0], "rating": float("inf")}],
        [{**REVIEWS[0], "written_at": 1}],
    ],
)
async def test_invalid_input_is_rejected_before_http(mock_http, reviews):
    mock_http(complete())
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze(reviews=reviews)
    assert not exc.value.retryable
    assert mock_http.requests == []


@pytest.mark.parametrize(
    "kwargs",
    [{"timeout": 0}, {"timeout": True}, {"timeout": float("nan")}, {"token": "secret\nvalue"}],
)
def test_invalid_client_configuration(kwargs):
    with pytest.raises(AnalysisStreamError) as exc:
        AnalysisStreamClient(URL, **kwargs)
    assert not exc.value.retryable


@pytest.mark.parametrize(
    "job_id,input_hash", [(True, "hash"), (0, "hash"), (7, ""), (7, "hash\r\nvalue")]
)
async def test_invalid_correlation_headers(mock_http, job_id, input_hash):
    mock_http(complete())
    with pytest.raises(AnalysisStreamError) as exc:
        await AnalysisStreamClient(URL).analyze("naver", "p1", REVIEWS, job_id, input_hash)
    assert not exc.value.retryable and not mock_http.requests


async def test_deep_json_is_wrapped(mock_http):
    raw = b"[" * 2000 + b"0" + b"]" * 2000
    mock_http(event("meta", META) + b"event: result\ndata: " + raw + b"\n\n")
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable


async def test_unsupported_endpoint_is_not_retryable(mock_http):
    mock_http(handler_error=httpx.UnsupportedProtocol("invalid endpoint"))
    with pytest.raises(AnalysisStreamError) as exc:
        await analyze()
    assert not exc.value.retryable
    assert mock_http.clients[0].is_closed


async def test_retries_keep_data_job_identity_and_new_jobs_use_new_keys(mock_http):
    mock_http(complete())
    first = await analyze()
    replayed = await analyze()
    assert first == replayed
    keys = [r.headers["idempotency-key"] for r in mock_http.requests]
    assert keys == ["7", "7"]
    mock_http(
        complete(
            meta={**META, "request_id": "8"},
            done={**DONE, "request_id": "8"},
            result={**RESULT, "request_id": "8"},
        )
    )
    await AnalysisStreamClient(URL).analyze("naver", "p1", REVIEWS, 8, "hash123")
    assert mock_http.requests[-1].headers["idempotency-key"] == "8"
    assert all(
        r.headers["x-request-id"] == r.headers["idempotency-key"] for r in mock_http.requests
    )


@pytest.mark.parametrize(
    "code,retryable",
    [
        ("IDEMPOTENCY_IN_PROGRESS", True),
        ("IDEMPOTENCY_KEY_REUSED", False),
        ("IDEMPOTENCY_FAILED", False),
        ("UNKNOWN", False),
    ],
)
async def test_idempotency_conflict_classification(mock_http, code, retryable):
    stream = mock_http(
        json.dumps({"detail": {"code": code}}).encode(), status=409, content_type="application/json"
    )
    with pytest.raises(AnalysisStreamError) as error:
        await analyze()
    assert error.value.retryable is retryable
    assert stream.closed and code not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b"invalid",
        b"[]",
        b"{}",
        b"x" * 4097,
        b'{"detail":{"code":"IDEMPOTENCY_IN_PROGRESS","code":"UNKNOWN"}}',
    ],
)
async def test_invalid_or_oversized_conflict_body_is_not_retryable(mock_http, body):
    mock_http(body, status=409, content_type="application/json")
    with pytest.raises(AnalysisStreamError) as error:
        await analyze()
    assert not error.value.retryable


@pytest.mark.parametrize("name", ["result", "progress", "heartbeat", "done", "error"])
@pytest.mark.parametrize("request_id", [None, 7, "8", "07"])
async def test_every_event_is_bound_to_data_request(mock_http, name, request_id):
    data = {**(RESULT if name == "result" else DONE), "request_id": request_id, "retryable": True}
    mock_http(event("meta", META) + event(name, data))
    with pytest.raises(AnalysisStreamError) as error:
        await analyze()
    assert not error.value.retryable


@pytest.mark.parametrize(
    "field,value",
    [
        ("platform", "kurly"),
        ("product_id", "other"),
        ("review_count", 2),
        ("review_count", True),
        ("contract_version", "v1"),
        ("ai_job_id", ""),
        ("model_version", None),
        ("policy_version", None),
    ],
)
async def test_meta_validates_input_and_contract(mock_http, field, value):
    mock_http(complete(meta={**META, field: value}))
    with pytest.raises(AnalysisStreamError) as error:
        await analyze()
    assert not error.value.retryable


async def test_internal_ai_job_is_preserved_and_consistent(mock_http):
    mock_http(complete())
    assert (await analyze()).ai_job_id == "ai-job-1"
    mock_http(complete(done={**DONE, "ai_job_id": "different-ai-job"}))
    with pytest.raises(AnalysisStreamError):
        await analyze()
