# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""The write log must be able to record a document that has no amount.

WHAT HAPPENED, LIVE, ON "create new item called socialmedia service"

    HTTP 422 | (1048, "Column 'amount_written' cannot be null")

    `amount_written` is a Currency field, and Frappe renders Currency as
    `decimal(21,9) NOT NULL DEFAULT 0`. `commit_write_log` passed the value
    through verbatim, and `_document_amount` returns None for anything without
    a grand total or a total debit - which is every Item, Customer, Supplier,
    Cost Center and Warehouse the agent can be asked to set up.

    So the agent could propose any document type and could never save one that
    was not a transaction. The refusal arrived after the customer had approved,
    with the log row already reserved, and was then masked further up by an
    unrelated ValueError.

These tests need no site: `commit_write_log` only ever calls `db_set` on the
document it is handed, so a recording double is a complete test of the
contract that broke.
"""

import unittest

from accountant_agent.agent_api.db.agent_write_repository import commit_write_log


class _Log:
    """Stands in for the reserved Agent Write Log row."""

    def __init__(self):
        self.written = None

    def db_set(self, values):
        self.written = dict(values)


class TestWriteLogColumns(unittest.TestCase):
    def test_a_document_with_no_amount_never_writes_null(self):
        log = _Log()
        commit_write_log(log, "socialmedia service", 0, None, {"ok": True})

        self.assertNotIn(
            "amount_written", log.written,
            "None into a NOT NULL Currency column is MariaDB error 1048",
        )
        self.assertEqual(log.written["status"], "COMMITTED")
        self.assertEqual(log.written["target_docname"], "socialmedia service")

    def test_an_amount_is_still_recorded_when_there_is_one(self):
        log = _Log()
        commit_write_log(log, "ACC-JV-2026-00009", 1, 15000.0, None)

        self.assertEqual(log.written["amount_written"], 15000.0)
        self.assertEqual(log.written["docstatus_written"], 1)

    def test_a_genuine_zero_is_not_mistaken_for_absence(self):
        """0.0 is falsy. Testing truthiness here would drop a real zero."""
        log = _Log()
        commit_write_log(log, "ACC-JV-2026-00010", 0, 0.0, None)

        self.assertIn("amount_written", log.written)
        self.assertEqual(log.written["amount_written"], 0.0)


if __name__ == "__main__":
    unittest.main()
