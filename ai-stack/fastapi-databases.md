---
title: Databases with FastAPI and SQLAlchemy 2
section: ai-stack
order: 5
phase: "Phase 1 · FastAPI"
summary: Async SQLAlchemy 2 in FastAPI with a session per request, safe transactions, no N+1, thin repositories, Alembic and pgvector.
---

Django gives you the ORM, the connection handling and the migrations in one box. FastAPI gives you none of them, so you assemble the data layer yourself, and interviewers for Python AI roles probe exactly that assembly. After this note you can wire async SQLAlchemy 2 into FastAPI with one session per request, put transaction boundaries where they belong, load relationships without N+1 queries, split code into repository and service layers without over-abstracting, and explain how Alembic and pgvector fit. In the finance data platform role this is the plumbing under every invoice API, every reconciliation job and every RAG store that keeps chunks and embeddings in PostgreSQL.

## Prerequisites

[Dependency injection]({{ '/ai-stack/fastapi-dependency-injection/' | relative_url }}) (yield dependencies) and [async and performance]({{ '/ai-stack/fastapi-async-and-performance/' | relative_url }}). You should know the Django ORM well.

## The SQLAlchemy mental model

SQLAlchemy has two layers. **Core** builds SQL from Python expressions. The **ORM** maps classes to tables on top of Core. Three objects matter:

- **Engine**: owns the connection pool. One per process, created at startup.
- **Session**: a unit of work. It tracks objects you add or change (the identity map), flushes them as SQL, and owns one transaction at a time. One per request.
- **Mapped classes**: your models.

The 2.x style replaces the old `session.query(Model)` API: you build a statement with `select()`, then hand it to the session. Statements are plain values, so you can compose them, pass them around and test them without a database. `Query` still exists for legacy code; do not write new code with it.

```text
HTTP request
   │
   ▼
get_session (yield dependency) ──► InvoiceService ──► InvoiceRepo
   │                                  │ commit()          │ select()
   ▼                                  ▼                   ▼
AsyncSession: unit of work + identity map, one per request
   │ checks out a connection on first query
   ▼
AsyncEngine ──► connection pool ──► PostgreSQL
```

## Models and queries in 2.x style

Declarative models use `Mapped[...]` annotations; the type drives the column type and nullability (`Mapped[str | None]` is nullable). Use `Numeric(18, 2)` with `Decimal` for money, never `Float`.

Install with `pip install "sqlalchemy[asyncio]" aiosqlite`. SQLAlchemy 2.1 no longer pulls in `greenlet` by default, and the async extension fails to import without it; the `asyncio` extra brings it back. On PostgreSQL you swap `aiosqlite` for `asyncpg` or `psycopg`.

Save this block as `ledger.py`; the FastAPI app later imports it.

```python
# ledger.py
import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint, event, func, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload


class Base(DeclarativeBase):
    pass


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(200))
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="vendor", lazy="raise")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("vendor_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"))
    number: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    issued_on: Mapped[date]
    status: Mapped[str] = mapped_column(String(20), default="draft")
    vendor: Mapped[Vendor] = relationship(back_populates="invoices", lazy="raise")


class GLEntry(Base):
    __tablename__ = "gl_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    account: Mapped[str] = mapped_column(String(20))
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))


async def demo() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sql_log: list[str] = []
    event.listen(engine.sync_engine, "before_cursor_execute",
                 lambda conn, cur, stmt, *rest: sql_log.append(stmt))
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session, session.begin():  # commits on exit
        for v in range(3):
            session.add(Vendor(tenant_id="t1", name=f"Vendor {v}", invoices=[
                Invoice(number=f"INV-{v}-{i}", amount=Decimal("100.50") * (i + 1),
                        issued_on=date(2026, 1, i + 1))
                for i in range(2)
            ]))

    async with Session() as session:
        stmt = select(Invoice).where(Invoice.amount > 150).order_by(Invoice.number)
        big = (await session.scalars(stmt)).all()
        assert [i.number for i in big] == ["INV-0-1", "INV-1-1", "INV-2-1"]

        totals = (await session.execute(
            select(Vendor.name, func.sum(Invoice.amount).label("total"))
            .join(Vendor.invoices)
            .group_by(Vendor.name)
            .order_by(Vendor.name)
        )).all()
        assert totals[0].total == Decimal("301.50")

        try:
            big[0].vendor  # lazy load: raises instead of silently querying
        except InvalidRequestError:
            pass
        else:
            raise AssertionError("expected lazy='raise' to fire")

        sql_log.clear()
        vendors = (await session.scalars(
            select(Vendor).options(selectinload(Vendor.invoices))
        )).all()
        assert sum(len(v.invoices) for v in vendors) == 6
        assert len(sql_log) == 2  # vendors + one IN (...) query for all invoices

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(demo())
    print("ok")
```

