---
title: Relational design, normalization and denormalization
section: backend
order: 11
phase: "Phase 2 · Databases"
summary: Model entities, keys and constraints, normalize through BCNF to remove anomalies, then denormalize deliberately with a plan to keep copies consistent.
---

A schema outlives every service that touches it. Code bugs get fixed with a deploy; data bugs (duplicate customers, orphaned rows, an email that differs between two tables) stay in the database and leak into reports and support tickets. The workflow that avoids most of them: model the domain, normalize until each fact lives in one place, let constraints enforce invariants, and denormalize only measured read paths, with an explicit mechanism that keeps every copy correct.

## Relational database design

### ER modelling

Entity-relationship modelling turns the domain into **entities** (things with identity: tenant, customer, order), **attributes** (facts about them: email, status, total) and **relationships** with a **cardinality** (one-to-one, one-to-many, many-to-many) and an **optionality** (must an order have a customer?). For a multi-tenant commerce backend:

```text
 tenants 1───N customers 1───N orders 1───N order_items N───1 products
    │                                                           │
    └──────────────────────────1───N────────────────────────────┘

 products N───M tags        resolved by product_tags (junction table)
 categories 1───N categories (self-reference: parent_id)
```

Per relationship, ask: does deleting the parent delete the children, can the link change over time, and does the relationship carry data (a member's role in a project)? Data on a relationship makes it a table.

### Keys

| Key | Meaning | Example |
|---|---|---|
| Superkey | Any column set that uniquely identifies a row | `(id, email)` |
| Candidate key | A minimal superkey | `id`; `(tenant_id, email)` |
| Primary key | The candidate key you pick as the row's identity | `id` |
| Alternate key | Other candidate keys, enforced with `UNIQUE` | `(tenant_id, email)` |
| Foreign key | Columns that must match a key in another table | `orders.customer_id` |
| Composite key | A key with more than one column | `(order_id, product_id)` |

**Natural vs surrogate keys.** Natural keys (email, SKU) come from the domain; surrogate keys (`bigint` identity, UUID) are generated. Use a surrogate primary key for almost every entity, because natural keys change and every foreign key would change with them. Still enforce the natural key with `UNIQUE`; a surrogate key alone lets duplicates in.

**`bigint` vs UUID.** `bigint` identity is compact and index-friendly but needs a central sequence and reveals row counts. Random UUIDv4 scatters inserts across the whole B-tree, hurting cache hit rate. Time-ordered UUIDv7 is globally unique with mostly-sequential inserts; PostgreSQL 18 ships `uuidv7()`, and older versions can generate them in the app.

### Relationships

- **One-to-many:** a foreign key on the "many" side (`orders.customer_id`).
- **One-to-one:** a `UNIQUE` foreign key, or a shared primary key (`user_profiles.user_id` is PK and FK).
- **Many-to-many:** a junction table with two foreign keys and a composite primary key, plus relationship attributes.
- **Self-referential:** `categories.parent_id REFERENCES categories(id)`. Query with recursive CTEs, or use a closure table or `ltree` for deep, read-heavy trees.
- **Polymorphic** ("a comment belongs to a post or a photo"): the Rails-style `commentable_type, commentable_id` pair cannot have a foreign key. Prefer one nullable FK per target with `CHECK (num_nonnulls(post_id, photo_id) = 1)`, or separate tables.

### Constraints

Constraints are the only invariants that hold whichever code path or script writes.

| Constraint | Enforces | Note |
|---|---|---|
| `NOT NULL` | Value required | Default to `NOT NULL`; allow `NULL` only with a meaning |
| `CHECK` | Row-level rule | `CHECK (quantity > 0)`, `CHECK (status IN (...))` |
| `UNIQUE` | No duplicates | Backed by a unique B-tree index |
| `PRIMARY KEY` | `UNIQUE` + `NOT NULL` | One per table |
| `FOREIGN KEY` | Referenced row exists | Choose `ON DELETE` deliberately |
| `EXCLUDE` | No two rows conflict by an operator | No overlapping bookings for a room |

"SELECT to check the email is free, then INSERT" races: two requests both see no row and both insert. A unique constraint is the only race-free check.

PostgreSQL doesn't index foreign key columns automatically (index them, or parent deletes scan the child table). Foreign key, unique and exclusion constraints can be `DEFERRABLE INITIALLY DEFERRED`, checked at commit, which helps with mutually-referencing rows.

## Database normalization

Normalization stores each fact once. It's defined via **functional dependencies**: `X → Y` means that rows with the same `X` always have the same `Y` (`customer_id → customer_email`).

### Anomalies

Start from one wide table that looks convenient for an order screen:

| order_id | order_date | customer_id | customer_email | product_id | product_name | list_price | qty |
|---|---|---|---|---|---|---|---|
| 1001 | 2026-11-02 | 7 | ana@acme.io | 55 | Desk lamp | 40.00 | 2 |
| 1001 | 2026-11-02 | 7 | ana@acme.io | 61 | Cable | 9.00 | 1 |
| 1002 | 2026-11-03 | 7 | ana@acme.io | 55 | Desk lamp | 40.00 | 1 |

- **Update anomaly:** Ana changes her email; miss one of her rows and the data contradicts itself.
- **Insert anomaly:** you can't record a product until someone orders it (`order_id` is part of the key).
- **Delete anomaly:** delete orders 1001 and 1002 and you lose that product 55 exists and costs 40.00.

The dependencies are: `(order_id, product_id) → qty`, `product_id → product_name, list_price`, `order_id → order_date, customer_id`, `customer_id → customer_email`. The only candidate key is `(order_id, product_id)`.

### First normal form (1NF)

Every column holds a single value from its domain, and there are no repeating groups. Violations: `item1, item2, item3` columns, or a comma-separated `tags` column. They make it impossible to put a foreign key or a unique constraint on an individual value. The fix is a child table (`product_tags`).

PostgreSQL arrays and JSONB are a pragmatic exception: fine for values you read and write as a whole, filter with a GIN index, and never need referential integrity for. If you need a foreign key to each element, it's a table.

### Second normal form (2NF)

1NF, plus every non-key attribute depends on the **whole** of every candidate key, not part of it. It only bites with composite keys. In the wide table, `product_name` and `list_price` depend on `product_id` alone, and `order_date` and `customer_id` on `order_id` alone. Split them out:

- `products(product_id PK, product_name, list_price)`
- `orders(order_id PK, order_date, customer_id, customer_email)`
- `order_items(order_id, product_id, qty, PK (order_id, product_id))`

### Third normal form (3NF)

2NF, plus no **transitive** dependency of a non-key attribute on the key: `order_id → customer_id → customer_email`. The email is a fact about the customer, not the order. Move it to `customers(customer_id PK, email)`. Now Ana's email lives in one row.

### Boyce-Codd normal form (BCNF)

Every determinant (left side of a non-trivial dependency) must be a superkey. 3NF allows `X → Y` with non-key `X` if `Y` is part of a candidate key. Classic case: each instructor teaches one course, and a student takes a course with one instructor:

- `(student, course) → instructor`
- `instructor → course`

Candidate keys are `(student, course)` and `(student, instructor)`. Every attribute is part of a key, so it's 3NF, yet "Lee teaches Databases" repeats for every student. `instructor` is a determinant but not a superkey: a BCNF violation.

Decompose into `instructor_courses(instructor PK, course)` and `enrollments(student, instructor)`. The redundancy is gone, but the rule "one instructor per student per course" no longer fits a single table's unique constraint; this decomposition is not dependency-preserving. In PostgreSQL you can keep both properties with controlled redundancy: store `course` on `enrollments`, add `UNIQUE (student, course)`, and a composite foreign key `(instructor, course) REFERENCES instructor_courses (instructor, course)` (backed by a `UNIQUE (instructor, course)` there) so the copy can't drift.

### Normal forms at a glance

| Form | Rule | Fixes |
|---|---|---|
| 1NF | Atomic values, no repeating groups | Can't constrain or index individual items |
| 2NF | No partial dependency on a composite key | Product facts copied into every order line |
| 3NF | No transitive dependency via a non-key column | Customer facts copied into every order |
| BCNF | Every determinant is a superkey | Residual redundancy among key columns |
| 4NF | No independent multi-valued facts in one table | Cross-product rows (skills x languages) |

> **Rule of thumb:** Design to 3NF or BCNF by default. In practice that's "one table per entity, one per relationship, every fact stored once". Going further than BCNF rarely matters outside textbook cases.

Not everything that looks duplicated is redundancy. `order_items.unit_price` is the price the customer **paid**, a fact about the sale, not a copy of `products.list_price`; so is an order's shipping address snapshot. Normalizing them away rewrites history.

## Database denormalization

Denormalization stores derived or duplicated data to make reads cheaper, trading write cost and consistency risk for read speed. It needs a reason (a measured slow read path) and an owner (a mechanism that keeps the copy right).

### When to denormalize

- A hot read path joins or aggregates many rows per request, and indexes and query tuning didn't fix it. See [Query optimization and N+1]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }}) first.
- Dashboards that aggregate millions of rows per page load.
- Data owned by another service that you need locally (customer names inside billing).

**Don't** denormalize write-heavy data, data where staleness causes wrong decisions (balances, sellable inventory), or anything an index would fix.

### How: techniques

| Technique | Example | Freshness | Kept consistent by |
|---|---|---|---|
| Duplicated column | `tenant_id` on `order_items`; `customer_name` on `orders` | Immediate | Same transaction, trigger, or composite FK |
| Counter / aggregate column | `posts.comment_count` | Immediate or near | Trigger or app code in same transaction |
| Summary (rollup) table | `daily_sales(tenant_id, day, revenue)` | Seconds to minutes | Incremental upsert job |
| Materialized view | `mv_tenant_usage` | As of last refresh | `REFRESH MATERIALIZED VIEW CONCURRENTLY` |
| Read model / projection | Denormalized order-summary table or document | Eventual | Events via outbox or CDC, rebuildable |

**Read models** are the general form: a table or document shaped exactly like the screen that reads it, built from events or CDC; the query side of CQRS, see [Event sourcing and CQRS]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }}).

