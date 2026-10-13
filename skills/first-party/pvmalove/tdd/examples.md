# TDD examples

The examples use Python-like notation, but the rules apply to any test framework.

## Parameterized literals and one behavior

```python
@pytest.mark.parametrize(
    ("items", "expected_total"),
    [
        ([LineItem("tea", Money("10.00")), LineItem("cake", Money("5.00"))], Money("15.00")),
        ([], Money("0.00")),
    ],
)
def test_sut_reports_the_cart_total(items, expected_total):
    sut = CartPricing()

    result = sut.total(items)

    assert result == expected_total
```

The expected totals are literals from the example contract. The test does not reimplement the
production summation algorithm. Parameterization expresses variants without selecting an
expectation conditionally inside the test.

## A multi-step behavior with multiple assertions

```python
def test_sut_retries_once_and_confirms_the_order():
    gateway = StubPaymentGateway(failures_before_success=1)
    sut = Checkout(gateway=gateway, max_retries=1)

    result = sut.checkout(order)

    assert result.status == "confirmed"
    assert result.attempts == 2
```

The retry and confirmation are one behavior, so both assertions stay together. `gateway` is a
deterministic substitute at an external boundary; internal checkout components remain real.

## Query response, command effect, and a fixed clock

```python
def test_sut_publishes_an_expiring_order_from_the_catalog():
    catalog = StubCatalog(query_result=Product("tea", Money("10.00")))
    publisher = RecordingPublisher()
    clock = FixedClock("2026-01-15T10:00:00Z")
    sut = PlaceOrder(catalog=catalog, publisher=publisher, clock=clock)

    result = sut.place("tea", quantity=2)

    assert result.expires_at == "2026-01-15T10:30:00Z"
    assert publisher.messages == [OrderPlaced("tea", quantity=2, total=Money("20.00"))]
```

The catalog query is stubbed with a response. The publisher command is checked through the message
it receives, not through an internal call count. `Product`, `Money`, and `OrderPlaced` remain real
value objects. The fixed clock makes time deterministic without exposing private state.
