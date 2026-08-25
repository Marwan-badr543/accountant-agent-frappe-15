# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""A create request may never post a document, whatever the payload says.

WHAT HAPPENED

    An accountant asked the agent to record a document. The plan it approved
    said the entry would be created as a DRAFT. The entry was posted to the
    general ledger. Their words: *"this also a fatal and critical bug, so he
    not have to submit any doc without user confirmation"*.

WHY THE GATEWAY IS WHERE THIS BELONGS

    `frappe.get_doc(payload).insert()` keeps whatever docstatus the payload
    carries. `set_docstatus()` defaults a MISSING one to 0; it does not force a
    supplied one back. So one `"docstatus": 1` anywhere in a create payload
    inserts a document that is already posted — with no `check_permission
    ("submit")`, no workflow, and, because `insert()` is not `submit()`, none
    of the DocType's `on_submit` work. For a Journal Entry that is a voucher
    the ledger shows as posted with no GL Entries behind it, which is a great
    deal worse than a refused write.

    There is no legitimate reason for a caller of this gateway to name that
    column, or `name`, or `owner`, or `creation`. Posting is
    `submit_existing_document`: permission-checked, logged, and reversible.

    A prompt is not where a rule like this belongs. The agent strips these too
    — that is the near end of the same rule — and this is the end that holds
    when the caller is something else, or is the same caller a version later.

No site is needed: the function under test is pure.

RUN THEM FROM THE BENCH ROOT, not from the app directory:

    cd ~/frappe/my-bench && ./env/bin/python -m unittest \
        accountant_agent.agent_api.tests.test_never_posts_itself
"""

import unittest

from accountant_agent.agent_api.services.agent_write_service import (
	_CALLER_MUST_NOT_SET,
	_only_the_caller_s_fields,
)


A_POSTED_ENTRY = {
	"doctype": "Journal Entry",
	"company": "marwan co",
	"posting_date": "2026-08-25",
	"docstatus": 1,
	"accounts": [
		{"account": "5211 - Print and Stationery - MC", "debit_in_account_currency": "500"},
		{"account": "1110 - Cash - MC", "credit_in_account_currency": "500"},
	],
}


class FrameworkColumnTests(unittest.TestCase):
	def test_a_supplied_docstatus_is_dropped(self):
		cleaned = _only_the_caller_s_fields(A_POSTED_ENTRY)
		self.assertNotIn("docstatus", cleaned)

	def test_the_document_itself_survives_intact(self):
		cleaned = _only_the_caller_s_fields(A_POSTED_ENTRY)
		self.assertEqual(cleaned["doctype"], "Journal Entry")
		self.assertEqual(cleaned["company"], "marwan co")
		self.assertEqual(len(cleaned["accounts"]), 2)
		self.assertEqual(
			cleaned["accounts"][0]["account"], "5211 - Print and Stationery - MC"
		)

	def test_a_child_row_is_scrubbed_too(self):
		"""A docstatus on a Journal Entry Account row is copied onto the row by
		the framework. One supplied by a caller is the same class of lie."""
		cleaned = _only_the_caller_s_fields({
			"doctype": "Journal Entry",
			"accounts": [{"account": "1110 - Cash - MC", "docstatus": 1, "idx": 7}],
		})
		self.assertNotIn("docstatus", cleaned["accounts"][0])
		self.assertNotIn("idx", cleaned["accounts"][0])
		self.assertEqual(cleaned["accounts"][0]["account"], "1110 - Cash - MC")

	def test_identity_and_audit_columns_go_as_well(self):
		"""A name, an owner or a creation timestamp supplied by a caller is a
		record that lies about itself."""
		cleaned = _only_the_caller_s_fields({
			"doctype": "Item",
			"item_code": "bmw",
			"name": "SOMETHING-ELSE",
			"owner": "Administrator",
			"creation": "2020-01-01",
			"modified_by": "Administrator",
			"amended_from": "ACC-JV-2026-00001",
		})
		self.assertEqual(cleaned, {"doctype": "Item", "item_code": "bmw"})

	def test_the_list_names_the_column_that_matters(self):
		self.assertIn("docstatus", _CALLER_MUST_NOT_SET)

	def test_a_payload_that_names_none_of_them_is_returned_unchanged(self):
		payload = {"doctype": "Item", "item_code": "bmw", "item_group": "Products"}
		self.assertEqual(_only_the_caller_s_fields(payload), payload)

	def test_a_non_dict_is_handed_straight_back(self):
		"""Called recursively over child rows, which are not always dicts."""
		self.assertEqual(_only_the_caller_s_fields("not a payload"), "not a payload")


class WriteServiceTests(unittest.TestCase):
	def test_every_write_path_scrubs_before_it_acts(self):
		"""Pinned on the source, because the ordering is the whole point: the
		digest, the policy caps and the insert must all see the SAME document,
		and a scrub after the digest would hash one and write another."""
		import inspect

		from accountant_agent.agent_api.services import agent_write_service

		for name in ("create_document", "preflight_document", "amend_existing_document"):
			source = inspect.getsource(getattr(agent_write_service, name))
			self.assertIn("_only_the_caller_s_fields", source, name)

		create = inspect.getsource(agent_write_service.create_document)
		self.assertLess(
			create.index("_only_the_caller_s_fields"),
			create.index("_payload_digest"),
			"the payload was hashed before it was scrubbed",
		)
