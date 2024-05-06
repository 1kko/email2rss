# Email to RSS

This project fetches emails from a specified mailbox and generates an RSS feed for each sender.

## Features

- Connects to a Gmail account using IMAP
- Fetches emails from the last 10 days (configurable)
- Groups emails by sender
- Generates an RSS feed for each sender
- Saves each RSS feed to a file
- Handles errors and logs them

## Usage

1. Clone this repository.
2. Install the required Python packages using Poetry

    ```bash
    poetry install
    ```

3. Run the main script:

    ```bash
    poetry run main.py
    ```

## Configuration

You can configure the Gmail account to connect to and the number of days to fetch emails from by saving lines in `.env`:

```env
user_email = 'your@email.here'
app_password = 'your_password_here'
```

## License

MIT