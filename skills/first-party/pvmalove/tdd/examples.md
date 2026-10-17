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
def test_sut_confirms_the_order_after_one_transient_payment_failure():
    gateway = RecordingPaymentGateway(failures_before_success=1)
    sut = Checkout(gateway=gateway, max_retries=1)

    result = sut.checkout(Order("order-7", total=Money("15.00")))

    assert result.status == "confirmed"
    assert gateway.accepted_charges == [Charge("order-7", Money("15.00"))]
```

The retry and confirmation are one behavior, so both assertions stay together. A charge is a
command, so the test checks the sending contract: the gateway accepted exactly one charge with the
expected order and amount. The gateway is a deterministic substitute at an external boundary;
internal checkout components remain real.

## A reference fixture as the expectation source

```python
VAT_REFERENCE = load_reference_rows("fixtures/vat-2026-published-table.csv")


@pytest.mark.parametrize("row", VAT_REFERENCE, ids=lambda row: row.case)
def test_sut_matches_the_published_vat_reference(row):
    sut = VatCalculator()

    result = sut.vat_for(row.net_amount, row.category)

    assert result == row.expected_vat
```

The expected values come from a fixture copied from the published tax table, and the fixture
records that origin. The test reads the expectation; it does not recompute VAT.

## An independent oracle

```python
@pytest.mark.parametrize("values", [[3, 1, 2], [], [5, 5, 1], list(range(50, 0, -1))])
def test_sut_orders_values_like_the_reference_sort(values):
    sut = MergeSorter()

    result = sut.sort(values)

    assert result == sorted(values)
```

`sorted` is a trusted oracle with an implementation independent of `MergeSorter`, so the assertion
does not repeat the production algorithm.

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

## A deterministic random source

```python
def test_sut_draws_the_winner_chosen_by_the_random_source():
    entries = [Entry("ann"), Entry("bob"), Entry("eve")]
    sut = Raffle(random_source=FixedChoice(index=1))

    result = sut.draw(entries)

    assert result == Entry("bob")
```

The random source is a boundary dependency injected through the constructor. The fixed choice
makes the expected winner a literal, so the test neither depends on unseeded randomness nor
reimplements the selection.