Pick the result shape on purpose:

| Call | Returns | Use for |
|---|---|---|
| `await session.scalars(stmt)` | ORM objects (first column) | `select(Invoice)` |
| `await session.execute(stmt)` | `Row` tuples | multi-column or aggregate selects |
| `await session.scalar(stmt)` | one value or `None` | `select(func.count())`, single lookup |
| `await session.get(Invoice, 7)` | object by primary key | uses the identity map first |

## Async sessions and why lazy loading breaks

In sync SQLAlchemy, touching `invoice.vendor` quietly runs a query. In async code that query would need an `await` hidden inside an attribute access, which Python cannot do, so SQLAlchemy raises `MissingGreenlet`. Two settings prevent surprises:

- `async_sessionmaker(engine, expire_on_commit=False)`. By default, commit expires every loaded object, and the next attribute read triggers a refresh query. After a commit you usually return the object to FastAPI for serialization; with expiry on, that serialization tries implicit IO and fails.
- `lazy="raise"` on relationships. An accidental lazy load becomes an immediate, readable error in tests instead of `MissingGreenlet` or a hidden N+1 in production.

## Eager loading and N+1

N+1 is the same problem as in Django: one query for the parents, then one per parent for the children. You fix it by declaring the load strategy on the statement.

| Strategy | SQL | Use when | Django equivalent |
|---|---|---|---|
| `selectinload(Vendor.invoices)` | second query `WHERE vendor_id IN (...)` | one-to-many, collections | `prefetch_related` |
| `joinedload(Invoice.vendor)` | `LEFT OUTER JOIN` in the same query | many-to-one, single object | `select_related` |
| `raiseload("*")` | none, raises on access | lock down a hot endpoint | none |

The demo counts statements with a `before_cursor_execute` listener: two queries for three vendors and six invoices, no matter how many vendors you add. Use the same listener in tests to pin query counts on list endpoints, the way you would use `assertNumQueries` in Django. More background in [N+1 in Django]({{ '/python-django/n-plus-one/' | relative_url }}).

## Session per request with a yield dependency

The session lives exactly as long as the request. A yield dependency opens it, hands it to the route, and closes it at the end. Closing a session rolls back anything that was not committed, so a crash in the middle of a handler cannot leave half-written rows.

Where should `commit()` go? Not after the `yield`. In FastAPI 0.16x a yield dependency defaults to `scope="request"`: the code after `yield` runs after the response has been sent. If you commit there and the commit fails (a unique violation, a serialization failure), the client has already received a 201. Commit explicitly in the service, before returning. If you really want commit-in-dependency, declare `Depends(get_session, scope="function")` so the exit code runs before the response is sent and a failure becomes a 500.

> **Gotcha:** this changed across versions. FastAPI 0.106 moved yield cleanup before the response; later versions moved it back and added `scope`. Check the version before trusting old commit-after-`yield` code.

## Repositories, services and transactions

Keep three layers and stop there:

- **Route**: HTTP concerns. Parse input, call one service method, map domain errors to status codes.
- **Service**: business rules and the transaction boundary. Owns `commit()`.
- **Repository**: builds and runs queries. Never commits.

Skip the generic `BaseRepository[T]` with twenty methods; it hides the query you need to tune. A repository is a handful of named queries for one aggregate.

The service below posts an invoice to the general ledger: it flips the status and writes a balanced debit and credit. All three writes land in one transaction or none do. The row lock (`with_for_update`) stops two concurrent "post" calls from double-posting on PostgreSQL. A unique constraint, not a Python check, rejects duplicate invoice numbers, because a check-then-insert races under concurrency.

