# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Searching the customer's OWN documents, not just the agent's write log.

WHAT CHANGED

    Lifecycle actions - post this, reverse that - could only ever reach a
    document the agent had created itself, because the only list it could read
    was its own write log. An accountant who said "post the Zuckerman invoice"
    about an invoice a human typed was told the agent had not recorded
    anything: true, and no use to them.

WHAT THESE TESTS PIN

    * Reading goes through frappe.get_list, so the customer's permissions
      decide. Never get_all, never ignore_permissions.
    * A DocType that cannot be searched is NAMED, with a reason. A silently
      dropped DocType reads as "you have no such document", which is the one
      answer that must never be guessed at.
    * Asking for drafts on a DocType with no docstatus concept returns nothing
      and says why, rather than offering an Item as something to submit.
    * The LIKE ladder widens exactly as the candidate search does, so
      "Zuckerman Security" finds "Zuckerman Security Ltd.".
    * Newest first ACROSS DocTypes, by posting date where there is one.
    * Only fields the DocType really has are ever queried.

No site is needed: every database call goes through one seam, patched here.

RUN THEM FROM THE BENCH ROOT, not from the app directory:

    cd ~/frappe/my-bench && ./env/bin/python -m unittest \\
        accountant_agent.agent_api.tests.test_document_search

