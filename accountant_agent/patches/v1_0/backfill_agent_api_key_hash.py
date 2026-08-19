# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

"""Populate ``Agent Settings.api_key_hash`` on sites upgraded from before it existed.

Authentication moved from "load every settings record and decrypt each key
until one matches" to a single indexed read on the key's fingerprint. On a
freshly installed site the fingerprint is written by the controller; on an
upgraded site every existing row has a NULL one, and without this patch every
already-connected customer is locked out of their own agent on the migrate that
ships the change.

Runs post_model_sync because it writes to a column the DocType sync creates.
Idempotent — it only touches rows whose fingerprint is missing.
"""

import frappe

from accountant_agent.agent_api.db.agent_api_repository import backfill_api_key_hashes


def execute() -> None:
	if not frappe.db.exists("DocType", "Agent Settings"):
		return

	if not frappe.db.has_column("Agent Settings", "api_key_hash"):
		# The DocType sync should have created it. If it has not, failing loudly
		# here is wrong — it would abort the customer's whole migration — but
		# succeeding silently would leave authentication broken with no trace.
		frappe.log_error(
			title="Accountant Agent: api_key_hash column missing",
			message="Agent Settings has no api_key_hash column; agent API authentication will fail until `bench migrate` recreates it.",
		)
		return

	repaired = backfill_api_key_hashes()
	frappe.db.commit()

	frappe.logger("accountant_agent").info(
		"Backfilled api_key_hash on %s Agent Settings record(s).", repaired
	)