**Summary tables** pre-aggregate by a time bucket, updated incrementally with `INSERT ... ON CONFLICT DO UPDATE`.

**Materialized views** store a query result; each refresh recomputes it all. `CONCURRENTLY` keeps reads going and needs a unique index on the view. See [Rollups and materialized views]({{ '/postgresql/rollups-and-materialized-views/' | relative_url }}).

### Keeping copies consistent

From strongest to weakest:

1. **Database-enforced:** a composite foreign key that includes the copied column (with `ON UPDATE CASCADE` if it can change), or a `GENERATED ALWAYS AS (...) STORED` column for values derived from the same row.
2. **Triggers:** run in the writing transaction, covering every code path. Costs: hidden logic, write latency, row-lock contention on shared aggregate rows.
3. **App code in the same transaction:** until a script or bulk `UPDATE` bypasses it.
4. **Async projection:** an outbox or CDC stream updates the copy after commit. Eventually consistent; consumers must be idempotent and tolerate reordering.
5. **Reconciliation job:** recompute from the source and fix drift; a safety net under all of the above. Alert on rows fixed.

> **Gotcha:** A counter row incremented by every write becomes a lock hotspot. Thousands of comments per second on one post serialize on its row. Fixes: split the counter into N rows and sum them, batch increments, or show an approximate count.

