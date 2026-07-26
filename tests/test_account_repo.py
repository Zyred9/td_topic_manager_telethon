import unittest
from unittest.mock import MagicMock, patch

from repositories import account_repo


class AccountStartupStatusTest(unittest.TestCase):
    def test_pending_login_states_are_reset_on_startup(self) -> None:
        connection_context = MagicMock()
        connection = connection_context.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value

        with patch.object(account_repo, "get_connection", return_value=connection_context):
            account_repo.reset_all_status_on_startup()

        cursor.execute.assert_called_once_with(
            "UPDATE t_account SET status=4 WHERE status IN (1,2,6)"
        )

    def test_mark_logged_in_reconnect_only_has_state_guard(self) -> None:
        connection_context = MagicMock()
        connection = connection_context.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        with patch.object(account_repo, "get_connection", return_value=connection_context):
            affected = account_repo.mark_logged_in(
                "+10000000009", 1, "first", "last", "user", reconnect_only=True,
            )

        sql, params = cursor.execute.call_args.args
        self.assertTrue(sql.endswith("WHERE phone=%s AND is_dead=0 AND status IN (3,5)"))
        self.assertEqual("+10000000009", params[-1])
        self.assertEqual(1, affected)

    def test_mark_logged_in_manual_path_clears_dead_fields_atomically(self) -> None:
        connection_context = MagicMock()
        connection = connection_context.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        with patch.object(account_repo, "get_connection", return_value=connection_context):
            affected = account_repo.mark_logged_in(
                "+10000000022", 1, "first", "last", "user",
            )

        sql, params = cursor.execute.call_args.args
        self.assertTrue(
            sql.endswith(
                "login_time=%s, is_dead=0, dead_reason=NULL, dead_time=NULL WHERE phone=%s"
            )
        )
        self.assertEqual("+10000000022", params[-1])
        self.assertEqual(1, affected)

    def test_conditional_status_update_has_alive_and_status_guards(self) -> None:
        connection_context = MagicMock()
        connection = connection_context.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        with patch.object(account_repo, "get_connection", return_value=connection_context):
            affected = account_repo.update_status_if_current(
                "+10000000027",
                5,
                (5, 3),
            )

        cursor.execute.assert_called_once_with(
            "UPDATE t_account SET status=%s WHERE phone=%s AND is_dead=0 "
            "AND status IN (%s,%s)",
            (5, "+10000000027", 3, 5),
        )
        self.assertEqual(1, affected)

    def test_mark_dead_expected_statuses_are_part_of_same_update(self) -> None:
        connection_context = MagicMock()
        connection = connection_context.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1

        with patch.object(account_repo, "get_connection", return_value=connection_context):
            affected = account_repo.mark_dead(
                "+10000000028",
                "TerminalError",
                "invalid",
                (5, 3),
            )

        sql, params = cursor.execute.call_args.args
        self.assertTrue(sql.endswith("WHERE phone=%s AND is_dead=0 AND status IN (%s,%s)"))
        self.assertEqual(("+10000000028", 3, 5), params[-3:])
        self.assertEqual(1, affected)


if __name__ == "__main__":
    unittest.main()
