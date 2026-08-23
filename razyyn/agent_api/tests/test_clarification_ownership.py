# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Regression test for the cross-session IDOR in the clarification/upload paths.

Before this fix, ``process_clarification_request`` and ``save_generated_file``
only checked that a ``session_id`` existed, not that it belonged to the caller
authenticated by the API key. Any customer's valid Agent Settings API key could
therefore inject clarification questions into, or attach a file to, any other
customer's chat session just by guessing/enumerating its session_id.
"""

from unittest import TestCase
from unittest.mock import patch

from razyyn.agent_api.services.agent_api_service import (
	ResourceNotFoundError,
	_assert_session_owned_by,
	process_clarification_request,
)


class TestSessionOwnershipGuard(TestCase):
	def test_owner_match_passes(self):
		with patch(
			"razyyn.agent_api.services.agent_api_service.get_chat_session_owner",
			return_value="alice@example.com",
		):
			_assert_session_owned_by("session-1", "alice@example.com")

	def test_missing_session_is_rejected(self):
		with patch(
			"razyyn.agent_api.services.agent_api_service.get_chat_session_owner",
			return_value=None,
		):
			with self.assertRaises(ResourceNotFoundError):
				_assert_session_owned_by("session-1", "alice@example.com")

	def test_other_customers_session_is_rejected(self):
		"""The IDOR this test guards against: a valid key for one customer must
		not be able to touch a session owned by a different customer."""
		with patch(
			"razyyn.agent_api.services.agent_api_service.get_chat_session_owner",
			return_value="bob@example.com",
		):
			with self.assertRaises(ResourceNotFoundError):
				_assert_session_owned_by("session-1", "alice@example.com")

	def test_process_clarification_request_rejects_foreign_session(self):
		with (
			patch(
				"razyyn.agent_api.services.agent_api_service.get_chat_session_owner",
				return_value="bob@example.com",
			),
			patch("razyyn.agent_api.services.agent_api_service.insert_chat_history_record") as insert_mock,
		):
			with self.assertRaises(ResourceNotFoundError):
				process_clarification_request("session-1", "[]", "alice@example.com")
			insert_mock.assert_not_called()
