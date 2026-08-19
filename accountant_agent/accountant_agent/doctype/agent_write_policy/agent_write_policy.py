# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Agent Write Policy — the customer's authoritative control over agent writes.

Everything here is enforced server-side inside the customer's own ERP, so it
holds even if the agent platform is compromised or misconfigured. The platform
keeps its own copy of these limits for fast failure, but this one wins.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class AgentWritePolicy(Document):
	def validate(self) -> None:
		self._validate_non_negative()
		self._validate_no_duplicate_document_types()
		self._warn_on_submit_without_approval()

	def _validate_non_negative(self) -> None:
		numeric_fields = (
			("max_documents_per_run", _("Max Documents Per Run")),
			("max_total_amount_per_run", _("Max Total Amount Per Run")),
			("posting_date_max_days_back", _("Posting Date - Max Days Back")),
			("posting_date_max_days_forward", _("Posting Date - Max Days Forward")),
		)
		for fieldname, label in numeric_fields:
			if (self.get(fieldname) or 0) < 0:
				frappe.throw(_("{0} cannot be negative.").format(label))

		for row in self.allowed_document_types or []:
			if (row.auto_submit_ceiling_amount or 0) < 0:
				frappe.throw(
					_("Auto-Submit Ceiling Amount cannot be negative (row {0}).").format(row.idx)
				)

	def _validate_no_duplicate_document_types(self) -> None:
		"""Two rows for one DocType would make the effective policy ambiguous."""
		seen: set[str] = set()
		for row in self.allowed_document_types or []:
			if row.document_type in seen:
				frappe.throw(
					_("{0} appears more than once in Allowed Document Types.").format(
						frappe.bold(row.document_type)
					)
				)
			seen.add(row.document_type)

	def _warn_on_submit_without_approval(self) -> None:
		"""Surface the segregation-of-duties consequence at the moment it is chosen.

		Allowing the agent to submit while waiving human approval means the ERP
		audit trail will record the agent as both preparer and approver. That is
		a legitimate choice for low-value routine postings, but it must be a
		deliberate one rather than a side effect of two unrelated checkboxes.
		"""
		if not self.enabled:
			return

		submitting = [
			row.document_type
			for row in self.allowed_document_types or []
			if row.allow_submit
		]
		if submitting and not self.require_approval:
			frappe.msgprint(
				_(
					"The agent may post {0} to the ledger without a human approval "
					"step. Your audit trail will show the agent account as both the "
					"preparer and the approver of these documents. Set an Auto-Submit "
					"Ceiling Amount, or re-enable Require Human Approval, if your "
					"controls require segregation of duties."
				).format(frappe.bold(", ".join(submitting))),
				title=_("Segregation of Duties"),
				indicator="orange",
			)
