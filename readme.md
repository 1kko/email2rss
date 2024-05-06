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



## Usage

1. Clone this repository.
2. Rum `make all` to build the docker container.
3. Copy `dotenv` to `.env` and change the values to your email account.
4. Run `make run` to start the container.
5. Browse to `http://localhost:8000` to see the generated RSS feeds.

## License

MIT