`frappe._()` builds every refusal message, and it opens `logs/frappe.log`
relative to the current directory. From anywhere else the assertion tests error
out on a missing log file rather than on anything they were written to check.
"""

import unittest
from unittest import mock

import frappe

from accountant_agent.agent_api.services import agent_write_service as svc
from accountant_agent.agent_api.services.agent_write_service import (
    MissingParameterError,
    search_documents,
)


class _Field:
    def __init__(self, fieldname: str) -> None:
        self.fieldname = fieldname


class _Meta:
    """Just enough DocType meta for the field intersection under test."""

    def __init__(self, fieldnames, *, submittable=1, istable=0, issingle=0) -> None:
        self.fields = [_Field(f) for f in fieldnames]
        self.is_submittable = submittable
        self.istable = istable
        self.issingle = issingle


_INVOICE_FIELDS = ("posting_date", "company", "status", "customer", "customer_name",
                   "grand_total", "base_grand_total", "due_date", "title")

_JOURNAL_FIELDS = ("posting_date", "company", "total_debit", "user_remark", "cheque_no")


def _invoice(name, *, date="2026-08-01", total=1000.0, party="Zuckerman Security Ltd.",
             docstatus=1):
    return {"name": name, "docstatus": docstatus, "posting_date": date,
            "modified": f"{date} 10:00:00", "company": "Marwan Co",
            "customer": party, "customer_name": party, "grand_total": total,
            "base_grand_total": total, "status": "Unpaid", "title": party}


class _Bench:
    """One patched world: which DocTypes exist, their meta, and their rows."""

    def __init__(self, metas: dict, rows: dict) -> None:
        self.metas = metas
        self.rows = rows
        self.queries: list[dict] = []
        self.counts: list[str] = []

    def exists(self, doctype):
        return doctype in self.metas

    def meta(self, doctype):
        return self.metas[doctype]

    def count(self, doctype, filters, or_filters):
        self.counts.append(doctype)
        return len(self._rows_for(doctype, filters, or_filters))

    def read(self, doctype, filters, or_filters, fields, limit, order_by):
        self.queries.append({
            "doctype": doctype, "filters": filters, "or_filters": or_filters,
            "fields": fields, "limit": limit, "order_by": order_by,
        })
        rows = self._rows_for(doctype, filters, or_filters)
        return [{k: v for k, v in r.items() if k in fields} for r in rows][:limit]

    def _rows_for(self, doctype, filters, or_filters):
        rows = self.rows.get(doctype, [])
        if or_filters:
            pattern = str(or_filters[0][2]).strip("%").casefold()
            tokens = [t for t in pattern.split("%") if t]
            rows = [
                r for r in rows
                if all(
                    any(tok in str(r.get(f, "")).casefold() for f in
                        ("name", "customer_name", "customer", "title"))
                    for tok in tokens
                )
            ]
        for clause in filters:
            field, op, value = clause
            if op == "in":
                rows = [r for r in rows if r.get(field) in value]
            else:
                rows = [r for r in rows if r.get(field) == value]
        return rows

    def __enter__(self):
        self._patches = [
            mock.patch.object(svc, "doctype_exists", side_effect=self.exists),
            mock.patch.object(svc, "get_doctype_meta", side_effect=self.meta),
            mock.patch.object(svc, "read_permitted_documents", side_effect=self.read),
            mock.patch.object(svc, "count_link_candidates", side_effect=self.count),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


def _world(**overrides):
    metas = {
        "Sales Invoice": _Meta(_INVOICE_FIELDS),
        "Journal Entry": _Meta(_JOURNAL_FIELDS),
    }
    rows = {
        "Sales Invoice": [
            _invoice("ACC-SINV-2026-00007", date="2026-08-20"),
            _invoice("ACC-SINV-2026-00003", date="2026-07-02", party="Delta Supplies"),
        ],
        "Journal Entry": [
            {"name": "ACC-JV-2026-00011", "docstatus": 0, "posting_date": "2026-08-22",
             "modified": "2026-08-22 09:00:00", "company": "Marwan Co",
             "total_debit": 500.0},
        ],
    }
    metas.update(overrides.get("metas") or {})
    rows.update(overrides.get("rows") or {})
    return _Bench(metas, rows)


class TestWhatIsAsked(unittest.TestCase):
    """The request itself, before any row is read."""

    def test_naming_no_doctype_is_refused_rather_than_scanning_everything(self):
        with self.assertRaises(MissingParameterError):
            search_documents([])

    def test_blank_names_do_not_count_as_doctypes(self):
        with self.assertRaises(MissingParameterError):
            search_documents(["", "   "])

    def test_more_doctypes_than_the_ceiling_is_refused(self):
        with self.assertRaises(MissingParameterError):
            search_documents([f"Type {n}" for n in range(svc.MAX_SEARCH_DOCTYPES + 1)])

    def test_a_doctype_named_twice_is_searched_once(self):
        with _world() as bench:
            search_documents(["Sales Invoice", "Sales Invoice"])
        self.assertEqual(len(bench.queries), 1)


class TestPermissionsDecide(unittest.TestCase):
    """The customer's own permission configuration, not ours."""

    def test_the_search_reads_through_the_permission_filtered_seam(self):
        with _world() as bench:
            search_documents(["Sales Invoice"])
        self.assertTrue(bench.queries, "no query was issued at all")

    def test_a_doctype_the_agent_may_not_read_is_named_not_dropped(self):
        with _world() as bench:
            with mock.patch.object(
                svc, "read_permitted_documents", side_effect=frappe.PermissionError
            ):
                result = search_documents(["Sales Invoice"])
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["unavailable"],
                         [{"doctype": "Sales Invoice", "reason": "READ_NOT_PERMITTED"}])

    def test_one_refused_doctype_does_not_lose_the_others(self):
        calls = {"n": 0}

        def flaky(doctype, **kwargs):
            calls["n"] += 1
            if doctype == "Journal Entry":
                raise frappe.PermissionError
            return [_invoice("ACC-SINV-2026-00007")]

        with _world():
            with mock.patch.object(svc, "read_permitted_documents", side_effect=flaky):
                result = search_documents(["Sales Invoice", "Journal Entry"])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual([u["doctype"] for u in result["unavailable"]], ["Journal Entry"])

    def test_the_repository_seam_uses_get_list_not_get_all(self):
        """The one line that decides whether permissions apply at all."""
        import inspect

        from accountant_agent.agent_api.db import agent_write_repository as repo

        source = inspect.getsource(repo.read_permitted_documents)
        # The BODY, not the docstring - which mentions get_all deliberately, to
        # say why the neighbouring master-data lookup is allowed to use it.
        body = source.replace(repo.read_permitted_documents.__doc__ or "", "")
        self.assertIn("frappe.get_list", body)
        self.assertNotIn("get_all", body)
        self.assertNotIn("ignore_permissions", body)