```python
# app.py (save the previous block as ledger.py next to it)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from ledger import Base, GLEntry, Invoice, Vendor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # StaticPool: one shared connection, so the in-memory DB survives across requests
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:  # demo only: real services run Alembic
        await conn.run_sync(Base.metadata.create_all)
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield
    await engine.dispose()


app = FastAPI(lifespan=lifespan)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session  # on close, anything not committed is rolled back


SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ---- API schemas: the contract, separate from ORM models ----
class VendorIn(BaseModel):
    name: str = Field(max_length=200)


class InvoiceIn(BaseModel):
    vendor_id: int
    number: str = Field(max_length=50)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    issued_on: date


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    number: str
    amount: Decimal
    status: str


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    invoices: list[InvoiceOut]


# ---- repository: query construction only, never commits ----
class InvoiceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_update(self, invoice_id: int) -> Invoice | None:
        # SELECT ... FOR UPDATE on Postgres; SQLite ignores the lock
        return await self.session.get(Invoice, invoice_id, with_for_update=True)

    async def vendor_with_invoices(self, vendor_id: int) -> Vendor | None:
        stmt = (select(Vendor).where(Vendor.id == vendor_id)
                .options(selectinload(Vendor.invoices)))
        return await self.session.scalar(stmt)

    async def gl_lines(self, invoice_id: int) -> list[GLEntry]:
        stmt = select(GLEntry).where(GLEntry.invoice_id == invoice_id)
        return list(await self.session.scalars(stmt))


# ---- service: business rules and the transaction boundary ----
class Conflict(Exception):
    pass


class InvoiceService:
    def __init__(self, session: SessionDep) -> None:
        self.session = session
        self.repo = InvoiceRepo(session)

    async def create(self, data: InvoiceIn) -> Invoice:
        invoice = Invoice(**data.model_dump())
        self.session.add(invoice)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict(f"invoice {data.number} already exists for this vendor")
        return invoice

    async def post_to_gl(self, invoice_id: int) -> Invoice:
        invoice = await self.repo.get_for_update(invoice_id)
        if invoice is None:
            raise LookupError(invoice_id)
        if invoice.status != "draft":
            raise Conflict("invoice already posted")
        invoice.status = "posted"
        self.session.add_all([
            GLEntry(invoice_id=invoice.id, account="6000-expense", debit=invoice.amount),
            GLEntry(invoice_id=invoice.id, account="2000-ap", credit=invoice.amount),
        ])
        await self.session.commit()  # status flip and both lines, or none of it
        return invoice


ServiceDep = Annotated[InvoiceService, Depends()]


@app.post("/vendors", status_code=201)
async def create_vendor(data: VendorIn, session: SessionDep) -> dict:
    vendor = Vendor(tenant_id="t1", name=data.name)
    session.add(vendor)
    await session.commit()
    return {"id": vendor.id}


@app.post("/invoices", status_code=201, response_model=InvoiceOut)
async def create_invoice(data: InvoiceIn, svc: ServiceDep):
    try:
        return await svc.create(data)
    except Conflict as e:
        raise HTTPException(409, str(e))


@app.post("/invoices/{invoice_id}/post", response_model=InvoiceOut)
async def post_invoice(invoice_id: int, svc: ServiceDep):
    try:
        return await svc.post_to_gl(invoice_id)
    except LookupError:
        raise HTTPException(404, "invoice not found")
    except Conflict as e:
        raise HTTPException(409, str(e))


@app.get("/invoices/{invoice_id}/gl")
async def invoice_gl(invoice_id: int, svc: ServiceDep) -> dict:
    lines = await svc.repo.gl_lines(invoice_id)
    return {"debits": sum(l.debit for l in lines), "credits": sum(l.credit for l in lines)}


@app.get("/vendors/{vendor_id}", response_model=VendorOut)
async def get_vendor(vendor_id: int, svc: ServiceDep):
    vendor = await svc.repo.vendor_with_invoices(vendor_id)
    if vendor is None:
        raise HTTPException(404, "vendor not found")
    return vendor


if __name__ == "__main__":
    with TestClient(app) as client:  # `with` runs the lifespan
        vid = client.post("/vendors", json={"name": "Acme Supplies"}).json()["id"]
        body = {"vendor_id": vid, "number": "INV-001", "amount": "1250.00",
                "issued_on": "2026-03-01"}
        inv = client.post("/invoices", json=body)
        assert inv.status_code == 201 and inv.json()["status"] == "draft"
        assert client.post("/invoices", json=body).status_code == 409

        iid = inv.json()["id"]
        assert client.post(f"/invoices/{iid}/post").json()["status"] == "posted"
        assert client.post(f"/invoices/{iid}/post").status_code == 409
        assert client.get(f"/invoices/{iid}/gl").json() == {"debits": "1250.00",
                                                             "credits": "1250.00"}
        vendor = client.get(f"/vendors/{vid}").json()
        assert [i["number"] for i in vendor["invoices"]] == ["INV-001"]
        assert client.get("/vendors/999").status_code == 404
    print("ok")
```

