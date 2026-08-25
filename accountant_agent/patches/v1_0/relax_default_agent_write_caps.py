# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Open the blast-radius caps on sites installed before 0 meant unlimited.

``max_documents_per_run`` shipped as 100 and ``posting_date_max_days_back`` as
90. Both now ship as 0, which means unlimited. Editing the DocType default does
NOT touch a site that is already installed — its numbers live in a Singles row
written at install time — so without this patch every existing customer keeps
the old ceilings.

That matters more than it looks: ``assert_run_caps`` was never called by
anything until this release, so those two numbers have been decorative. Turning
enforcement on without this patch would make the visible effect of an upgrade
"the agent got stricter", which is the opposite of the intent.

ONLY ROWS STILL AT THE FACTORY VALUES ARE TOUCHED. 100 and 90 were our decision
and we are changing our decision. A number the customer typed themselves is a
control they own, and an update that silently widens it is a control failure
even when nothing bad follows from it. So a site sitting at 50 documents keeps
50, and a site that deliberately set 100 is indistinguishable from a site that
never looked — that ambiguity is accepted, and it errs toward the value the
product shipped with.

Runs post_model_sync because the DocType sync must have applied the new
defaults first. Idempotent — a second run finds 0 and does nothing.
"""

import frappe

#: field -> the value install.py wrote before this release. Anything else is
#: the customer's own setting and is left alone.
_FACTORY_VALUES: dict[str, int] = {
	"max_documents_per_run": 100,
	"posting_date_max_days_back": 90,
}


def execute() -> None:
	if not frappe.db.exists("DocType", "Agent Write Policy"):
		return

	# A site that has never opened the policy has no Singles row, and there is
	# nothing to relax — install.py writes the new defaults on a fresh install.
	if not frappe.db.exists("Singles", {"doctype": "Agent Write Policy"}):
		return

	policy = frappe.get_single("Agent Write Policy")
	relaxed: list[str] = []

	for fieldname, shipped_default in _FACTORY_VALUES.items():
		current = policy.get(fieldname)
		if current is None:
			continue
		try:
			current = int(current)
		except (TypeError, ValueError):
			continue
		if current != shipped_default:
			continue

		# db_set, not save(): this is a migration, and running the DocType's
		# own validate() here would surface a segregation-of-duties warning to
		# a customer who is not present and did not ask for anything.
		policy.db_set(fieldname, 0, update_modified=False)
		relaxed.append(fieldname)

	if not relaxed:
		return

	frappe.db.commit()
	frappe.logger("accountant_agent").info(
		"Agent Write Policy: relaxed %s to unlimited (was at the shipped default).",
		", ".join(relaxed),
	)
