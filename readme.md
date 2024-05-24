# Email to RSS

This project fetches emails from a specified mailbox and generates an RSS feed for each sender.

## Features

- Connects to a Email account using IMAP
- Fetches emails from the last 10 days (configurable)
- Groups emails by sender
- Generates an RSS feed for each sender
- Saves each RSS feed to a file
- Handles errors and logs them



## Configuration

You can configure your email account to connect to and the number of days to fetch emails from by saving lines in `.env`:

```env
imap_server=imap.gmail.com_or_some_other_imap_server
userid=your@email.address
userpw=your_password_here
mailbox=your_mailbox_name_to_fetch_emails_from
PORT=8000
```

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
PORT=8000
certfile="/etc/letsencrypt/live/your-domain.com/fullchain.pem"
keyfile="/etc/letsencrypt/live/your-domain.com/privkey.pem"
```


## Usage

1. Clone this repository.
2. Rum `make all` to build the docker container.
3. Copy `dotenv` to `.env` and change the values to your email account.
4. Run `make run` to start the container.
5. Browse to `http://localhost:8000` to see the generated RSS feeds.

## Tip
You might want to use Tailscale's `funnel` to serve your local server to the internet. This way you can access your RSS feeds from anywhere.

In case you face problem installing it in raspi,
try:
`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring poetry install`

## License

MIT