Points worth saying out loud in an interview:

- `InvoiceService` is itself a dependency (`Depends()` with no argument uses the class). FastAPI builds it per request and injects the session, so routes never touch `AsyncSession` directly for business writes.
- `get_vendor` returns an ORM object; `response_model=VendorOut` with `from_attributes=True` converts it. Because `invoices` was loaded with `selectinload`, serialization does no IO.
- Decimal amounts go out as strings (`"1250.00"`), which is what you want for money in JSON.
- For multi-step work that must be atomic but spans several service calls, use `async with session.begin():` around the whole block, and `session.begin_nested()` for a savepoint that can fail without killing the outer transaction (for example, "try to insert, skip duplicates" inside a bulk import).

> **In production:** a nightly reconciliation of 2 million bank lines does not belong in a request. Run it in a worker with its own session, commit in chunks, and use the PostgreSQL `insert(...).on_conflict_do_nothing()` for idempotent loads.

## Migrations with Alembic (illustrative)

`create_all` is for demos and tests. Real schemas change through reviewed, versioned migrations, which is Alembic's job.

```bash
pip install alembic
alembic init -t async migrations      # async template: env.py uses an AsyncEngine
# in migrations/env.py:  from app.models import Base; target_metadata = Base.metadata
alembic revision --autogenerate -m "add gl_entries"
alembic upgrade head                  # run in a deploy step, not at app startup
alembic downgrade -1
```

Autogenerate diffs models against the live schema and misses renames (it sees drop plus add) and some constraint changes, so read every generated file. Keep data backfills out of schema migrations on large tables, and follow the expand/contract approach in [zero-downtime migrations]({{ '/postgresql/zero-downtime-migrations/' | relative_url }}).

## PostgreSQL and pgvector (illustrative)

Production settings and a chunk table for RAG. Keeping embeddings next to the business rows lets you filter by tenant and entitlements in the same SQL query as the similarity search.

```python
# illustrative: needs PostgreSQL with the vector extension, asyncpg and pgvector
from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, String, Text, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

engine = create_async_engine(
    "postgresql+asyncpg://app:***@db:5432/finance",
    pool_size=10, max_overflow=5,   # per process: workers x 15 <= DB connection budget
    pool_pre_ping=True,             # drop dead connections after failovers
    pool_recycle=1800,
)


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    __table_args__ = (
        Index("ix_chunks_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


async def search(session, tenant_id: str, query_vec: list[float], k: int = 5):
    stmt = (select(PolicyChunk)
            .where(PolicyChunk.tenant_id == tenant_id)
            .order_by(PolicyChunk.embedding.cosine_distance(query_vec))
            .limit(k))
    return (await session.scalars(stmt)).all()
```

Details on index types and recall tuning are in [pgvector]({{ '/postgresql/pgvector/' | relative_url }}).

**SQLModel** merges a Pydantic model and a SQLAlchemy model into one class. It is pleasant for small CRUD services and tutorials. For a finance service, keep ORM models and API schemas separate: the table shape and the public contract change for different reasons, and you will want SQLAlchemy features (hybrid properties, custom loaders, dialect inserts) that SQLModel wraps late or not at all.

## Coming from Django

| Django | SQLAlchemy 2 in FastAPI |
|---|---|
| `models.Model` | `DeclarativeBase` subclass with `Mapped[...]` |
| `Invoice.objects.filter(amount__gt=150)` | `select(Invoice).where(Invoice.amount > 150)` |
| QuerySet runs on iteration | statement runs on `session.scalars()` / `execute()` |
| implicit connection per request | explicit `AsyncSession` from a yield dependency |
| autocommit plus `transaction.atomic()` | autobegin; you call `commit()` or wrap in `session.begin()` |
| `select_related` / `prefetch_related` | `joinedload` / `selectinload` |
| `select_for_update()` | `with_for_update=True` or `.with_for_update()` |
| `annotate(total=Sum(...))` | `func.sum(...).label("total")` with `group_by` |
| `makemigrations` / `migrate` | `alembic revision --autogenerate` / `alembic upgrade head` |
| DRF serializer | Pydantic schema with `from_attributes=True` |

