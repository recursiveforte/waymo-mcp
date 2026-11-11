# Waymo MCP

> Fork of nathanielangafor/waymo-mcp with a stricter quote → book flow and
> San Francisco search defaults.

An unofficial local [Model Context Protocol](https://modelcontextprotocol.io/)
server for Waymo ride management.

It gives an agent eight tools:

- Search Waymo places
- Get ride history
- Get active ride status, including phase, ETA, vehicle, and arrival data
- List safe payment-method labels
- Get immediate or scheduled quotes with up to three intermediate stops
- Book a quoted ride
- Preview a cancellation and its fee
- Cancel the active ride

> [!WARNING]
> This project uses Waymo's private, reverse-engineered rider API. It is not
> affiliated with or endorsed by Waymo. The API can change without notice.
> Review Waymo's terms and use this only with your own account.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- A Waymo refresh token from your own signed-in account

The server runs locally over stdio. Your credentials are not sent to a hosted
third-party MCP service.

## 1. Configure credentials

The server checks, in order:

1. `WAYMO_REFRESH_TOKEN` and optional `WAYMO_DEVICE_ID`
2. `WAYMO_CREDENTIALS_FILE`
3. `~/.config/waymo/credentials.json`

The default credential file has this shape:

```json
{
  "refresh_token": "your-token",
  "device_id": "your-device-id"
}
```

Create it locally with file mode `0600`. Credential acquisition is outside the server itself; this fork ships
`scripts/capture_credentials.py`, a mitmproxy addon that captures your own
iOS app's refresh token and device id into a 0600 file. Never commit a capture, token, or credential file.

## 2. Add the MCP server

Add this to `~/.cursor/mcp.json` for all Cursor projects, or `.cursor/mcp.json`
inside one project:

```json
{
  "mcpServers": {
    "waymo": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/nathanielangafor/waymo-mcp",
        "waymo-mcp"
      ]
    }
  }
}
```

Restart or reload MCP servers in your client. Other MCP clients can use the
same stdio command and arguments.

You can give an agent this repository URL and say:

> Add this local stdio MCP server with uvx, then use its Waymo tools:
> https://github.com/nathanielangafor/waymo-mcp

## Safe agent behavior

- Resolve ambiguous places before quoting.
- Use result indices to select ambiguous pickup, stop, and dropoff matches.
- For a scheduled ride, quote once to get available times, then quote again
  with the chosen `scheduled_option_token`.
- Booking is two-step. `waymo_get_quote` returns a server-issued `quote_id`
  (the raw Waymo proposal token never leaves the server). Show the canonical
  pickup, ordered stops, dropoff, and fare, and only after the user approves
  that quote call `waymo_book_ride` with the same `quote_id`, its
  `expected_fare`, and `confirmation: "BOOK"`.
- Quote ids are single-use (consumed by any booking attempt, even a failed
  one) and expire after five minutes, so one call can never order a ride.
- Before cancellation, call `waymo_preview_cancellation` and show any fee.
- Only pass `confirmation: "CANCEL"` after the user explicitly confirms that
  preview.
- Never retry booking or cancellation after `OUTCOME_UNKNOWN`; check status.
- Do not print refresh, chauffeur, proposal, or internal payment tokens to the
  user.

The server reinforces these rules with MCP annotations, literal confirmation
arguments, and a five-minute, single-use in-memory quote cache. Client annotations are
hints, not an authorization boundary; only connect this server to agents you
trust.

## Development

```bash
git clone https://github.com/nathanielangafor/waymo-mcp
cd waymo-mcp
uv sync --extra dev
uv run pytest
uv run waymo-mcp
```

The implementation intentionally contains no HTTP server, database, browser
automation, or captured application files. See [SECURITY.md](SECURITY.md)
before reporting a vulnerability.