class TestUnsearchableDoctypes(unittest.TestCase):
    """Everything that cannot be offered says so, with a reason."""

    def test_an_unknown_doctype_is_reported_not_queried(self):
        with _world() as bench:
            result = search_documents(["Sales Invoise"])
        self.assertEqual(bench.queries, [])
        self.assertEqual(result["unavailable"],
                         [{"doctype": "Sales Invoise", "reason": "UNKNOWN_DOCTYPE"}])

    def test_a_child_table_is_never_offered_as_a_document(self):
        metas = {"Sales Invoice Item": _Meta(("item_code",), istable=1)}
        with _world(metas=metas):
            result = search_documents(["Sales Invoice Item"])
        self.assertEqual(result["unavailable"],
                         [{"doctype": "Sales Invoice Item", "reason": "CHILD_TABLE"}])

    def test_a_single_doctype_is_never_offered_as_a_document(self):
        metas = {"Accounts Settings": _Meta(("acc_frozen_upto",), issingle=1)}
        with _world(metas=metas):
            result = search_documents(["Accounts Settings"])
        self.assertEqual(result["unavailable"],
                         [{"doctype": "Accounts Settings", "reason": "SINGLE_DOCTYPE"}])

    def test_asking_for_drafts_of_something_unsubmittable_says_why(self):
        metas = {"Item": _Meta(("item_code", "item_name"), submittable=0)}
        with _world(metas=metas):
            result = search_documents(["Item"], docstatus=[0])
        self.assertEqual(result["unavailable"],
                         [{"doctype": "Item", "reason": "NOT_SUBMITTABLE"}])

    def test_an_unsubmittable_doctype_is_searchable_when_no_state_was_asked_for(self):
        metas = {"Item": _Meta(("item_code", "item_name"), submittable=0)}
        rows = {"Item": [{"name": "SKU002", "docstatus": 0, "modified": "2026-08-01 09:00:00"}]}
        with _world(metas=metas, rows=rows):
            result = search_documents(["Item"])
        self.assertEqual(result["unavailable"], [])
        self.assertEqual(result["documents"][0]["docname"], "SKU002")


class TestFindingTheRightDocument(unittest.TestCase):

    def test_no_text_means_the_most_recent_documents(self):
        with _world() as bench:
            result = search_documents(["Sales Invoice"])
        self.assertIsNone(bench.queries[0]["or_filters"])
        self.assertEqual(len(result["documents"]), 2)

    def test_a_partial_party_name_still_finds_the_invoice(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="Zuckerman Security")
        self.assertEqual([d["docname"] for d in result["documents"]],
                         ["ACC-SINV-2026-00007"])

    def test_the_like_ladder_widens_past_a_separator_the_erp_never_stored(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="Zuckerman – Security")
        self.assertEqual([d["docname"] for d in result["documents"]],
                         ["ACC-SINV-2026-00007"])

    def test_a_document_named_outright_is_found_by_its_own_name(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="ACC-SINV-2026-00003")
        self.assertEqual([d["docname"] for d in result["documents"]],
                         ["ACC-SINV-2026-00003"])

    def test_nothing_matching_is_an_empty_list_not_an_error(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="Nobody At All Ltd")
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["unavailable"], [])

    def test_a_state_filter_reaches_the_query(self):
        with _world() as bench:
            search_documents(["Sales Invoice"], docstatus=[0])
        self.assertIn(["docstatus", "in", [0]], bench.queries[0]["filters"])

    def test_only_documents_in_the_asked_for_state_come_back(self):
        with _world():
            result = search_documents(["Sales Invoice", "Journal Entry"], docstatus=[0])
        self.assertEqual([d["docname"] for d in result["documents"]],
                         ["ACC-JV-2026-00011"])

    def test_a_company_filter_reaches_the_query_when_the_doctype_has_one(self):
        with _world() as bench:
            search_documents(["Sales Invoice"], company="Marwan Co")
        self.assertIn(["company", "=", "Marwan Co"], bench.queries[0]["filters"])

    def test_a_company_filter_is_dropped_for_a_doctype_that_has_no_company(self):
        metas = {"Item": _Meta(("item_code",), submittable=0)}
        with _world(metas=metas) as bench:
            search_documents(["Item"], company="Marwan Co")
        self.assertEqual(bench.queries[0]["filters"], [])


