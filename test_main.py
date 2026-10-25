import logging
import os
import sys
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import Mock, patch
from selenium.common.exceptions import TimeoutException

from main import (
    MAX_ATTEMPTS,
    SINGAPORE_TIMEZONE,
    WORD_LIST_URL,
    DailyResult,
    Word,
    best_words_by_pair,
    button_label,
    current_pair,
    escape_markdown_v2,
    format_message,
    has_result_score,
    is_finished,
    load_word_list,
    midnight_wait_seconds,
    parse_result,
    play_daily_with_retries,
    telegram_credentials,
    enter_word,
    submit_candidate,
    send_telegram_with_retries,
    word_was_accepted,
)


class WordSelectionTests(unittest.TestCase):
    def test_midnight_wait_avoids_rewaiting_after_daily_boundary(self):
        before_window = datetime(2026, 10, 25, 19, 59, tzinfo=SINGAPORE_TIMEZONE)
        before_midnight = datetime(2026, 10, 25, 20, 0, tzinfo=SINGAPORE_TIMEZONE)
        after_midnight = datetime(2026, 10, 26, 0, 30, tzinfo=SINGAPORE_TIMEZONE)
        daytime = datetime(2026, 10, 26, 10, 0, tzinfo=SINGAPORE_TIMEZONE)

        self.assertEqual(midnight_wait_seconds(before_window), 0)
        self.assertEqual(midnight_wait_seconds(before_midnight), 4 * 60 * 60)
        self.assertEqual(midnight_wait_seconds(after_midnight), 0)
        self.assertEqual(midnight_wait_seconds(daytime), 0)

    def test_keeps_top_scoring_words_with_shorter_words_tiebroken_first(self):
        result = best_words_by_pair([
            "ABZ",
            "ABZZ",
            "ACZ",
            "ADZ",
            "AEZ",
            "ABY",
            "invalid1",
            "XY",
        ])

        self.assertEqual(
            result[("A", "Z")],
            [
                Word("ABZZ", 24),
                Word("ABZ", 14),
                Word("ACZ", 14),
                Word("ADZ", 13),
                Word("AEZ", 12),
            ],
        )

    def test_limits_candidates_per_pair(self):
        result = best_words_by_pair(["ABZ", "ACZ", "ADZ", "AEZ"], limit=2)

        self.assertEqual(len(result[("A", "Z")]), 2)

    def test_word_list_is_always_downloaded_from_configured_url(self):
        response = Mock()
        response.__enter__ = Mock(return_value=[b"ABZ\n"])
        response.__exit__ = Mock(return_value=False)
        with patch("main.urllib.request.urlopen", return_value=response) as urlopen:
            result = load_word_list()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, WORD_LIST_URL)
        self.assertIn(("A", "Z"), result)


