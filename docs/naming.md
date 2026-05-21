# specmcp Naming Algorithm

**Status:** v1 (stable — changes require a deprecation cycle)

This document defines how specmcp derives a tool name from an OpenAPI operation. The algorithm is deterministic: the same spec always produces the same names. Any change to this algorithm is a breaking change and requires a major or minor version bump with a deprecation notice.

## Algorithm

### Step 1 — Prefer `operationId`

If the operation has an `operationId` field and it is non-empty after stripping whitespace, use it directly as the tool name. No further transformation is applied.

```yaml
# Input
operationId: getUserById
# Output tool name: getUserById
```

### Step 2 — Fallback: derive from method + path

If `operationId` is absent or empty, derive the name from the HTTP method and path:

1. Lowercase the HTTP method: `GET` → `get`, `POST` → `post`, etc.
2. Take the path string (e.g. `/users/{id}/orders`).
3. Replace each `/` with `_`.
4. Strip all `{` and `}` characters (path parameter braces).
5. Collapse consecutive `_` into a single `_`.
6. Strip leading and trailing `_`.
7. Prepend the lowercased method and an `_`.

```
GET  /users/{id}/orders
→  get_users_id_orders

POST /api/v2/items/{item_id}/tags
→  post_api_v2_items_item_id_tags

DELETE /v1/users
→  delete_v1_users
```

### Step 3 — Collision resolution

If two or more operations in the same spec produce the same tool name (after steps 1–2), resolve collisions by appending `_2`, `_3`, … in the order the operations appear in the spec (top-to-bottom, paths object order, then method order within a path: GET, PUT, POST, PATCH, DELETE, HEAD, OPTIONS).

```
# Both operations lack operationId and map to "get_users_id"
GET /users/{id}    → get_users_id      (first in spec order)
GET /users/{id}    → get_users_id_2    (second)
```

The first occurrence keeps the undecorated name. Collision suffixes start at `_2`.

### Invariants

- The output name is always a non-empty string.
- The output name contains only ASCII letters, digits, and underscores.
- The output name never starts or ends with `_`.
- Two different operations always have different tool names.

## Golden-file test cases

These inputs and expected outputs are checked by `tests/unit/test_naming_algorithm.py`. Any change to the expected output is a breaking change.

| Method | Path | operationId | Expected tool name |
|--------|------|-------------|-------------------|
| GET | /pets | getPets | `getPets` |
| GET | /pets/{petId} | getPetById | `getPetById` |
| POST | /pets | (none) | `post_pets` |
| DELETE | /pets/{petId} | (none) | `delete_pets_petId` |
| GET | /users/{id}/orders/{orderId} | (none) | `get_users_id_orders_orderId` |
| GET | /v1/api/items | (none) | `get_v1_api_items` |
| PUT | / | (none) | `put` |
| GET | /{id} | (none) | `get_id` |

> Note: `delete_pets_petId` — brace stripping removes `{` and `}` but the parameter name (`petId`) is kept. This makes names readable without braces.
