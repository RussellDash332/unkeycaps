import argparse
from datetime import datetime, time, timedelta
import json
import logging
import os
import re
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import List
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

WORD_LIST_URL = "https://raw.githubusercontent.com/jesstess/Scrabble/master/scrabble/sowpods.txt"
DAILY_URL = "https://keyca.ps/"
BOARD_SIZE = 5
WAIT_SECONDS = 3.5
START_WAIT_SECONDS = 20
SUBMISSION_WAIT_SECONDS = 1.3
WORD_ATTEMPTS = 2
MAX_ATTEMPTS = 6
RETRY_DELAY_SECONDS = 2.5
SINGAPORE_TIMEZONE = ZoneInfo("Asia/Singapore")

SCORES = {
    "A": 1, "B": 3, "C": 3, "D": 2, "E": 1, "F": 4, "G": 2,
    "H": 4, "I": 1, "J": 8, "K": 5, "L": 1, "M": 3, "N": 1,
    "O": 1, "P": 3, "Q": 10, "R": 1, "S": 1, "T": 1, "U": 1,
    "V": 4, "W": 4, "X": 8, "Y": 4, "Z": 10,
}

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

@dataclass(frozen=True)
class Word:
    text: str
    score: int

@dataclass(frozen=True)
class DailyResult:
    date: str
    score: int
    target: int
    grade: str
    status: str
    words: List[Word]

    @property
    def speed_bonus(self):
        return self.score - sum(word.score for word in self.words)

def word_score(word):
    return sum(SCORES[letter] for letter in word)

def best_words_by_pair(lines, limit=5):
    """Keep the highest-scoring, shortest-tied words for each start/end pair."""
    words_by_pair = defaultdict(list)
    for raw_word in lines:
        word = raw_word.strip().upper()
        if len(word) < 3 or not word.isalpha() or any(c not in SCORES for c in word):
            continue
        pair = (word[0], word[-1])
        words_by_pair[pair].append(Word(word, word_score(word)))

    return {
        pair: sorted(words, key=lambda item: (item.score, -len(item.text)), reverse=True)[:limit]
        for pair, words in words_by_pair.items()
    }

