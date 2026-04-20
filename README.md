# Email to RSS

This project fetches emails from a specified mailbox and generates an RSS feed for each sender.

## Features

- Connects to an Email account using IMAP
- Fetches emails from the last 10 days (configurable)
- Groups emails by sender
- Generates an RSS feed for each sender
- Saves each RSS feed to a file
- Handles errors and logs them
- **Internal RSS Reader** - Optional web-based reader to view full email content directly
- Serves RSS feeds via a Flask/gunicorn HTTP server
- **Optional OpenTelemetry** - Export traces, metrics, and logs to any OTLP collector (SigNoz, Grafana Tempo, etc.)



## Configuration

You can configure your email account to connect to and the number of days to fetch emails from by saving lines in `.env`:

```env
imap_server=imap.gmail.com_or_some_other_imap_server
userid=your@email.address
userpw=your_password_here
mailbox=your_mailbox_name_to_fetch_emails_from
port=8000
refresh_seconds=300
data_dir=data
max_item_per_feed=100
server_baseurl=http://localhost:8000
bind_address=0.0.0.0

# Internal RSS Reader (optional)
# Set to 'true' to enable internal article viewer
# When enabled, RSS links point to /article/{feed}/{guid} instead of external websites
enable_internal_reader=false
```

### Configuration Options

| Option | Description | Default |
|--------|-------------|---------|
| `imap_server` | IMAP server address | Required |
| `userid` | Email account username | Required |
| `userpw` | Email account password or app password | Required |
| `mailbox` | Mailbox/folder to fetch emails from | `INBOX` |
| `port` | HTTP server port | `8000` |
| `refresh_seconds` | Interval between email fetches | `300` |
| `data_dir` | Directory for database and RSS files | `data` |
| `max_item_per_feed` | Maximum items per RSS feed | `100` |
| `server_baseurl` | Base URL for RSS feed links | Optional |
| `bind_address` | Address the HTTP server binds to | `127.0.0.1` |
| `enable_internal_reader` | Enable internal article viewer | `false` |

## Internal RSS Reader

The internal RSS reader is an optional feature that allows you to read email content directly on your server instead of following links to external websites.

### How It Works

**Default Behavior (Internal Reader Disabled):**
- RSS feed links point to the sender's domain (e.g., `https://tailscale.com`)
- Clicking an item in your RSS reader opens the sender's website
- Best for newsletters with web versions

**Internal Reader Enabled:**
- RSS feed links point to your server: `/article/{feed}/{guid}`
- Clicking an item displays the full email HTML content in a clean, readable format
- No external requests needed - all content served from your database
- Ideal for email-only content or when you want to read everything in one place

### Enabling the Internal Reader

Set the environment variable in your `.env` file:

```env
enable_internal_reader=true
```

After enabling, regenerate your RSS feeds for the changes to take effect.

### Features

- **Server-side rendering** - Minimal resource usage on client and server
- **Responsive design** - Works on mobile and desktop
- **Dark mode support** - Automatically adapts to system preferences
- **Full HTML content** - Displays emails exactly as received
- **Lightweight** - ~2KB CSS, minimal JavaScript

### URL Structure

Articles are accessed via: `http://your-server:8000/article/{feed}/{guid}`

## Operational Endpoints

Two lightweight JSON endpoints ship out of the box for health checks and ad-hoc inspection:

- `GET /health` → `{"status": "ok"}` — suitable for uptime probes and Coolify/Kubernetes health checks
- `GET /stats` → total emails, sender count, full sender list

## OpenTelemetry (Optional)

The server and fetcher containers support exporting traces, metrics, and structured logs to any OTLP-compatible collector. This is **off by default** — set `OTEL_EXPORTER_OTLP_ENDPOINT` to enable.

### Enabling

