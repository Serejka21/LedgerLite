# LedgerLite

Два сервіси: **wallet-service** (рахунки користувачів у USD і кредитах) та **provider-mock** (імітація зовнішнього платіжного провайдера з підписаними вебхуками).

## Швидкий старт

```bash
cp .env.example .env          # необов'язково: без .env compose бере dev-плейсхолдери
docker compose up --build     # postgres + provider-mock (:8001) + wallet (:8000)
```

Міграції застосовуються автоматично при старті wallet. Створити користувачів (API-ключ показується один раз, у БД зберігається лише SHA-256):

```bash
docker compose exec wallet python -m app.cli seed                       # alice і bob
docker compose exec wallet python -m app.cli create-account --name carol
```

### Сценарій від початку до кінця

```bash
KEY=ll_...        # api_key alice з виводу seed
BOB=acc_...       # account_id bob
PKEY=dev-provider-key   # PROVIDER_API_KEY

# 1. депозит -> інвойс у провайдера
curl -s -XPOST localhost:8000/v1/deposits -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d '{"amount":"10.00"}'
# -> {"deposit_id":"dep_...","status":"pending","invoice_id":"inv_...","pay_url":"..."}

# 2. "оплата" у провайдері; duplicate=true шле той самий вебхук двічі
curl -s -XPOST localhost:8001/invoices/inv_.../simulate -H "Authorization: Bearer $PKEY" \
     -H 'Content-Type: application/json' -d '{"status":"paid","duplicate":true}'
# webhook_deliveries: outcome "credited", потім "duplicate"

curl -s localhost:8000/v1/accounts/me -H "X-API-Key: $KEY"        # balance_usd "9.85"

# 3. переказ (Idempotency-Key обов'язковий)
curl -s -XPOST localhost:8000/v1/transfers -H "X-API-Key: $KEY" -H "Idempotency-Key: t-1" \
     -H 'Content-Type: application/json' -d "{\"to_account_id\":\"$BOB\",\"amount\":\"5.00\"}"

# 4. покупка кредитів та історія
curl -s -XPOST localhost:8000/v1/purchases -H "X-API-Key: $KEY" \
     -H 'Content-Type: application/json' -d '{"credits":33}'
curl -s "localhost:8000/v1/transactions?limit=20" -H "X-API-Key: $KEY"

# 5. звірка депозитів без вебхука (simulate з "deliver_webhook": false)
docker compose exec wallet python -m app.cli reconcile --older-than-minutes 15
```

OpenAPI: http://localhost:8000/docs і http://localhost:8001/docs.

## Тести та лінтер

Тести wallet-service ганяються на справжньому PostgreSQL: блокування рядків, `ON CONFLICT` і `CHECK` — суть задачі, на SQLite вони нічого б не довели.

```bash
pip install -r wallet_service/requirements-dev.txt -r provider_mock/requirements.txt

# будь-який порожній postgres; базу тести мігрують самі (alembic downgrade base -> upgrade head)
docker run -d --name ll-test-db -p 5433:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=wallet_test postgres:16-alpine
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/wallet_test

(cd wallet_service && python -m pytest -v)
(cd provider_mock && python -m pytest -v)
ruff check . && ruff format --check .
```

Що покрито (95 тестів): таблиці комісії та цін (+ межі знижок і половинне округлення), строгий парсинг сум (число / 3 знаки → 422), підпис вебхука (правильний, неправильний, прострочений, з майбутнього, підмінене тіло), повторний і паралельно-повторний вебхук, `expired`, вебхук після закриття депозиту, недоступність / таймаут провайдера, ідемпотентність переказів (той самий ключ і тіло, інше тіло → 409, ключі в межах акаунта, ключ не «згорає» після помилки), недостатньо коштів, 25 паралельних переказів не дають мінуса, A→B / B→A без дедлоку, пагінація історії, звірка, rate limit.

CI (`.github/workflows/ci.yml`): ruff → pytest з postgres-сервісом → збірка образів і `docker compose up --wait` зі smoke-перевіркою `/health`.

## Схема

```
           X-API-Key                         Authorization: Bearer
 client ───────────────►  wallet-service  ─────────────────────────►  provider-mock
                          (FastAPI, :8000)   POST /invoices            (FastAPI, :8001,
                               │   ▲         GET  /invoices/{id}        пам'ять)
                               │   │         таймаути 2 с / 5 с              │
                               ▼   │                                         │
                          PostgreSQL   ◄──── POST /v1/webhooks/provider ─────┘
                                             X-Signature: sha256=HMAC(secret, "{ts}.{body}")
                                             X-Timestamp
```

