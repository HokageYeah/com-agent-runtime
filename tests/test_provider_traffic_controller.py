from __future__ import annotations

from collections import defaultdict

import pytest

from app.runtime.model_gateway import ModelRoute, ProviderTrafficController


class FakeRedis:
    """只模拟本控制器使用的 Lua 命令，避免测试依赖外部 Redis。"""

    def __init__(self) -> None:
        self.operations: list[str] = []
        self.permits: dict[str, dict[str, str]] = {}
        self.active: dict[str, set[str]] = defaultdict(set)
        self.blocked_until: dict[str, float] = {}
        self.rpm: dict[str, list[float]] = defaultdict(list)
        self.tpm: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self.circuit_failures: dict[str, int] = defaultdict(int)
        self.circuit_open_until: dict[str, float] = {}

    def eval(self, script: str, numkeys: int, *args: object) -> list[object]:
        operation = str(args[0])
        self.operations.append(operation)
        route_key, permit_key, blocked_key = map(str, args[1:4])
        now = float(args[4])
        if operation == "circuit_preflight":
            threshold, route_id = int(args[5]), str(args[6])
            if threshold > 0 and self.circuit_open_until.get(route_id, 0) > now:
                return ["circuit_open", self.circuit_open_until[route_id]]
            return ["circuit_available", 0]
        if operation == "acquire":
            limit, ttl, rpm_limit, tpm_limit, estimated_tokens = map(
                int, args[5:10]
            )
            route_id = str(args[10])
            circuit_threshold = int(args[11])
            if circuit_threshold > 0 and self.circuit_open_until.get(route_id, 0) > now:
                return ["circuit_open", self.circuit_open_until[route_id]]
            permit = self.permits.get(permit_key)
            if permit is not None:
                if permit["route_id"] != route_id:
                    return ["route_mismatch", 0]
                return [permit["state"], 0]
            if self.blocked_until.get(blocked_key, 0) > now:
                return ["blocked", self.blocked_until[blocked_key]]
            if len(self.active[route_key]) >= limit:
                return ["concurrency_exceeded", 0]
            self.rpm[route_key] = [at for at in self.rpm[route_key] if at > now - 60]
            self.tpm[route_key] = [item for item in self.tpm[route_key] if item[0] > now - 60]
            if len(self.rpm[route_key]) >= rpm_limit:
                return ["rpm_exceeded", 0]
            if sum(tokens for _, tokens in self.tpm[route_key]) + estimated_tokens > tpm_limit:
                return ["tpm_exceeded", 0]
            self.active[route_key].add(permit_key)
            self.permits[permit_key] = {
                "state": "acquired", "route": route_key, "route_id": route_id
            }
            self.rpm[route_key].append(now)
            self.tpm[route_key].append((now, estimated_tokens))
            return ["acquired", ttl]
        if operation == "circuit_failure":
            threshold, open_seconds, route_id = int(args[5]), float(args[6]), str(args[7])
            if threshold == 0:
                return ["circuit_disabled", 0]
            self.circuit_failures[route_id] += 1
            if self.circuit_failures[route_id] >= threshold:
                self.circuit_open_until[route_id] = now + open_seconds
                return ["circuit_opened", self.circuit_open_until[route_id]]
            return ["circuit_failure", self.circuit_failures[route_id]]
        if operation == "circuit_success":
            route_id = str(args[5])
            self.circuit_failures.pop(route_id, None)
            self.circuit_open_until.pop(route_id, None)
            return ["circuit_reset", 0]
        if operation == "started":
            permit = self.permits.get(permit_key)
            if permit is None:
                return ["expired", 0]
            if permit["route_id"] != str(args[5]):
                return ["route_mismatch", 0]
            if permit["state"] == "acquired":
                permit["state"] = "started"
                return ["started", 0]
            if permit["state"] == "started":
                return ["already_started", 0]
            return [permit["state"], 0]
        if operation == "settle":
            permit = self.permits.get(permit_key)
            if permit is None:
                return ["already_settled", 0]
            if permit["route_id"] != str(args[6]):
                return ["route_mismatch", 0]
            if permit["state"] == "settled":
                return ["already_settled", 0]
            permit["state"] = "settled"
            self.active[route_key].discard(permit_key)
            retry_after = float(args[5])
            if retry_after > 0:
                self.blocked_until[blocked_key] = max(
                    self.blocked_until.get(blocked_key, 0), now + retry_after
                )
            return ["settled", 0]
        raise AssertionError(f"未知操作: {operation}")