The biggest shift: Django hides the session. In SQLAlchemy you own its lifetime, and most bugs come from a session living too long (shared across requests or tasks) or too short (closed before serialization).

## Common mistakes

- **One global session.** An `AsyncSession` is not safe for concurrent use. Share the engine, never the session. Do not pass a request's session into a `BackgroundTasks` job or `asyncio.gather` branches; open a new one.
- **Committing after `yield`.** The response is already gone. Commit in the service, or use `scope="function"`.
- **Leaving `expire_on_commit=True`.** Returning an object right after commit triggers `MissingGreenlet`. Set it to `False`.
- **Float for money.** Use `Numeric` plus `Decimal` end to end, including Pydantic fields.
- **Check-then-insert for uniqueness.** Two requests both pass the check. Use a unique constraint and catch `IntegrityError`.
- **Pool larger than the database allows.** `pool_size + max_overflow` is per process. Eight workers with 15 each is 120 connections; add PgBouncer or shrink the pool.
- **`create_all` at startup in production.** It never alters existing tables and races between replicas. Use Alembic in a deploy step.

## Practice tasks

1. Add `GET /vendors?limit=&offset=` that returns vendors with their invoices. Done: a test asserts exactly two SQL statements regardless of vendor count.
2. Add `POST /invoices/{id}/void` that writes reversing GL entries in one transaction. Done: the trial balance endpoint still returns equal debits and credits, and voiding twice returns 409.
3. Add a bulk import endpoint that takes 500 invoices, uses `begin_nested()` per row, skips duplicates and returns counts of inserted and skipped rows. Done: re-posting the same file inserts zero rows.
4. Point the app at PostgreSQL in Docker, generate the first Alembic migration, and run two concurrent `post` calls on the same invoice. Done: one succeeds, one gets 409, and the ledger has exactly two lines.
5. Add the `PolicyChunk` table with pgvector and a `/search` endpoint filtered by tenant. Done: a test shows tenant A never sees tenant B's chunks even when B's chunk is the nearest neighbor.

## Interview questions

**What is the difference between an engine and a session?**
The engine owns the connection pool and lives for the process. The session is a short-lived unit of work with an identity map and one transaction at a time. Share engines, never sessions.

**How do you scope a session in FastAPI?**
A yield dependency creates it per request and closes it after. Closing rolls back uncommitted work. The service layer commits explicitly.

**Why can't you lazy load in async SQLAlchemy?**
Attribute access cannot await, so the implicit query would block the event loop; SQLAlchemy raises `MissingGreenlet` instead. Load eagerly with `selectinload` or `joinedload`, and set `lazy="raise"` to catch misses in tests.

**`selectinload` or `joinedload`?**
`selectinload` for collections: a second `IN` query, no row multiplication. `joinedload` for many-to-one: one query with a join. Joining a collection repeats each parent row per child (2.x makes you call `.unique()`) and forces a subquery around `LIMIT`.

**Where does the transaction boundary belong?**
In the service, around one business operation (post an invoice: status change plus both ledger lines). Not in the route, not in the repository, and not after the dependency's `yield`.

**How do you prevent double-posting under concurrency?**
Lock the row with `SELECT ... FOR UPDATE`, check the status inside the same transaction, and back it with a constraint or idempotency key so retries are safe.

**How would you store embeddings for RAG in this stack?**
A pgvector column on a chunk table with `tenant_id` and document metadata, an HNSW index, and similarity queries that filter by tenant in the same SQL. Migrations through Alembic like any other table.

## Further reading

- SQLAlchemy docs: "ORM Quick Start", "Asynchronous I/O (asyncio)", "Relationship Loading Techniques", "Session Basics"
- FastAPI docs: "SQL (Relational) Databases", "Dependencies with yield"
- Alembic docs: "Tutorial" and "Auto Generating Migrations"
- pgvector-python README, SQLAlchemy section
- Notes: [PostgreSQL connection pooling]({{ '/postgresql/connection-pooling/' | relative_url }}), [transactions and isolation]({{ '/postgresql/transactions-and-isolation/' | relative_url }})