Set these environment variables (via `.env`, `docker-compose.override.yaml`, or your platform's secret manager):

```env
# Required to enable (leave blank to disable OTel entirely)
OTEL_EXPORTER_OTLP_ENDPOINT=https://your-otel-collector.example.com

# Optional overrides (defaults shown)
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_PYTHON_LOG_CORRELATION=true
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=production,service.version=0.2.0
```

When `OTEL_EXPORTER_OTLP_ENDPOINT` is unset or empty, the entrypoint skips the `opentelemetry-instrument` wrapper entirely — no startup cost, no exporter threads, no accidental data shipping.

### What gets instrumented

Two service identities are reported:

- `email2rss-server` — Flask requests (auto), SQLAlchemy queries (auto), `/article`/`/stats`/feed-file spans
- `email2rss-fetcher` — IMAP fetch cycle spans, per-sender `emails.received` counter, fetch/generation duration histograms, cycle counters labeled `{status=success|error}`

Custom metrics:

| Name | Type | Labels | Source |
|------|------|--------|--------|
| `email2rss.fetch.duration` | histogram (s) | — | fetcher cycle |
| `email2rss.fetch.cycles` | counter | `status` | fetcher cycle |
| `email2rss.emails.received` | counter | `sender` | per persisted email |
| `email2rss.feed.generation.duration` | histogram (s) | — | generator cycle |
| `email2rss.feed.generation.cycles` | counter | `status` | generator cycle |
| `email2rss.feeds.generated` | counter | `status` | per sender feed |

### Collector compatibility

Any OTLP/HTTP collector works. Tested against [SigNoz](https://signoz.io/) self-hosted. For gRPC collectors set `OTEL_EXPORTER_OTLP_PROTOCOL=grpc` and point at a gRPC endpoint.

Example: `http://localhost:8000/article/hello_tailscale_com/4e939412d854ceb79b21f011d93e2ec7`

Where:
- `{feed}` = Sanitized sender email (e.g., `hello_tailscale_com`)
- `{guid}` = Unique article identifier (MD5 hash)

## (optional) SSL cert with lets encrypt

If you want to use your own domain and have a SSL certificate, you can use lets encrypt to get a free SSL certificate.

1. Install certbot on your server. The command to do this depends on your Linux distribution. For Ubuntu, you can use:
```bash
sudo apt-get update
sudo apt-get install software-properties-common
sudo add-apt-repository universe
sudo add-apt-repository ppa:certbot/certbot
sudo apt-get update
sudo apt-get install certbot python3-certbot-nginx
```

2. Run certbot to obtain the certificates. Replace your-domain.com with your actual domain:
```bash
sudo certbot --nginx -d your-domain.com
```

3. now add certfile and keyfile to the .env file

Final .env file should look like this:
```env
imap_server=imap.gmail.com_or_some_other_imap_server
userid=your@email.address
userpw=your_password_here
mailbox=your_mailbox_name_to_fetch_emails_from
port=8000
certfile="/etc/letsencrypt/live/your-domain.com/fullchain.pem"
keyfile="/etc/letsencrypt/live/your-domain.com/privkey.pem"
server_baseurl=https://your-domain.com
enable_internal_reader=false
```


## Usage

1. Clone this repository.
2. Run `make all` to build the docker container.
3. Copy `.env.oauth.example` to `.env` and configure your email account settings.
4. Run `make run` to start the container.
5. Browse to `http://localhost:8000` to see the generated RSS feeds.

### Accessing Your Feeds

- **RSS Feeds**: `http://localhost:8000/{sender_email}.xml`
  - Example: `http://localhost:8000/hello_tailscale_com.xml`
- **OPML Subscription File**: `http://localhost:8000/subscriptions.opml`
  - Import this into your RSS reader to subscribe to all feeds at once
- **Internal Reader** (when enabled): `http://localhost:8000/article/{feed}/{guid}`
  - Click article links in your RSS reader to view content directly

### Running Locally (without Docker)

```bash
# Install dependencies
poetry install

# Configure your .env file
cp .env.oauth.example .env
# Edit .env with your settings

# Run the application
poetry run python start.py
```

## Tips & Troubleshooting

### Exposing Your Server to the Internet

You might want to use Tailscale's `funnel` to serve your local server to the internet. This way you can access your RSS feeds from anywhere.

```bash
tailscale funnel 8000
```

### Raspberry Pi Installation Issues

If you face problems installing on Raspberry Pi, try:

```bash
PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring poetry install
```

### Switching Between Internal and External Reader Modes

When you change the `enable_internal_reader` setting:

1. Update your `.env` file
2. Restart the application (it will regenerate feeds automatically)
3. Existing RSS feeds will be updated with new links on the next refresh cycle
4. You may need to refresh your RSS reader to see the updated links

### Static Files Not Loading

If CSS/JS files aren't loading with the internal reader:

- Verify the `static/` directory exists and contains `reader.css` and `reader.js`
- Check Docker logs: `docker logs <container-name>`
- Ensure the `static/` directory is copied in your Dockerfile (already configured in `Dockerfile.serve`)

### Database Location

The SQLite database and RSS feeds are stored in the `data/` directory by default. When running in Docker, mount this directory as a volume to persist data between container restarts.

## License

MIT