Таблиці: `accounts` (баланси, `CHECK balance >= 0`), `deposits`, `webhook_events` (PK = `event_id`), `transfers`, `purchases`, `ledger_entries` (журнал змін балансу, з якого віддається історія), `idempotency_keys`.

## Ключові рішення і компроміси

1. **FastAPI + SQLAlchemy 2 (async) + asyncpg + Alembic.** Pydantic дає валідацію «сума тільки рядком, ≤ 2 знаки» одним анотованим типом (`MoneyStr`: рядок на вході, `Decimal` усередині, рядок на виході), а async природно вкладається у виклики провайдера. У БД гроші — `NUMERIC(18,2)`, у коді — лише `Decimal`; `float` ніде не з'являється.
2. **Баланс захищений двічі.** Перевірка під блокуванням рядка (`SELECT … FOR NO KEY UPDATE`, рахунки завжди блокуються у відсортованому порядку — без дедлоку A→B / B→A) плюс `CHECK (balance_usd >= 0)` у схемі як остання лінія захисту. Саме `NO KEY UPDATE`, а не `FOR UPDATE`: вставки в таблиці з FK на `accounts` беруть `KEY SHARE`, який конфліктує з `FOR UPDATE` — паралельні перекази одного користувача ловили дедлок (знайдено тестом).
3. **Ідемпотентність у тій самій транзакції, що й рух грошей.** `INSERT … ON CONFLICT DO NOTHING` у `idempotency_keys`, далі переказ, далі збереження відповіді — і один `COMMIT`. Паралельний запит з тим самим ключем чекає на першу транзакцію і отримує збережену відповідь. Відбиток — хеш *валідованого* тіла, тож `"5.0"` і `"5.00"` — той самий запит. Свідомий вибір: якщо операція завершилась помилкою (наприклад, `insufficient_funds`), ключ не зберігається, і клієнт може повторити з тим самим ключем після поповнення. Відповідь має заголовок `Idempotent-Replayed`.
4. **Вебхук.** HMAC рахується по сирих байтах тіла (без повторної серіалізації) і порівнюється через `compare_digest`; вікно ±5 хв в обидва боки. Дедуплікація — `event_id` як первинний ключ у тій самій транзакції, що й зарахування, тож навіть два одночасні дублікати зараховуються рівно раз. Рядок депозиту блокується; змінити можна лише `pending`, тому пізні й «чужі» вебхуки ігноруються. На дублікати, пізні події та невідомий `invoice_id` повертається 200, щоб провайдер не ретраїв, а результат записується в `webhook_events.outcome`.
5. **Депозит зберігається до виклику провайдера**, а мережевий виклик іде поза транзакцією БД (жодних блокувань під час I/O). Помилки провайдера мапляться в зрозумілі коди: недоступний або 5xx → 503 `provider_unavailable`, таймаут → 504 `provider_timeout`, 4xx або биті дані → 502 `provider_error`; депозит отримує статус `failed` з причиною.
6. **Журнал (`ledger_entries`)** пишеться в кожній операції разом із балансом «після». Історія віддається з keyset-пагінацією (`cursor` = id останнього запису): вона стабільна, поки додаються нові операції, на відміну від OFFSET.
7. **Бонуси.** Звірка — CLI `reconcile`, що проганяє стан інвойсу через ту саму функцію, що й вебхук. Rate limit — sliding window у пам'яті процесу (30 POST/хв на ключ, 429 + `Retry-After`, вебхук не лімітується).

## Що спрощено або не зроблено

- **Rate limit і provider-mock у пам'яті одного процесу.** Тому uvicorn працює з одним воркером. Для кількох реплік лічильники треба перенести в Redis.
- **Таймаут при створенні інвойсу.** Інвойс міг створитися у провайдера, а депозит уже позначено `failed`. Якщо такий інвойс оплатять, вебхук прийде з невідомим для нас `invoice_id`: подія збережеться як `unknown_invoice`, але гроші автоматично не зарахуються. Правильне рішення — статус `creating` і пошук інвойсу за `external_id` при звірці; у mock-провайдера такого API немає.
- **Звірка запускається вручну** (CLI), а не за розкладом. У проді це cron / k8s CronJob або фонова задача.
- **`paid_amount` береться як є.** Якщо провайдер повідомить `paid` із сумою, більшою за інвойс, зарахуємо фактично сплачене (за ТЗ «фактично сплачену суму»). Окремої обробки переплат немає.
- **`idempotency_keys` і `webhook_events` не чистяться** (немає TTL-джоби).
- **Користувачі створюються лише через CLI.** Немає endpoint-а реєстрації, ротації та відкликання ключів.
- **provider-mock не ретраїть** вебхуки, на які wallet відповів помилкою. Цей випадок закриває звірка.