class ShareMessageTests(unittest.TestCase):
    def test_requires_a_visible_final_score_before_result_parsing(self):
        self.assertFalse(has_result_score("KEYCAPS™ · Two letters. Find the words between."))
        self.assertTrue(has_result_score("KEYCAPS™ · Oct 25 · 148 of 105 pts · A+"))

    def test_parses_result_and_formats_spoiler_rows_and_hashtag(self):
        result = parse_result(
            "THE DAILY · OCT 25 152 of 105 pts A+ OVER THE BAR",
            [
                Word("RHOMBENPORPHYRY", 35),
                Word("COMPLEMENTIZER", 31),
                Word("BARMITZVAH", 29),
                Word("DOLICHOCEPHALIC", 30),
                Word("SPIDERWEB", 17),
            ],
        )

        self.assertEqual(result.date, "Oct 25")
        self.assertEqual(result.speed_bonus, 10)
        message = format_message(result)
        self.assertIn(r"KEYCAPS™ · Oct 25 · 152 of 105 pts · A\+ · OVER THE BAR", message)
        self.assertEqual(
            message.splitlines()[1:6],
            [
                r"||RHOMBENPORPHYRY \+35 pts||",
                r"||COMPLEMENTIZER \+31 pts||",
                r"||BARMITZVAH \+29 pts||",
                r"||DOLICHOCEPHALIC \+30 pts||",
                r"||SPIDERWEB \+17 pts||",
            ],
        )
        self.assertEqual(message.splitlines()[6], "SPEED ⚡️⚡️⚡️ \\+10 pts")
        self.assertTrue(message.endswith(r"\#unkeycaps"))

    def test_parses_grade_from_result_line_not_unrelated_page_text(self):
        result = parse_result(
            "C\nTHE DAILY · OCT 25 148 of 105 pts · A+ · OVER THE BAR",
            [],
        )

        self.assertEqual(result.grade, "A+")

    def test_parses_grade_from_keycaps_board_clear_summary(self):
        result = parse_result(
            "KEYCAPS™ · Two letters. Find the words between.\n"
            "THE DAILY · OCT 25\n"
            "GRADE GRADE\n"
            "BOARD CLEAR\n"
            "Grade A+ A+\n"
            "OVER THE BAR\n"
            "152 OF 105 PTS · 47 OVER",
            [],
        )

        self.assertEqual(result.grade, "A+")
        self.assertEqual((result.score, result.target, result.status), (152, 105, "OVER THE BAR"))

    def test_escapes_markdown_v2_reserved_characters(self):
        self.assertEqual(
            escape_markdown_v2("A+B.C"),
            r"A\+B\.C",
        )


class TelegramCredentialTests(unittest.TestCase):
    def test_loads_credentials_from_env_py(self):
        local_env = types.ModuleType("env")
        local_env.TOKEN = "local-token"
        local_env.CHATS = "123, -456"
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {"env": local_env}):
            self.assertEqual(telegram_credentials(), ("local-token", ["123", "-456"]))

    def test_environment_credentials_take_precedence(self):
        local_env = types.ModuleType("env")
        local_env.TOKEN = "file-token"
        local_env.CHATS = "file-chat"
        with (
            patch.dict(os.environ, {"TOKEN": "process-token", "CHATS": "7, 8"}, clear=True),
            patch.dict(sys.modules, {"env": local_env}),
        ):
            self.assertEqual(telegram_credentials(), ("process-token", ["7", "8"]))

    def test_required_credentials_fail_when_unset(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.dict(sys.modules, {"env": None}),
            self.assertRaisesRegex(RuntimeError, "Telegram is required"),
        ):
            telegram_credentials(required=True)