class BrokenRedis:
    def eval(self, *args: object) -> object:
        raise ConnectionError("redis unavailable")


@pytest.fixture
def route() -> ModelRoute:
    return ModelRoute(
        route_id="summarize",
        provider="example",
        model="example-small",
        endpoint="https://provider.example/v1/chat",
        rate_limit_key="example:small",
        max_concurrency=1,
        rpm_limit=30,
        tpm_limit=10_000,
        timeout_seconds=10,
        permit_ttl_seconds=20,
        settle_margin_seconds=5,
        price_unit="usd_per_1k_tokens",
        input_price=0.001,
        output_price=0.002,
    )


def test_redis_failure_fails_closed(route: ModelRoute) -> None:
    result = ProviderTrafficController(BrokenRedis()).acquire(route, "permit-a")

    assert result.status == "redis_unavailable"
    assert not result.granted


@pytest.mark.parametrize(
    ("threshold", "open_seconds"),
    [(True, 0), (-1, 0), (1, True), (1, 0), (1, float("inf")), (0, 1)],
)
def test_route_validates_circuit_configuration(
    route: ModelRoute, threshold: object, open_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="circuit"):
        ModelRoute(**{
            **route.__dict__, "circuit_failure_threshold": threshold,
            "circuit_open_seconds": open_seconds,
        })


def test_circuit_opens_after_consecutive_failures_and_rejects_before_permit(route: ModelRoute) -> None:
    redis = FakeRedis()
    def clock() -> float:
        return 100.0

    guarded = ModelRoute(**{**route.__dict__, "circuit_failure_threshold": 2, "circuit_open_seconds": 30})
    controller = ProviderTrafficController(redis, clock=clock)

    assert controller.record_circuit_failure(guarded).status == "circuit_failure"
    assert controller.record_circuit_failure(guarded).status == "circuit_opened"
    rejected = controller.acquire(guarded, "permit-circuit")

    assert rejected.status == "circuit_open"
    assert rejected.retry_after_seconds == 30
    assert "model_gateway:permit:permit-circuit" not in redis.permits


def test_circuit_success_resets_and_429_does_not_count_as_failure(route: ModelRoute) -> None:
    redis = FakeRedis()
    guarded = ModelRoute(**{**route.__dict__, "circuit_failure_threshold": 2, "circuit_open_seconds": 30})
    controller = ProviderTrafficController(redis, clock=lambda: 100.0)

    assert controller.record_circuit_failure(guarded).status == "circuit_failure"
    assert controller.record_circuit_success(guarded).status == "circuit_reset"
    assert controller.settle(guarded, "missing", retry_after_seconds=20).status == "already_settled"
    assert controller.record_circuit_failure(guarded).status == "circuit_failure"
    assert redis.circuit_failures[guarded.route_id] == 1


def test_circuit_is_route_isolated_can_be_disabled_and_redis_failure_is_closed(route: ModelRoute) -> None:
    redis = FakeRedis()
    guarded = ModelRoute(**{**route.__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30})
    other = ModelRoute(**{**guarded.__dict__, "route_id": "other"})
    disabled = ModelRoute(**{**route.__dict__, "circuit_failure_threshold": 0, "circuit_open_seconds": 0})
    controller = ProviderTrafficController(redis, clock=lambda: 100.0)

    assert controller.record_circuit_failure(guarded).status == "circuit_opened"
    assert controller.acquire(other, "permit-other").granted
    assert controller.record_circuit_failure(disabled).status == "circuit_disabled"
    assert ProviderTrafficController(BrokenRedis()).record_circuit_failure(guarded).status == "redis_unavailable"


