# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Agent Write Log — the append-only record of every agent write attempt.

Tamper resistance is the point of this DocType. No role has ``delete``
permission (see the JSON), and ``validate`` refuses any edit to a row that has
already reached a terminal status.

The write protocol in ``agent_write_repository`` moves a row from IN_FLIGHT to
COMMITTED with ``db_set``, which deliberately bypasses controller validation.
That asymmetry is intentional: the service layer's own state machine may
advance a row, a human with a keyboard may not.
"""

import frappe
from frappe import _
from frappe.model.document import Document

TERMINAL_STATUSES = ("COMMITTED", "FAILED")


class AgentWriteLog(Document):
	def validate(self) -> None:
		"""Refuse edits to a row that has already reached a terminal status."""
		if self.is_new():
			return

		previous = self.get_doc_before_save()
		if previous is None:
			return

		if previous.status in TERMINAL_STATUSES:
			frappe.throw(
				_(
					"Agent Write Log entries cannot be modified once they are "
					"{0}. This record is part of the audit trail."
				).format(previous.status),
				title=_("Audit Record Is Immutable"),
			)

	def on_trash(self) -> None:
		"""Belt and braces: no role has delete permission, but say why anyway."""
		frappe.throw(
			_(
				"Agent Write Log entries cannot be deleted. They are the audit "
				"trail of everything the agent attempted in this system."
			),
			title=_("Audit Record Is Immutable"),
		)