class TelegramSendTests(unittest.TestCase):
    def test_does_not_echo_message_or_chat_id_after_successful_send(self):
        from main import send_telegram

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        output = StringIO()
        with (
            patch("main.urllib.request.urlopen", return_value=response),
            redirect_stdout(output),
        ):
            send_telegram("token", "123", "Daily result")

        self.assertEqual(output.getvalue(), "")

    def test_telegram_network_errors_do_not_expose_token_or_chat_id(self):
        from main import send_telegram

        token = "secret-token"
        chat_id = "private-chat-id"
        network_error = urllib.error.URLError(
            "https://api.telegram.org/bot{}/sendMessage chat_id={}".format(
                token, chat_id
            )
        )
        with patch("main.urllib.request.urlopen", side_effect=network_error):
            with self.assertRaisesRegex(RuntimeError, "Telegram send failed") as raised:
                send_telegram(token, chat_id, "Daily result")

        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn(chat_id, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_telegram_retry_logs_do_not_expose_credentials(self):
        token = "secret-token"
        chat_id = "private-chat-id"
        output = StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger()
        logger.addHandler(handler)
        try:
            with (
                patch(
                    "main.send_telegram",
                    side_effect=RuntimeError("{} {}".format(token, chat_id)),
                ),
                patch("main.time_module.sleep"),
                self.assertRaisesRegex(RuntimeError, "failed after 6 attempts"),
            ):
                send_telegram_with_retries(token, chat_id, "Daily result")
        finally:
            logger.removeHandler(handler)

        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(chat_id, output.getvalue())

    def test_retries_telegram_send_until_success(self):
        with (
            patch("main.send_telegram", side_effect=[RuntimeError("temporary"), None]) as send,
            patch("main.time_module.sleep") as sleep,
        ):
            send_telegram_with_retries("token", "123", "Daily result")

        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once()

    def test_telegram_delivery_stops_after_initial_attempt_and_five_retries(self):
        with (
            patch("main.send_telegram", side_effect=RuntimeError("offline")) as send,
            patch("main.time_module.sleep") as sleep,
            self.assertRaisesRegex(RuntimeError, "Telegram delivery failed after 6 attempts"),
        ):
            send_telegram_with_retries("token", "123", "Daily result")

        self.assertEqual(send.call_count, MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, MAX_ATTEMPTS - 1)


class DailyRetryTests(unittest.TestCase):
    def test_restarts_game_after_failure_and_returns_completed_result(self):
        first_browser = Mock()
        second_browser = Mock()
        result = DailyResult("Oct 25", 152, 105, "C", "OVER THE BAR", [])
        with (
            patch("main.make_browser", side_effect=[first_browser, second_browser]),
            patch("main.play_daily", side_effect=[RuntimeError("temporary"), []]),
            patch(
                "main.accessible_text",
                return_value="KEYCAPS™ · Oct 25 · 152 of 105 pts · A+ · OVER THE BAR",
            ),
            patch("main.parse_result", return_value=result),
            patch("main.time_module.sleep") as sleep,
        ):
            self.assertIs(play_daily_with_retries(True, {}), result)

        first_browser.quit.assert_called_once_with()
        second_browser.quit.assert_called_once_with()
        sleep.assert_called_once()


class BrowserInputTests(unittest.TestCase):
    def test_detects_completed_board_clear_layout_as_acceptance(self):
        driver = Mock()
        page = (
            "THE DAILY · OCT 25\nGRADE GRADE\nBOARD CLEAR\n"
            "Grade A+ A+\nOVER THE BAR\nYOU 152 · 47 OVER"
        )
        with (
            patch("main.accessible_text", return_value=page),
            patch("main.current_pair", return_value=None),
        ):
            self.assertTrue(is_finished(driver))
            self.assertTrue(word_was_accepted(driver, ("S", "B")))

    def test_completed_final_word_is_not_retried_as_a_rejection(self):
        driver = Mock()
        candidate = Word("SPIDERWEB", 17)
        page = "BOARD CLEAR\nGrade A+ A+\nOVER THE BAR\nYOU 152 · 47 OVER"
        with (
            patch("main.enter_word") as enter,
            patch("main.WebDriverWait") as wait,
            patch("main.accessible_text", return_value=page),
            patch("main.current_pair", return_value=None),
            patch("main.clear_entry") as clear,
        ):
            wait.return_value.until.side_effect = TimeoutException()
            accepted = submit_candidate(driver, ("S", "B"), candidate)

        self.assertTrue(accepted)
        enter.assert_called_once_with(driver, candidate.text)
        clear.assert_not_called()

    def test_retries_a_word_once_after_acceptance_timeout(self):
        driver = Mock()
        candidate = Word("TEST", 4)
        with (
            patch("main.enter_word") as enter,
            patch("main.WebDriverWait") as wait,
            patch("main.word_was_accepted", return_value=False),
            patch("main.current_pair", return_value=("T", "T")),
            patch("main.live_score", return_value=0),
            patch("main.accessible_text", return_value=""),
            patch("main.clear_entry") as clear,
        ):
            wait.return_value.until.side_effect = [TimeoutException(), True]
            accepted = submit_candidate(driver, ("T", "T"), candidate)

        self.assertTrue(accepted)
        self.assertEqual(enter.call_count, 2)
        clear.assert_called_once_with(driver, len(candidate.text) - 2)

    def test_does_not_retry_word_after_late_acceptance(self):
        driver = Mock()
        candidate = Word("TEST", 4)
        with (
            patch("main.enter_word") as enter,
            patch("main.WebDriverWait") as wait,
            patch("main.word_was_accepted", return_value=True),
            patch("main.current_pair", return_value=("T", "T")),
            patch("main.live_score", return_value=0),
            patch("main.accessible_text", return_value=""),
            patch("main.clear_entry") as clear,
        ):
            wait.return_value.until.side_effect = TimeoutException()
            accepted = submit_candidate(driver, ("T", "T"), candidate)

        self.assertTrue(accepted)
        enter.assert_called_once_with(driver, candidate.text)
        clear.assert_not_called()

    def test_returns_rejected_after_two_unaccepted_attempts(self):
        driver = Mock()
        candidate = Word("TEST", 4)
        with (
            patch("main.enter_word") as enter,
            patch("main.WebDriverWait") as wait,
            patch("main.word_was_accepted", return_value=False),
            patch("main.current_pair", return_value=("T", "T")),
            patch("main.live_score", return_value=0),
            patch("main.accessible_text", return_value=""),
            patch("main.clear_entry") as clear,
        ):
            wait.return_value.until.side_effect = [TimeoutException(), TimeoutException()]
            accepted = submit_candidate(driver, ("T", "T"), candidate)

        self.assertFalse(accepted)
        self.assertEqual(enter.call_count, 2)
        self.assertEqual(clear.call_count, 2)

    def test_current_pair_uses_most_recently_revealed_pair(self):
        driver = Mock()
        driver.execute_script.return_value = ["ESC ESC", "RY", "? ?", "CR", "R 1"]

        self.assertEqual(current_pair(driver), ("C", "R"))

    def test_waits_through_missing_pair_during_transition(self):
        driver = Mock()
        with (
            patch("main.is_finished", return_value=False),
            patch("main.current_pair", return_value=None),
            patch("main.live_score", return_value=0),
        ):
            self.assertFalse(word_was_accepted(driver, ("R", "Y")))

    def test_detects_acceptance_after_pair_changes(self):
        driver = Mock()
        with (
            patch("main.is_finished", return_value=False),
            patch("main.current_pair", return_value=("C", "R")),
        ):
            self.assertTrue(word_was_accepted(driver, ("R", "Y")))

    def test_score_change_without_next_pair_is_not_acceptance(self):
        driver = Mock()
        with (
            patch("main.is_finished", return_value=False),
            patch("main.current_pair", return_value=("R", "Y")),
            patch("main.live_score", return_value=35),
        ):
            self.assertFalse(word_was_accepted(driver, ("R", "Y")))

    def test_button_label_collapses_duplicate_accessible_text(self):
        button = Mock()
        button.get_attribute.return_value = "THE DAILY THE DAILY"

        self.assertEqual(button_label(button), "THE DAILY")

    def test_button_label_preserves_non_duplicate_accessible_text(self):
        button = Mock()
        button.get_attribute.return_value = "Left shift, off SHIFT"

        self.assertEqual(button_label(button), "Left shift, off SHIFT")

    def test_types_only_interior_letters_separately_then_submits(self):
        driver = Mock()
        actions = Mock()
        actions.send_keys.return_value = actions
        with patch("main.ActionChains", return_value=actions) as action_chains:
            enter_word(driver, "CAT")

        action_chains.assert_called_once_with(driver)
        self.assertEqual(
            [call.args[0] for call in actions.send_keys.call_args_list],
            ["A", "\ue007"],
        )
        actions.perform.assert_called_once_with()

    def test_word_entry_skips_first_and_last_characters(self):
        driver = Mock()
        actions = Mock()
        actions.send_keys.return_value = actions
        with patch("main.ActionChains", return_value=actions) as action_chains:
            enter_word(driver, "ABCD")

        self.assertEqual(
            [call.args[0] for call in actions.send_keys.call_args_list],
            ["B", "C", "\ue007"],
        )
        action_chains.assert_called_once_with(driver)
        actions.perform.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