def test_disabled_circuit_does_not_mutate_existing_circuit_state(route: ModelRoute) -> None:
    redis = FakeRedis()
    redis.circuit_failures[route.route_id] = 7
    redis.circuit_open_until[route.route_id] = 130.0
    disabled = ModelRoute(**{**route.__dict__, "circuit_failure_threshold": 0, "circuit_open_seconds": 0})
    controller = ProviderTrafficController(redis, clock=lambda: 100.0)

    assert controller.record_circuit_failure(disabled).status == "circuit_disabled"
    assert controller.record_circuit_success(disabled).status == "circuit_disabled"

    assert redis.circuit_failures == {route.route_id: 7}
    assert redis.circuit_open_until == {route.route_id: 130.0}
    assert redis.operations == []


def test_shared_concurrency_limit_rejects_second_permit(route: ModelRoute) -> None:
    controller = ProviderTrafficController(FakeRedis())

    first = controller.acquire(route, "permit-a")
    second = controller.acquire(route, "permit-b")

    assert first.granted
    assert second.status == "concurrency_exceeded"


def test_shared_rpm_and_tpm_limits_reject_excess(route: ModelRoute) -> None:
    route = ModelRoute(**{**route.__dict__, "max_concurrency": 3, "rpm_limit": 1, "tpm_limit": 10})
    controller = ProviderTrafficController(FakeRedis())

    assert controller.acquire(route, "permit-a", estimated_tokens=6).granted
    assert controller.acquire(route, "permit-b", estimated_tokens=1).status == "rpm_exceeded"

    token_route = ModelRoute(**{**route.__dict__, "rpm_limit": 3})
    controller = ProviderTrafficController(FakeRedis())
    assert controller.acquire(token_route, "permit-c", estimated_tokens=6).granted
    assert controller.acquire(token_route, "permit-d", estimated_tokens=5).status == "tpm_exceeded"


def test_settle_is_idempotent_and_releases_once(route: ModelRoute) -> None:
    controller = ProviderTrafficController(FakeRedis())
    assert controller.acquire(route, "permit-a").granted
    assert controller.mark_started(route, "permit-a").status == "started"

    assert controller.settle(route, "permit-a").status == "settled"
    assert controller.settle(route, "permit-a").status == "already_settled"
    assert controller.acquire(route, "permit-b").granted


def test_mark_started_grants_send_right_only_once_across_controllers(route: ModelRoute) -> None:
    redis = FakeRedis()
    controller_a = ProviderTrafficController(redis)
    controller_b = ProviderTrafficController(redis)
    assert controller_a.acquire(route, "permit-a").granted

    assert controller_a.mark_started(route, "permit-a").status == "started"
    assert controller_b.mark_started(route, "permit-a").status == "already_started"


def test_replayed_acquire_does_not_reset_started_permit(route: ModelRoute) -> None:
    controller = ProviderTrafficController(FakeRedis())
    assert controller.acquire(route, "permit-a").granted
    assert controller.mark_started(route, "permit-a").status == "started"

    assert controller.acquire(route, "permit-a").status == "started"


def test_permit_cannot_be_started_or_settled_on_another_route(route: ModelRoute) -> None:
    other_route = ModelRoute(**{**route.__dict__, "route_id": "other"})
    controller = ProviderTrafficController(FakeRedis())
    assert controller.acquire(route, "permit-a").granted

    assert controller.acquire(other_route, "permit-a").status == "route_mismatch"
    assert controller.mark_started(other_route, "permit-a").status == "route_mismatch"
    assert controller.settle(other_route, "permit-a").status == "route_mismatch"
    assert controller.mark_started(route, "permit-a").status == "started"
    assert controller.settle(route, "permit-a").status == "settled"
    assert controller.settle(other_route, "permit-a").status == "route_mismatch"


