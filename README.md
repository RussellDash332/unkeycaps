# unkeycaps

Automatically plays the KEYCAPS™ Daily in Chrome using the highest-scoring
words from a SOWPODS word list, then formats and optionally sends a spoiler-tagged
result to Telegram.

## Setup

```sh
python -m pip install -r requirements.txt
```

Chrome must be installed. Selenium Manager obtains the matching ChromeDriver.
Each run downloads the word list from the SOWPODS URL in `main.py`.

## Run

```sh
python main.py
python main.py --headless
```

Chrome is visible by default; `--headless` runs without a window. Use
`--no-telegram` to prevent sending even when Telegram credentials are configured.
The scheduled workflow uses `--require-telegram` so it fails rather than
reporting success if Telegram credentials are missing or delivery fails.

To send the formatted result to one or more Telegram chats, set `TOKEN` and
`CHATS` in the process environment, or create an `env.py` file beside `main.py`
with those variables. Environment variables take precedence. Separate multiple
chat IDs with commas:

```py
# env.py (keep this file private; do not commit your bot token)
TOKEN = "your-bot-token"
CHATS = "123456789,-987654321"
```

```sh
# PowerShell
$env:TOKEN = "your-bot-token"
$env:CHATS = "123456789,-987654321"
python main.py --headless
```

```sh
# Bash / WSL
export TOKEN="your-bot-token"
export CHATS="123456789,-987654321"
python main.py --headless
```

The Telegram message uses MarkdownV2 spoilers around each word and appends
`#unkeycaps`. If `TOKEN` and `CHATS` are not set, the result is printed without
being sent.

The scheduled GitHub Actions workflow starts at 10:00 PM Singapore time and
waits until midnight before playing. If a delayed scheduled run starts after
midnight, it continues immediately rather than waiting another day.
