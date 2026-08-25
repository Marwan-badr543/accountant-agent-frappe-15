# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""The blast-radius caps, which until this release were never checked at all.

WHAT WAS WRONG

    `assert_run_caps` was defined and had no call site anywhere in the app.
    A customer could type 20 into "Max Documents Per Run", save it, read it
    back, and the agent would write 400. Two fields on a safety form that did
    nothing, which is worse than two fields with the wrong default.

WHAT THESE TESTS PIN

    * A cap of 0 means unlimited, and costs ZERO queries to not enforce.
    * "Per run" means the whole run, not the request in front of us.
    * A batch is counted once, before anything is written - never per row.
    * A REPLAY is never refused by a cap. It is already written; refusing it
      would strand the caller that retried precisely because it does not know.
    * An unpriced document counts as zero money, and does not raise TypeError.

No site is needed: every function under test either is pure or reaches the
database through one seam that is patched here.

RUN THEM FROM THE BENCH ROOT, not from the app directory:

    cd ~/frappe/my-bench && ./env/bin/python -m unittest \\
        accountant_agent.agent_api.tests.test_write_caps

`frappe._()` builds every refusal message, and it opens `logs/frappe.log`
relative to the current directory. From anywhere else the assertion tests error
out on a missing log file rather than on anything they were written to check.
"""

import unittest
from unittest import mock

from accountant_agent.agent_api.services import agent_write_service as svc
from accountant_agent.agent_api.services.agent_write_service import (
    PolicyCapExceededError,
    WritePolicy,
    _payload_amount,
    assert_run_caps,
    assert_run_caps_for_run,
)


def _policy(*, documents: int = 0, amount: float = 0.0) -> WritePolicy:
    """A policy that permits everything except what a test is measuring."""
    return WritePolicy(
        enabled=True,
        dry_run_only=False,
        require_approval=True,
        max_documents_per_run=documents,
        max_total_amount_per_run=amount,
        posting_date_max_days_back=0,
        posting_date_max_days_forward=0,
        allowed_document_types=(),
        allowed_companies=(),
        blocked_accounts=(),
    )


_JOURNAL = {
    "doctype": "Journal Entry",
    "accounts": [
        {"account": "Cash - M", "debit_in_account_currency": 15000, "credit_in_account_currency": 0},
        {"account": "Capital - M", "debit_in_account_currency": 0, "credit_in_account_currency": 15000},
    ],
}

_ITEM = {"doctype": "Item", "item_code": "socialmedia service", "item_group": "Services"}


class TestPayloadAmount(unittest.TestCase):
    """What a document is worth, BEFORE the ERP has computed its totals."""

    def test_a_document_with_no_money_is_zero_not_none(self):
        """THE regression `_document_amount` carries: None breaks sum().

        Summing None across a batch raises TypeError, which would turn a
        policy check into a crashed import - and an Item genuinely has no
        amount, so this is the ordinary case rather than an edge one.
        """
        self.assertEqual(_payload_amount(_ITEM), 0.0)
        self.assertIsInstance(_payload_amount(_ITEM), float)

    def test_an_empty_payload_is_zero(self):
        self.assertEqual(_payload_amount({}), 0.0)
        self.assertEqual(_payload_amount(None), 0.0)

    def test_a_journal_entry_is_worth_its_debit_side(self):
        self.assertEqual(_payload_amount(_JOURNAL), 15000.0)

    def test_debits_and_credits_are_not_added_together(self):
        """A balanced 15,000 entry is a 15,000 entry, not a 30,000 one."""
        self.assertNotEqual(_payload_amount(_JOURNAL), 30000.0)

    def test_a_headline_total_wins_when_the_erp_sent_one(self):
        payload = {"doctype": "Sales Invoice", "grand_total": 250,
                   "items": [{"amount": 100}, {"amount": 150}]}
        self.assertEqual(_payload_amount(payload), 250.0)

    def test_line_amounts_are_summed_when_there_is_no_headline(self):
        payload = {"doctype": "Sales Invoice",
                   "items": [{"amount": 100}, {"amount": 150}]}
        self.assertEqual(_payload_amount(payload), 250.0)

    def test_a_negative_amount_counts_toward_the_ceiling(self):
        """A cap is about size, and a reversal is as large as what it reverses."""
        self.assertEqual(_payload_amount({"doctype": "Journal Entry",
                                          "accounts": [{"debit": -500}]}), 500.0)

    def test_an_unparseable_amount_is_skipped_not_fatal(self):
        payload = {"doctype": "Journal Entry",
                   "accounts": [{"debit": "not a number"}, {"debit": 40}]}
        self.assertEqual(_payload_amount(payload), 40.0)


class TestRunCaps(unittest.TestCase):
    """The comparison itself: 0 is unlimited, anything else is a ceiling."""

    def test_zero_documents_means_unlimited(self):
        assert_run_caps(_policy(documents=0), 10_000, 0.0)

    def test_zero_amount_means_unlimited(self):
        assert_run_caps(_policy(amount=0), 1, 9_999_999.0)

    def test_a_document_ceiling_refuses_the_run_that_crosses_it(self):
        with self.assertRaises(PolicyCapExceededError):
            assert_run_caps(_policy(documents=20), 21, 0.0)

    def test_exactly_at_the_ceiling_is_allowed(self):
        assert_run_caps(_policy(documents=20), 20, 0.0)

    def test_an_amount_ceiling_refuses_the_run_that_crosses_it(self):
        with self.assertRaises(PolicyCapExceededError):
            assert_run_caps(_policy(amount=1000.0), 1, 1000.01)

    def test_the_refusal_names_the_limit_the_customer_set(self):
        with self.assertRaises(PolicyCapExceededError) as caught:
            assert_run_caps(_policy(documents=20), 21, 0.0)
        self.assertIn("20", str(caught.exception))


class TestRunCapsAcrossTheWholeRun(unittest.TestCase):
    """"Per run" has to mean the run, or it means nothing."""

    def test_unlimited_caps_never_touch_the_database(self):
        """The default is 0/0, so the common path must cost nothing.

        This is why the caps can default to unlimited without a performance
        argument: when nobody has set one, no query is issued at all.
        """
        with mock.patch.object(svc, "run_totals_so_far") as reader:
            assert_run_caps_for_run(_policy(), "run-1", 400, 5_000_000.0)
        reader.assert_not_called()

    def test_what_the_run_already_wrote_counts_toward_the_ceiling(self):
        """18 already written plus 3 more breaches a ceiling of 20."""
        with mock.patch.object(svc, "run_totals_so_far", return_value=(18, 0.0)):
            with self.assertRaises(PolicyCapExceededError):
                assert_run_caps_for_run(_policy(documents=20), "run-1", 3, 0.0)

    def test_what_the_run_already_spent_counts_toward_the_ceiling(self):
        with mock.patch.object(svc, "run_totals_so_far", return_value=(1, 900.0)):
            with self.assertRaises(PolicyCapExceededError):
                assert_run_caps_for_run(_policy(amount=1000.0), "run-1", 1, 200.0)

    def test_a_run_still_inside_its_ceiling_proceeds(self):
        with mock.patch.object(svc, "run_totals_so_far", return_value=(18, 0.0)):
            assert_run_caps_for_run(_policy(documents=20), "run-1", 2, 0.0)

    def test_the_history_is_read_exactly_once(self):
        """One query per request. Never one per document."""
        with mock.patch.object(svc, "run_totals_so_far", return_value=(0, 0.0)) as reader:
            assert_run_caps_for_run(_policy(documents=500), "run-1", 400, 0.0)
        self.assertEqual(reader.call_count, 1)

    def test_a_run_with_no_id_has_no_history_to_read(self):
        with mock.patch.object(svc, "run_totals_so_far", return_value=(0, 0.0)):
            assert_run_caps_for_run(_policy(documents=5), None, 1, 0.0)


class TestBatchCountsItselfOnce(unittest.TestCase):
    """A batch is measured before it starts, as one unit."""

    def _run_batch(self, documents, policy, created):
        with mock.patch.object(svc, "load_write_policy", return_value=policy), \
             mock.patch.object(svc, "assert_write_policy_enabled"), \
             mock.patch.object(svc, "assert_not_dry_run"), \
             mock.patch.object(svc, "run_totals_so_far", return_value=(0, 0.0)), \
             mock.patch.object(svc, "create_document", side_effect=created) as writer:
            result = svc.create_documents_batch(documents=documents, run_id="run-1")
        return result, writer

    def test_a_batch_over_the_ceiling_writes_nothing_at_all(self):
        """Not "writes up to the row that crosses it". Nothing.

        A half-applied import is the outcome a cap exists to prevent.
        """
        documents = [{"payload": dict(_JOURNAL), "idempotency_key": f"k{i}"}
                     for i in range(5)]
        with self.assertRaises(PolicyCapExceededError):
            self._run_batch(documents, _policy(documents=3), None)

    def test_the_batch_checks_the_caps_before_writing_a_single_row(self):
        documents = [{"payload": dict(_ITEM), "idempotency_key": f"k{i}"}
                     for i in range(5)]
        with mock.patch.object(svc, "load_write_policy", return_value=_policy(documents=3)), \
             mock.patch.object(svc, "assert_write_policy_enabled"), \
             mock.patch.object(svc, "assert_not_dry_run"), \
             mock.patch.object(svc, "run_totals_so_far", return_value=(0, 0.0)), \
             mock.patch.object(svc, "create_document") as writer:
            with self.assertRaises(PolicyCapExceededError):
                svc.create_documents_batch(documents=documents, run_id="run-1")
        writer.assert_not_called()

    def test_each_row_is_told_not_to_re_check_the_caps(self):
        """Otherwise a 400-row import issues 400 counting queries."""
        documents = [{"payload": dict(_ITEM), "idempotency_key": f"k{i}"}
                     for i in range(3)]
        outcome = {"outcome": "CREATED", "doctype": "Item", "docname": "x"}
        _, writer = self._run_batch(documents, _policy(documents=100),
                                    [dict(outcome) for _ in range(3)])
        self.assertEqual(writer.call_count, 3)
        for call in writer.call_args_list:
            self.assertFalse(call.kwargs["enforce_run_caps"])

    def test_an_unpriced_batch_does_not_crash_the_amount_sum(self):
        """Five Items have no amount between them. That is 0, not a TypeError."""
        documents = [{"payload": dict(_ITEM), "idempotency_key": f"k{i}"}
                     for i in range(5)]
        outcome = {"outcome": "CREATED", "doctype": "Item", "docname": "x"}
        result, _ = self._run_batch(documents, _policy(amount=100.0),
                                    [dict(outcome) for _ in range(5)])
        self.assertEqual(result["created"], 5)


class TestReplayIsNeverRefused(unittest.TestCase):
    """Idempotency outranks a cap: the document already exists."""

    def test_a_replay_past_the_ceiling_still_reports_what_happened(self):
        prior = {
            "status": "COMMITTED",
            "target_doctype": "Journal Entry",
            "target_docname": "ACC-JV-2026-00006",
            "docstatus_written": 0,
        }
        with mock.patch.object(svc, "load_write_policy", return_value=_policy(documents=1)), \
             mock.patch.object(svc, "assert_write_policy_enabled"), \
             mock.patch.object(svc, "assert_not_dry_run"), \
             mock.patch.object(svc, "assert_doctype_allowed"), \
             mock.patch.object(svc, "assert_within_policy_caps"), \
             mock.patch.object(svc, "find_write_log_by_key", return_value=prior), \
             mock.patch.object(svc, "run_totals_so_far", return_value=(99, 0.0)) as reader:
            result = svc.create_document(
                payload=dict(_JOURNAL), idempotency_key="k1", run_id="run-1",
            )

        self.assertEqual(result["outcome"], "REPLAYED")
        self.assertEqual(result["docname"], "ACC-JV-2026-00006")
        # The cap was never even consulted — the early return came first.
        reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
