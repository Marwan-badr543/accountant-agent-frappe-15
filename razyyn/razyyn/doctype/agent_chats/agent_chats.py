# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AgentChats(Document):
	def after_delete(self):
		# Cascade delete all related messages in Agent Chat History with this session_id
		frappe.db.delete("Agent Chat History", {"session_id": self.session_id})
