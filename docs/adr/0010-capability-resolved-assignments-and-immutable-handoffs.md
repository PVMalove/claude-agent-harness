# Capability-resolved assignments и immutable handoff

## Контекст системы

Роли должны сохранять переносимые требования, а runtime-specific provider choice не должен
неявно расширять scope или заменять commit и verification evidence.

## Действующий контракт

Assignment разрешается от role manifest через project mapping к допустимому one-run override.
Каждый provider profile объявляется проектом, содержит fallback и known limitations и обязан
удовлетворять required capabilities и ограничениям роли. Assignment plan роли обязательно задаёт
model и effort для dispatch.

Каждый dispatched batch получает immutable brief и возвращает один completion report. Новый факт,
изменение scope, зоны, DoD, assignment или proof оформляется отдельным решением coordinator и
новым dispatch. Write-role report содержит commit SHA и verification evidence; read-only role
фиксирует соответствующее read-only evidence.

## Операционные последствия

Агент не выбирает несовместимую модель, не меняет исходный scope через чат и не заменяет evidence
текстовым сообщением о завершении. Независимая работа может выполняться параллельно в непересекающихся
зонах, а тяжёлые integration и quality gates остаются сериализованными.