## In practice

A normalized, tenant-safe core schema (illustrative). The composite foreign keys guarantee an order item can never reference another tenant's order or product:

```sql
CREATE TABLE customers (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  bigint NOT NULL REFERENCES tenants (id),
  email      text   NOT NULL,
  name       text   NOT NULL,
  UNIQUE (tenant_id, id)                 -- target for composite FKs
);
CREATE UNIQUE INDEX customers_tenant_email_key
  ON customers (tenant_id, lower(email));

CREATE TABLE orders (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id    bigint NOT NULL,
  customer_id  bigint NOT NULL,
  status       text   NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'paid', 'shipped', 'cancelled')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id)
);
CREATE INDEX orders_customer_idx ON orders (tenant_id, customer_id);

CREATE TABLE order_items (
  tenant_id   bigint  NOT NULL,
  order_id    bigint  NOT NULL,
  product_id  bigint  NOT NULL,
  quantity    integer NOT NULL CHECK (quantity > 0),
  unit_price  numeric(12, 2) NOT NULL CHECK (unit_price >= 0),  -- price paid
  PRIMARY KEY (order_id, product_id),
  FOREIGN KEY (tenant_id, order_id)
    REFERENCES orders (tenant_id, id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, product_id) REFERENCES products (tenant_id, id)
);  -- products also declares UNIQUE (tenant_id, id)
CREATE INDEX order_items_product_idx ON order_items (tenant_id, product_id);
```

A trigger-maintained counter (illustrative). It runs inside the inserting transaction, so the count can't drift from the rows:

```sql
ALTER TABLE posts ADD COLUMN comment_count integer NOT NULL DEFAULT 0;

CREATE FUNCTION bump_comment_count() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    UPDATE posts SET comment_count = comment_count + 1 WHERE id = NEW.post_id;
  ELSIF TG_OP = 'DELETE' THEN
    UPDATE posts SET comment_count = comment_count - 1 WHERE id = OLD.post_id;
  END IF;
  RETURN NULL;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER comments_count_trg
AFTER INSERT OR DELETE ON comments
FOR EACH ROW EXECUTE FUNCTION bump_comment_count();
```

In Django, declare the same rules in `Meta.constraints` with `UniqueConstraint` (expressions such as `Lower("email")` are allowed) and `CheckConstraint`. Django can't declare composite foreign keys on regular models; add them with a `RunSQL` migration.

Normal forms are mechanical once you know the dependencies. This stdlib script finds candidate keys and the dependencies that break 3NF and BCNF in both examples above:

