# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Regression tests for the read-only query guard.

This endpoint executes SQL written by a language model whose context can
contain text an attacker put into a document the customer uploaded. The guard
is therefore a security boundary, and a security boundary without tests is a
claim rather than a control.

Both directions are asserted, because both have failed before:

  * Queries that MUST be refused — the credential and system tables a prompt
    injection would reach for.
  * Queries that MUST be allowed — including four ordinary accounting queries
    that an earlier keyword-only guard rejected because customer *data*
    contained words like "update" or "Drop", and the four schema-discovery
    queries the audit agent depends on (agent/agent_audit/audit_nodes.py).

A guard that only gets stricter eventually blocks the product. Keep both lists.
"""

from frappe.tests.utils import FrappeTestCase

from accountant_agent.agent_api.services.agent_api_service import (
	ForbiddenQueryError,
	assert_query_is_read_only,
)

#: Ordinary work the agents do. Every one of these must run.
PERMITTED_QUERIES: tuple[str, ...] = (
	"SELECT name, grand_total FROM `tabSales Invoice` WHERE docstatus = 1",
	"select account, sum(debit) from `tabGL Entry` group by account",
	"WITH monthly AS (SELECT MONTH(posting_date) m, SUM(debit) d FROM `tabGL Entry` "
	"GROUP BY 1) SELECT * FROM monthly",
	"SELECT posting_date, voucher_no FROM `tabGL Entry` ORDER BY posting_date DESC;",
	# Customer data that happens to contain SQL keywords. A keyword scan that
	# does not mask string literals refuses all four.
	"SELECT name FROM `tabItem` WHERE item_name LIKE '%Drop Shipping%'",
	"SELECT customer FROM `tabSales Invoice` WHERE remarks = 'update the PO'",
	"SELECT REPLACE(account_name, 'Ltd', 'Limited') FROM `tabAccount`",
	'SELECT name FROM `tabUser` WHERE full_name = "Create Holdings"',
	# The audit agent's schema discovery. Breaking these breaks the audit.
	"SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()",
	"SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
	"SELECT column_name FROM information_schema.columns WHERE table_name = 'tabGL Entry'",
	"SELECT country, default_currency FROM `tabCompany` LIMIT 1",
)

#: Each entry is (query, what it would achieve if it ran).
REFUSED_QUERIES: tuple[tuple[str, str], ...] = (
	("UPDATE `tabGL Entry` SET debit = 0", "silently rewrite the ledger"),
	("DELETE FROM `tabSales Invoice`", "destroy the invoice register"),
	("INSERT INTO `tabAccount` VALUES (1)", "create an unaudited account"),
	("DROP TABLE `tabGL Entry`", "destroy the general ledger"),
	("SELECT 1; DROP TABLE `tabGL Entry`", "stacked statement on a Postgres site"),
	("SELECT 1; SELECT 2", "stacked statement"),
	("SELECT * FROM __Auth", "read every stored password on the site"),
	("select doctype, password from `__Auth` where name='Administrator'", "read the admin password"),
	("SELECT api_key FROM `tabAgent Settings`", "read every tenant's agent key"),
	("SELECT name, api_secret FROM `tabUser`", "read every user's API secret"),
	("SELECT * FROM `tabOAuth Bearer Token`", "steal live OAuth sessions"),
	("SELECT * FROM mysql.user", "read database account grants"),
	("SELECT * FROM information_schema.user_privileges", "enumerate privileges"),
	("SELECT * FROM information_schema.processlist", "enumerate live sessions"),
	("SELECT LOAD_FILE('/etc/passwd')", "read the server filesystem"),
	("SELECT 1 INTO OUTFILE '/tmp/x'", "write to the server filesystem"),
	(
		"SELECT 1 FROM DUAL WHERE (SET SESSION SQL_SELECT_LIMIT = 100000)",
		"lift the row cap this request runs under",
	),
)


class TestQueryGuard(FrappeTestCase):
	def test_ordinary_accounting_queries_are_permitted(self) -> None:
		for query in PERMITTED_QUERIES:
			with self.subTest(query=query):
				assert_query_is_read_only(query)

	def test_dangerous_queries_are_refused(self) -> None:
		for query, consequence in REFUSED_QUERIES:
			with self.subTest(query=query):
				with self.assertRaises(ForbiddenQueryError, msg=f"would {consequence}"):
					assert_query_is_read_only(query)
