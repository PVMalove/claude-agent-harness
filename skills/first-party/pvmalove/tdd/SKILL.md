---
name: tdd
description: Use test-driven development to build behavior or fix bugs test-first, at agreed public seams, with independent expectations, deterministic boundaries, and a red-green-refactor loop.
---

# Test-driven development

Use this skill when building a feature or fixing a bug test-first. Work in small vertical
slices: define one observable behavior, make one test red, implement the minimum that makes it
green, and refactor after Green without changing the behavior. The skill applies to unit,
integration, and end-to-end tests; choose the seam and test environment that make the behavior
real.

Before writing a test, read `CONTEXT.md` when it exists and respect the ADRs for the area. Agree
the public seam with the developer or user. In an orchestration dispatch, use the supplied
Context Package and its approved seam instead of starting a new discovery process.

## One behavior at an observable seam

A seam is the public boundary at which a caller can observe behavior. Test at the highest useful
agreed seam. Use the real internal components and value objects that participate in that behavior;
do not reach into private state, private methods, or implementation-only side channels.

One test proves one behavior, not one assertion. A retry, transaction, workflow, or state
transition may need several actions and several assertions. Keep those steps in one test when
they describe one behavior. Separate blank lines between Arrange, Act, and Assert sections and
between meaningful steps so the behavior is easy to read.

Name tests with a behavior phrase that says what the caller can observe, not how the code is
implemented. Name the system under test `sut` when the local convention does not provide a clearer
name. Keep the test independent: it must not depend on another test's order, mutable global state,
or data left in a shared environment.

## Red → Green → Refactor

Choose one next test from the behavior still missing. Do not write a batch of speculative tests
before learning from the current implementation.

1. **Red:** write the smallest test at the agreed seam. Derive its expectation independently and
   run the focused test so it fails for the intended missing behavior.
2. **Green:** make the smallest production change that satisfies the test. Do not add speculative
   behavior, remove assertions, skip the test, or weaken the expected result to obtain Green.
3. **Refactor:** after Green, improve the production or test design without changing behavior.
   Rerun the focused test and the relevant project checks after the refactor.

For a bug fix, begin with a minimal reproducer at the seam where the bug is observable. Keep the
expectation that describes the bug. Weakening it for Green only hides the regression and leaves no
evidence that the fix is correct.

## Independent expectations

Expected values must come from a source independent of the implementation under test:

- Prefer short, known-good literals in examples.
- Use reference fixtures, documented examples, properties, or an independently sourced oracle
  when a literal is not practical.
- Use parameterization for input variants. Do not choose the expected result with an `if`, `match`,
  conditional expression, or lookup that repeats production branching inside the test.
- Do not repeat the production algorithm in an assertion or recompute the expected value by the
  same steps as the implementation.

Multiple assertions are valid when they describe different observable parts of the same behavior.
They are not a reason to split a necessary multi-step scenario into tests that no longer prove the
behavior together.

## Collaborators and unmanaged boundaries

Use real internal collaborators. Do not mock your own modules, internal classes, value objects, or
concrete classes merely to make a test convenient.

At an external unmanaged boundary, **Distinguish commands from queries**:

- A command asks the boundary to cause an effect. Test the sending contract and its observable
  effect, such as the message, request, or persisted result that the boundary receives.
- A query supplies information. Stub the response needed by the scenario. Usually do not assert
  internal call counts, call order, or the number of queries.

Do not turn a query stub into a copy of production logic. Do not assert a private collaborator
call when the public behavior is the contract.

## Time, randomness, and determinism

Deterministic substitutes for time and randomness are permitted. Give domain logic explicit time
and boundary code an injectable clock or random source. Inject those substitutes at the boundary;
do not extend a public production API solely to make a test possible. Tests must not depend on the
wall clock, ambient timezone, unseeded randomness, network timing, or execution order.

This skill does not require a project to add flaky-test policies, seed policies, property-based
testing, or mutation-testing infrastructure. Add such tooling only when the project independently
requires it.

## Database and integration isolation

Test SQL, transactions, indexes, and constraints against the production database engine in an
isolated test environment. Do not replace that engine with a different in-memory engine: dialect,
transaction, locking, and constraint behavior are part of the contract. Keep the isolated
database disposable and independent for each test or test group according to the project's safe
fixture policy.

Test pure domain logic without a database. A domain test should not need persistence merely because
the production adapter uses it.

## Choosing cases

Use the following order as a heuristic, not a coverage mandate:

1. no-op or empty input;
2. the typical successful case;
3. meaningful boundaries and failures;
4. invariants that must hold across the behavior.

Choose the cases that explain the contract and protect valuable behavior. Coverage is a signal,
not the goal of this skill.

## Examples

The examples use Python-like notation, but the rules apply to any test framework.

### Parameterized literals and one behavior

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

### A multi-step behavior with multiple assertions

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

### Query response, command effect, and a fixed clock

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

## Review checklist

Before handing off a slice, check the following:

- The test reaches an agreed public seam and observes behavior through that seam.
- Real internal components and value objects are used; private state and concrete internal mocks
  are absent.
- The test name uses a behavior phrase and the `sut` name is clear.
- Arrange, Act, and Assert are separated by blank lines. One behavior may have multiple steps and
  assertions.
- The expectation is independent of production. Variants use parameterization, not conditional
  expected-result selection.
- External commands are checked for observable effects, while external query responses are stubbed
  without incidental call-count assertions.
- Time and randomness are deterministic, and any substitute enters at a boundary.
- Database behavior uses the production engine in an isolated environment; pure domain tests have
  no database dependency.
- The current test was red before the minimal implementation, Green was reached, and Refactor
  followed Green without changing behavior.
- The next test is explicit. No test was added only to raise coverage, and no mandatory flaky,
  seed, property-based, or mutation policy was invented.
