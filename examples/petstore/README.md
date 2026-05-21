# Petstore example

Demonstrates specmcp against the classic Petstore API.

## Setup

```bash
cp .env.example .env
# Edit .env and set PETSTORE_API_KEY
export $(cat .env | xargs)
```

## Preview tools

```bash
specmcp inspect --config mcp.config.yaml
```

Output:
```
Spec    : test-corpus/petstore.json  (OpenAPI 3.0)
Summary : 4 operations → 4 tools

  ┌─ listPets
  │  GET https://petstore.example.com/v1/pets
  │  List all pets [GET /pets]
  ...
```

## Run as MCP server

```bash
specmcp serve --config mcp.config.yaml
```

Add to Claude Desktop's `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "petstore": {
      "command": "specmcp",
      "args": ["serve", "--config", "/path/to/examples/petstore/mcp.config.yaml"]
    }
  }
}
```

## Tools exposed

| Tool | Method | Path | Description |
|---|---|---|---|
| `listPets` | GET | `/pets` | List all pets (optional `limit`) |
| `createPet` | POST | `/pets` | Create a pet (`name` required, `tag` optional) |
| `getPetById` | GET | `/pets/{petId}` | Get a pet by ID |
| `deletePet` | DELETE | `/pets/{petId}` | Delete a pet |