```python
from itertools import combinations


def fd(text):  # 'a b -> c' => (lhs set, rhs set)
    lhs, rhs = text.split("->")
    return frozenset(lhs.split()), frozenset(rhs.split())


def closure(attrs, fds):  # everything attrs determines
    result = set(attrs)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return frozenset(result)


def candidate_keys(attrs, fds):  # minimal sets determining everything
    attrs, keys = frozenset(attrs), []
    for size in range(1, len(attrs) + 1):
        for combo in combinations(sorted(attrs), size):
            c = frozenset(combo)
            if not any(k <= c for k in keys) and closure(c, fds) == attrs:
                keys.append(c)
    return keys


def violations(attrs, fds):  # -> (breaks 3NF, breaks BCNF)
    attrs = frozenset(attrs)
    prime = frozenset().union(*candidate_keys(attrs, fds))
    not_3nf, not_bcnf = [], []
    for lhs, rhs in fds:
        extra = rhs - lhs
        if not extra or closure(lhs, fds) == attrs:
            continue  # trivial, or lhs is a superkey: fine everywhere
        not_bcnf.append((lhs, rhs))
        if extra - prime:  # determines a non-key attribute
            not_3nf.append((lhs, rhs))
    return not_3nf, not_bcnf


if __name__ == "__main__":
    # One wide order_lines table: 2NF and 3NF problems.
    lines = "order_id product_id qty list_price product_name " \
            "order_date customer_id customer_email".split()
    lines_fds = [fd("order_id product_id -> qty"),
                 fd("product_id -> product_name list_price"),
                 fd("order_id -> order_date customer_id"),
                 fd("customer_id -> customer_email")]
    assert candidate_keys(lines, lines_fds) == [
        frozenset({"order_id", "product_id"})]
    not_3nf, not_bcnf = violations(lines, lines_fds)
    assert len(not_3nf) == 3 and len(not_bcnf) == 3

    # Enrollment: in 3NF but not BCNF (instructor -> course).
    enroll = ["student", "course", "instructor"]
    enroll_fds = [fd("student course -> instructor"),
                  fd("instructor -> course")]
    keys = candidate_keys(enroll, enroll_fds)
    assert set(keys) == {frozenset({"student", "course"}),
                         frozenset({"student", "instructor"})}
    not_3nf, not_bcnf = violations(enroll, enroll_fds)
    assert not_3nf == [] and not_bcnf == [fd("instructor -> course")]
    print("ok")
```

## Pitfalls

- **Entity-attribute-value tables** (`entity_id, attribute, value`) to avoid migrations. You lose types, constraints and sane queries; use columns or JSONB.
- **Unindexed foreign keys.** Parent deletes are slow and hold locks while scanning children. Index every FK column.
- **Soft deletes vs uniqueness.** A deleted user's email still blocks sign-up. Use a partial unique index `WHERE deleted_at IS NULL`.
- **Denormalized copies with no owner.** A `customer_name` on orders that nobody updates. Decide if it's a snapshot (leave it) or a copy (needs sync plus reconciliation).
- **Over-normalizing.** A lookup table for every three-value enum. A `CHECK` constraint is enough when values rarely change and carry no data.
- **Money in floats.** Use `numeric` or integer minor units (cents).

## Interview questions

**Q: Explain 1NF, 2NF and 3NF with one example.**
A wide order-lines table repeats product and customer data. 1NF: no lists in a column. 2NF: product name depends only on `product_id`, part of the `(order_id, product_id)` key, so move it to `products`. 3NF: customer email depends on `customer_id`, which depends on `order_id`, so move it to `customers`.

**Q: Can a 3NF table still have anomalies?**
Yes, when a non-key attribute determines part of a candidate key, e.g. `instructor → course` in an enrollment table. BCNF removes it but may lose the ability to enforce some dependencies with one table's constraints.

**Q: When do you denormalize?**
When a measured read path is still too slow after indexing and query tuning: aggregates, multi-table listings, another service's data. Pair it with a consistency mechanism and a rebuild path.

**Q: How do you keep a `comment_count` column correct?**
Update it in the inserting transaction, ideally via a trigger. At high write rates, shard or batch the counter. Back it with periodic reconciliation.

**Q: Surrogate or natural primary key? UUID or bigint?**
Surrogate PK, natural key enforced with `UNIQUE`. `bigint` identity inside one database; UUIDv7 when IDs are generated without coordination (clients, shards). Avoid random UUIDv4 keys on big, write-heavy tables.

**Q: Materialized view or summary table?**
A materialized view when a scheduled full recompute is cheap enough. An incrementally updated summary table for large data and fresher results, at the cost of upsert logic and handling late data.

## Further reading

- E. F. Codd, "A Relational Model of Data for Large Shared Data Banks" (Communications of the ACM, 1970).
- C. J. Date, *Database Design and Relational Theory: Normal Forms and All That Jazz*.
- Bill Karwin, *SQL Antipatterns*.
- PostgreSQL documentation: "Constraints" (Data Definition chapter) and "Materialized Views".
- Martin Kleppmann, *Designing Data-Intensive Applications*, chapter 2 and chapter 11 (derived data).