class TestWhatComesBack(unittest.TestCase):

    def test_newest_first_across_doctypes(self):
        with _world():
            result = search_documents(["Sales Invoice", "Journal Entry"])
        self.assertEqual([d["docname"] for d in result["documents"]],
                         ["ACC-JV-2026-00011", "ACC-SINV-2026-00007",
                          "ACC-SINV-2026-00003"])

    def test_every_row_carries_what_a_person_needs_to_recognise_it(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="Zuckerman")
        row = result["documents"][0]
        self.assertEqual(row["doctype"], "Sales Invoice")
        self.assertEqual(row["docname"], "ACC-SINV-2026-00007")
        self.assertEqual(row["docstatus"], 1)
        self.assertEqual(row["amount"], 1000.0)
        self.assertEqual(row["posting_date"], "2026-08-20")
        self.assertEqual(row["party"], "Zuckerman Security Ltd.")

    def test_a_document_with_no_headline_total_reports_no_amount(self):
        rows = {"Sales Invoice": [{"name": "ACC-SINV-2026-00009", "docstatus": 0,
                                   "posting_date": "2026-08-21",
                                   "modified": "2026-08-21 09:00:00"}]}
        with _world(rows=rows):
            result = search_documents(["Sales Invoice"])
        self.assertIsNone(result["documents"][0]["amount"])

    def test_the_journal_entry_total_is_read_from_its_own_field(self):
        with _world():
            result = search_documents(["Journal Entry"])
        self.assertEqual(result["documents"][0]["amount"], 500.0)

    def test_the_limit_is_honoured_across_the_merged_result(self):
        with _world():
            result = search_documents(["Sales Invoice", "Journal Entry"], limit=2)
        self.assertEqual(len(result["documents"]), 2)

    def test_a_caller_cannot_ask_for_more_than_the_ceiling(self):
        with _world() as bench:
            search_documents(["Sales Invoice"], limit=10_000)
        self.assertEqual(bench.queries[0]["limit"], svc.MAX_DOCUMENTS_OFFERED)


class TestTruncationIsNeverSilent(unittest.TestCase):
    """Ten of forty must say forty. See erp-candidate-search-false-truncation."""

    def _many(self, count=40):
        return {"Sales Invoice": [
            _invoice(f"ACC-SINV-2026-{n:05d}", date=f"2026-08-{(n % 28) + 1:02d}")
            for n in range(count)
        ]}

    def test_a_full_page_reports_how_many_there_really_are(self):
        with _world(rows=self._many()):
            result = search_documents(["Sales Invoice"])
        self.assertEqual(len(result["documents"]), svc.MAX_DOCUMENTS_OFFERED)
        self.assertEqual(result["total_matched"], 40)
        self.assertTrue(result["truncated"])

    def test_a_result_that_fits_is_not_reported_as_truncated(self):
        with _world():
            result = search_documents(["Sales Invoice"])
        self.assertEqual(result["total_matched"], 2)
        self.assertFalse(result["truncated"])

    def test_a_page_with_room_to_spare_costs_no_counting_query(self):
        """The page IS the whole result; counting it buys a number we have."""
        with _world() as bench:
            search_documents(["Sales Invoice"])
        self.assertEqual(bench.counts, [])

    def test_a_full_page_is_counted_once(self):
        with _world(rows=self._many()) as bench:
            search_documents(["Sales Invoice"])
        self.assertEqual(bench.counts, ["Sales Invoice"])

    def test_the_total_counts_every_doctype_that_matched(self):
        with _world(rows=self._many(12)):
            result = search_documents(["Sales Invoice", "Journal Entry"], limit=5)
        self.assertEqual(result["total_matched"], 13)
        self.assertTrue(result["truncated"])

    def test_nothing_found_is_not_truncated(self):
        with _world():
            result = search_documents(["Sales Invoice"], text="Nobody At All Ltd")
        self.assertEqual(result["total_matched"], 0)
        self.assertFalse(result["truncated"])


class TestOnlyRealFieldsAreQueried(unittest.TestCase):
    """Asking for a column a DocType has not got is a SQL error, not a blank."""

    def test_a_doctype_without_a_customer_never_asks_for_one(self):
        with _world() as bench:
            search_documents(["Journal Entry"])
        self.assertNotIn("customer", bench.queries[0]["fields"])
        self.assertIn("total_debit", bench.queries[0]["fields"])

    def test_the_ordering_never_names_a_date_field_that_is_missing(self):
        metas = {"Item": _Meta(("item_code",), submittable=0)}
        with _world(metas=metas) as bench:
            search_documents(["Item"])
        self.assertEqual(bench.queries[0]["order_by"], "modified desc")

    def test_the_ordering_prefers_the_posting_date_where_there_is_one(self):
        with _world() as bench:
            search_documents(["Sales Invoice"])
        self.assertEqual(bench.queries[0]["order_by"], "posting_date desc, modified desc")

    def test_a_doctype_without_a_title_never_searches_one(self):
        with _world() as bench:
            search_documents(["Journal Entry"], text="anything")
        searched = {clause[0] for clause in bench.queries[0]["or_filters"]}
        self.assertNotIn("title", searched)
        self.assertIn("cheque_no", searched)


if __name__ == "__main__":
    unittest.main()