def load_word_list():
    request = urllib.request.Request(
        WORD_LIST_URL,
        headers={"User-Agent": "unkeycaps/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            lines = (line.decode("utf-8").strip() for line in response)
            return best_words_by_pair(lines)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError("Could not download the word list from {}.".format(WORD_LIST_URL)) from error

def make_browser(headless):
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-notifications")
    if headless:
        options.add_argument("--headless=new")
    browser = webdriver.Chrome(options=options)
    browser.execute_cdp_cmd(
        "Emulation.setTimezoneOverride",
        {"timezoneId": "Asia/Singapore"},
    )
    return browser

def midnight_wait_seconds(now):
    """Wait only for scheduled runs launched in the 10 PM pre-midnight window."""
    if now.hour < 22:
        return 0
    next_midnight = datetime.combine(
        now.date() + timedelta(days=1),
        time.min,
        tzinfo=SINGAPORE_TIMEZONE,
    )
    return max(0, (next_midnight - now).total_seconds())

def wait_until_singapore_midnight():
    now = datetime.now(SINGAPORE_TIMEZONE)
    wait_seconds = midnight_wait_seconds(now)
    if not wait_seconds:
        logging.info("Already past the pre-midnight window in Singapore; continuing now.")
        return

    deadline = now + timedelta(seconds=wait_seconds)
    logging.info("Waiting until midnight Singapore time.")
    while True:
        remaining = (deadline - datetime.now(SINGAPORE_TIMEZONE)).total_seconds()
        if remaining <= 0:
            break
        time_module.sleep(min(30, remaining))
    logging.info("Midnight Singapore time reached.")

def accessible_text(driver):
    return driver.execute_script("return document.body ? document.body.innerText : '';") or ""

def enable_accessibility(driver):
    # Flutter exposes the usable control tree only after its accessibility prompt.
    prompt_clicked = driver.execute_script(
        "const prompt = document.querySelector('flt-semantics-placeholder');"
        "if (!prompt) return false;"
        "prompt.click();"
        "return true;"
    )
    if prompt_clicked:
        logging.info("Clicked KEYCAPS accessibility prompt.")
    else:
        logging.info("KEYCAPS accessibility prompt was not present; checking controls.")
    WebDriverWait(driver, WAIT_SECONDS).until(
        lambda browser: browser.find_elements(By.CSS_SELECTOR, '[role="button"]')
    )
    logging.info("KEYCAPS accessibility controls are available.")

def button_label(element):
    return normalize_button_label(element.get_attribute("aria-label") or element.text or "")

def normalize_button_label(label):
    label = " ".join(label.split())
    duplicate = re.fullmatch(r"(.+?)\s+\1", label, re.IGNORECASE)
    return duplicate.group(1) if duplicate else label

def find_button(driver, phrase):
    phrase = phrase.casefold()
    candidates = []
    for element in driver.find_elements(By.CSS_SELECTOR, '[role="button"], button'):
        if not element.is_displayed() or element.get_attribute("disabled"):
            continue
        label = button_label(element)
        if phrase in label.casefold():
            candidates.append((len(label), element))
    if not candidates:
        raise RuntimeError(
            "Could not find the KEYCAPS control {!r}. Visible text: {}".format(
                phrase, accessible_text(driver)[:500]
            )
        )
    return min(candidates, key=lambda candidate: candidate[0])[1]

def click_button(driver, phrase):
    button = find_button(driver, phrase)
    label = button_label(button) or phrase
    logging.info("Clicking KEYCAPS button: %s", label)
    try:
        button.click()
    except Exception:
        logging.exception("KEYCAPS button click failed: %s", label)
        raise
    logging.info("KEYCAPS button click completed: %s", label)

def current_pair(driver):
    revealed_pairs = []
    labels = driver.execute_script(
        "return [...document.querySelectorAll('[role=button],button')]"
        ".filter(element => element.getClientRects().length)"
        ".map(element => element.getAttribute('aria-label') || element.innerText || '')"
    )
    for raw_label in labels:
        label = normalize_button_label(raw_label).upper()
        match = re.fullmatch(r"([A-Z])\s*([A-Z])", label)
        if match:
            revealed_pairs.append((match.group(1), match.group(2)))
    return revealed_pairs[-1] if revealed_pairs else None

def live_score(driver):
    match = re.search(r"\b(\d+)\s+points?\b", accessible_text(driver), re.IGNORECASE)
    return int(match.group(1)) if match else None

def is_finished(driver):
    text = accessible_text(driver)
    return bool(
        has_result_score(text)
        or re.search(r"\bSHARE THE DAILY\b", text, re.IGNORECASE)
        or re.search(r"\bBOARD CLEAR\b", text, re.IGNORECASE)
        or re.search(r"\bYOU\s+\d+\s*[·•]\s*\d+\s+OVER\b", text, re.IGNORECASE)
    )

def has_result_score(text):
    return bool(re.search(r"\b\d+\s+of\s+\d+\s+pts\b", text, re.IGNORECASE))

def word_was_accepted(driver, pair):
    if is_finished(driver):
        return True
    active_pair = current_pair(driver)
    return active_pair is not None and active_pair != pair

def clear_entry(driver, entered_letter_count):
    logging.info("Clearing rejected word entry.")
    actions = ActionChains(driver)
    for _ in range(entered_letter_count):
        actions.send_keys(Keys.BACKSPACE)
    actions.perform()

def enter_word(driver, word):
    letters_to_type = word[1:-1]
    logging.info("Typing interior letters for %s: %s", word, letters_to_type)
    actions = ActionChains(driver)
    for letter in letters_to_type:
        actions.send_keys(letter)
    actions.send_keys(Keys.ENTER).perform()
    logging.info("Typed interior letters: %s", letters_to_type)
    logging.info("Submitted word: %s", word)

def submit_candidate(driver, pair, candidate):
    for attempt in range(1, WORD_ATTEMPTS + 1):
        if attempt > 1:
            logging.info(
                "Retrying %s for %s-%s (attempt %s of %s).",
                candidate.text,
                *pair,
                attempt,
                WORD_ATTEMPTS,
            )
        enter_word(driver, candidate.text)
        try:
            WebDriverWait(
                driver,
                SUBMISSION_WAIT_SECONDS,
                poll_frequency=0.1,
            ).until(lambda browser: word_was_accepted(browser, pair))
            return True
        except TimeoutException:
            logging.warning(
                "No acceptance signal after %s seconds for %s; pair=%s, score=%s, page=%r",
                SUBMISSION_WAIT_SECONDS,
                candidate.text,
                current_pair(driver),
                live_score(driver),
                accessible_text(driver)[-300:],
            )
            if word_was_accepted(driver, pair):
                logging.info("Detected late acceptance of %s.", candidate.text)
                return True
            clear_entry(driver, len(candidate.text) - 2)
    return False

def play_daily(driver, candidates_by_pair):
    driver.set_page_load_timeout(30)
    driver.get(DAILY_URL)
    enable_accessibility(driver)
    click_button(driver, "THE DAILY")

    WebDriverWait(driver, START_WAIT_SECONDS).until(
        lambda browser: current_pair(browser) is not None
    )
    rows = []
    for round_number in range(BOARD_SIZE):
        pair = current_pair(driver)
        if pair is None:
            break

        candidates = candidates_by_pair.get(pair, [])
        if not candidates:
            raise RuntimeError("The word list has no words for {}-{}.".format(*pair))

        accepted = None
        for candidate in candidates:
            logging.info("Trying %s (+%d pts) for %s-%s", candidate.text, candidate.score, *pair)
            if submit_candidate(driver, pair, candidate):
                accepted = candidate
                break

        if accepted is None:
            raise RuntimeError(
                "None of the top {} words were accepted for {}-{}: {}.".format(
                    len(candidates),
                    *pair,
                    ", ".join(candidate.text for candidate in candidates),
                )
            )

        rows.append(accepted)
        logging.info("Accepted %s (+%d pts)", accepted.text, accepted.score)
        if is_finished(driver):
            break
        if round_number < BOARD_SIZE - 1:
            WebDriverWait(driver, WAIT_SECONDS).until(
                lambda browser: current_pair(browser) is not None and current_pair(browser) != pair
            )

    if len(rows) != BOARD_SIZE:
        raise RuntimeError("The Daily ended after {} of {} pairs.".format(len(rows), BOARD_SIZE))

    try:
        WebDriverWait(driver, 15).until(
            lambda browser: has_result_score(accessible_text(browser))
        )
    except TimeoutException:
        # The result view may expose its score only after opening the share sheet.
        click_button(driver, "SHARE")
        try:
            WebDriverWait(driver, START_WAIT_SECONDS).until(
                lambda browser: has_result_score(accessible_text(browser))
            )
        except TimeoutException as error:
            raise RuntimeError(
                "The KEYCAPS game completed, but its result screen did not appear. "
                "Available page text: {}".format(accessible_text(driver)[-1200:])
            ) from error

    return rows

def play_daily_with_retries(headless, candidates):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        browser = None
        try:
            logging.info("Starting Daily attempt %s of %s.", attempt, MAX_ATTEMPTS)
            browser = make_browser(headless)
            words = play_daily(browser, candidates)
            WebDriverWait(browser, START_WAIT_SECONDS).until(
                lambda current_browser: has_result_score(
                    accessible_text(current_browser)
                )
            )
            return parse_result(accessible_text(browser), words)
        except Exception as error:
            last_error = error
            logging.exception("Daily attempt %s of %s failed.", attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                logging.info("Retrying the Daily in %s seconds.", RETRY_DELAY_SECONDS)
                time_module.sleep(RETRY_DELAY_SECONDS)
        finally:
            if browser is not None:
                browser.quit()

    raise RuntimeError(
        "The KEYCAPS Daily failed after {} attempts.".format(MAX_ATTEMPTS)
    ) from last_error

def send_telegram_with_retries(token, chat_id, message):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            send_telegram(token, chat_id, message)
            return
        except RuntimeError as error:
            last_error = error
            logging.exception(
                "Telegram delivery failed on attempt %s of %s.",
                attempt,
                MAX_ATTEMPTS,
            )
            if attempt < MAX_ATTEMPTS:
                logging.info("Retrying Telegram delivery in %s seconds.", RETRY_DELAY_SECONDS)
                time_module.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        "Telegram delivery failed after {} attempts.".format(
            MAX_ATTEMPTS
        )
    ) from last_error

def parse_result(text, words):
    date_match = re.search(
        r"THE DAILY\s*[·•]\s*([A-Z][a-z]{2}\s+\d{1,2}|[A-Z]{3}\s+\d{1,2})",
        text,
        re.IGNORECASE,
    )
    score_match = re.search(r"\b(\d+)\s+of\s+(\d+)\s+pts\b", text, re.IGNORECASE)
    grade_match = re.search(
        r"\bGrade\s+(A\+|A-|A|B\+|B-|B|C\+|C-|C|D|F)(?=\s|[·•]|$)",
        text,
        re.IGNORECASE,
    )
    if grade_match is None and score_match:
        grade_match = re.match(
            r"\s*[·•]?\s*(A\+|A-|A|B\+|B-|B|C\+|C-|C|D|F)(?=\s|[·•]|$)",
            text[score_match.end():],
            re.IGNORECASE,
        )
    status_match = re.search(
        r"\b(OVER THE BAR|ON THE BAR|UNDER THE BAR)\b",
        text,
        re.IGNORECASE,
    )
    missing = [
        name for name, match in (
            ("date", date_match),
            ("final score", score_match),
            ("grade", grade_match),
            ("bar result", status_match),
        ) if match is None
    ]
    if missing:
        raise RuntimeError(
            "Could not read {} from the KEYCAPS result screen. "
            "Available page text: {}".format(", ".join(missing), text[-1200:])
        )

    date = date_match.group(1).title()
    return DailyResult(
        date=date,
        score=int(score_match.group(1)),
        target=int(score_match.group(2)),
        grade=grade_match.group(1).upper(),
        status=status_match.group(1).upper(),
        words=words,
    )

def escape_markdown_v2(text):
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)

def format_message(result):
    heading = "KEYCAPS™ · {} · {} of {} pts · {} · {}".format(
        result.date, result.score, result.target, result.grade, result.status
    )
    lines = [escape_markdown_v2(heading)]
    for word in result.words:
        row = "{} +{} pts".format(word.text, word.score)
        lines.append("||{}||".format(escape_markdown_v2(row)))

    speed_bonus = result.speed_bonus
    lightning = " ⚡️⚡️⚡️" if speed_bonus > 0 else ""
    speed_points = "+{} pts".format(speed_bonus) if speed_bonus >= 0 else "{} pts".format(speed_bonus)
    lines.extend([
        escape_markdown_v2("SPEED{} {}".format(lightning, speed_points)),
        "",
        r"\#unkeycaps",
    ])
    return "\n".join(lines)

def telegram_credentials(required=False):
    try:
        token = os.environ["TOKEN"].strip()
        chats_value = os.environ["CHATS"]
    except KeyError:
        try:
            from env import CHATS, TOKEN
        except ModuleNotFoundError as error:
            if error.name != "env":
                raise
            if not os.environ.get("TOKEN") and not os.environ.get("CHATS"):
                if required:
                    raise RuntimeError(
                        "Telegram is required for this run, but TOKEN and CHATS are unset."
                    ) from error
                return None, []
            raise RuntimeError(
                "Set both TOKEN and CHATS, or define them in env.py."
            ) from error
        token = TOKEN.strip()
        chats_value = CHATS

    chats = [
        chat.strip()
        for chat in chats_value.split(",")
        if chat.strip()
    ]
    if not token or not chats:
        raise RuntimeError(
            "Set TOKEN and comma-separated CHATS in the environment or env.py."
        )
    return token, chats

def send_telegram(token, chat_id, message):
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/sendMessage".format(token),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError("Telegram send failed.") from error
    if not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected the message: {}".format(
                result.get("description", "unknown error")
            )
        )

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Play the KEYCAPS Daily and share the result.")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome without a visible window.",
    )
    visibility.add_argument(
        "--visible",
        action="store_true",
        help="Show the Chrome window (the default).",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Do not send the result, even when TOKEN and CHATS are set.",
    )
    parser.add_argument(
        "--require-telegram",
        action="store_true",
        help="Fail if Telegram credentials are unavailable or delivery fails.",
    )
    args = parser.parse_args()

    if args.no_telegram and args.require_telegram:
        parser.error("--no-telegram and --require-telegram cannot be used together.")
    token, chats = (
        (None, [])
        if args.no_telegram
        else telegram_credentials(required=args.require_telegram)
    )
    candidates = load_word_list()
    result = play_daily_with_retries(args.headless, candidates)
    message = format_message(result)
    print(message, flush=True)

    if token:
        errors = []
        for chat_id in chats:
            try:
                send_telegram_with_retries(token, chat_id, message)
                logging.info("Telegram delivery succeeded.")
            except RuntimeError as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("; ".join(errors))
    elif not args.no_telegram:
        logging.info("TOKEN and CHATS are unset; Telegram sending was skipped.")


if __name__ == "__main__":
    main()