def test_retry_after_blocks_other_workers(route: ModelRoute) -> None:
    redis = FakeRedis()
    controller_a = ProviderTrafficController(redis)
    controller_b = ProviderTrafficController(redis)
    assert controller_a.acquire(route, "permit-a").granted

    assert controller_a.settle(route, "permit-a", retry_after_seconds=30).status == "settled"
    blocked = controller_b.acquire(route, "permit-b")

    assert blocked.status == "blocked"
    assert blocked.retry_after_seconds > 0


def test_shorter_retry_after_never_shortens_existing_shared_cooldown(route: ModelRoute) -> None:
    redis = FakeRedis()
    route = ModelRoute(**{**route.__dict__, "max_concurrency": 2})
    controller_a = ProviderTrafficController(redis, clock=lambda: 100.0)
    controller_b = ProviderTrafficController(redis, clock=lambda: 110.0)
    assert controller_a.acquire(route, "permit-a").granted
    assert controller_b.acquire(route, "permit-b").granted
    assert controller_a.settle(route, "permit-a", retry_after_seconds=60).status == "settled"
    assert controller_b.settle(route, "permit-b", retry_after_seconds=1).status == "settled"

    blocked = controller_b.acquire(route, "permit-c")
    assert blocked.status == "blocked"
    assert blocked.retry_after_seconds == 50.0


def test_route_rejects_invalid_ttl_price_unit_and_endpoint() -> None:
    with pytest.raises(ValueError, match="permit_ttl_seconds"):
        ModelRoute(
            route_id="bad", provider="p", model="m", endpoint="https://p.example", rate_limit_key="p:m",
            max_concurrency=1, rpm_limit=1, tpm_limit=1, timeout_seconds=10,
            permit_ttl_seconds=14, settle_margin_seconds=5, price_unit="usd_per_1k_tokens",
            input_price=0, output_price=0,
        )


def test_settings_only_exposes_validated_server_side_routes() -> None:
    from app.core.config import Settings

    settings = Settings(
        MODEL_ROUTES_JSON='[{"route_id":"summary","provider":"p","model":"m",'
        '"endpoint":"https://provider.example/v1","rate_limit_key":"p:m",'
        '"max_concurrency":1,"rpm_limit":2,"tpm_limit":3,"timeout_seconds":1,'
        '"permit_ttl_seconds":2,"settle_margin_seconds":1,'
        '"price_unit":"usd_per_1k_tokens","input_price":0,"output_price":0}]'
    )

    assert settings.model_routes[0].route_id == "summary"
    with pytest.raises(ValueError, match="endpoint"):
        ModelRoute(
            route_id="bad", provider="p", model="m", endpoint="not-a-url", rate_limit_key="p:m",
            max_concurrency=1, rpm_limit=1, tpm_limit=1, timeout_seconds=10,
            permit_ttl_seconds=15, settle_margin_seconds=5, price_unit="usd_per_1k_tokens",
            input_price=0, output_price=0,
        )
    with pytest.raises(ValueError, match="price_unit"):
        ModelRoute(
            route_id="bad", provider="p", model="m", endpoint="https://p.example", rate_limit_key="p:m",
            max_concurrency=1, rpm_limit=1, tpm_limit=1, timeout_seconds=10,
            permit_ttl_seconds=15, settle_margin_seconds=5, price_unit="free_tokens",
            input_price=0, output_price=0,
        )


def test_settings_rejects_duplicate_route_id_at_startup() -> None:
    from app.core.config import Settings

    route = (
        '{"route_id":"summary","provider":"p","model":"m",'
        '"endpoint":"https://provider.example/v1","rate_limit_key":"p:m",'
        '"max_concurrency":1,"rpm_limit":2,"tpm_limit":3,"timeout_seconds":1,'
        '"permit_ttl_seconds":2,"settle_margin_seconds":1,'
        '"price_unit":"usd_per_1k_tokens","input_price":0,"output_price":0}'
    )
    with pytest.raises(ValueError, match="重复 route_id"):
        Settings(MODEL_ROUTES_JSON=f"[{route},{route}]")
