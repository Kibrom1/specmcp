# Stripe example

Exposes Stripe payment and customer management endpoints as MCP tools.
Read operations and safe write operations are included by default.
Destructive operations (refunds, deletions) are excluded — add them
to `include_operations` if needed.

## Setup

```bash
cp .env.example .env
# Edit .env — get your key at https://dashboard.stripe.com/apikeys
# Use sk_test_... for development, sk_live_... for production.
export $(cat .env | xargs)
```

## Preview tools

```bash
specmcp inspect --config mcp.config.yaml
```

Note: the Stripe spec is large. The first run downloads and resolves it;
subsequent runs use the cached version.

## Run as MCP server

```bash
specmcp serve --config mcp.config.yaml
```

## Tools exposed

| Tool | Description |
|---|---|
| `list_customers` | List customers (filter by email) |
| `create_customer` | Create a customer |
| `get_customer` | Get a customer by ID |
| `update_customer` | Update customer details |
| `list_payment_intents` | List payment intents |
| `create_payment_intent` | Create a payment intent |
| `get_payment_intent` | Get a payment intent |
| `list_invoices` | List invoices |
| `get_invoice` | Get an invoice |
| `list_subscriptions` | List subscriptions |
| `get_subscription` | Get a subscription |
| `list_products` | List products |
| `get_product` | Get a product |
| `list_prices` | List prices |
| `get_price` | Get a price |
| `list_balance_transactions` | List balance transactions |
| `list_charges` | List charges |
| `get_charge` | Get a charge |

## Safety note

This config uses `include_operations` to explicitly allow-list endpoints.
Any Stripe operation not on the list is hidden from the LLM, even if it
appears in the spec. Review and trim the list for your use case.
