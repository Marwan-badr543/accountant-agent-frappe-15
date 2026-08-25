# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import hashlib
import json
import mimetypes
import os
import re
import uuid
from base64 import b64decode, b64encode
from html import escape, unescape
from typing import Optional

import requests
import frappe
from frappe import _
from frappe.model.document import Document

from accountant_agent.accountant_agent.doctype.agent_settings.agent_settings import (
	decode_jwt_payload,
	get_agent_server_url,
)

#: Messages of a conversation sent to the agent server with each turn.
#:
#: Every turn previously replayed the ENTIRE session. A long-running
#: reconciliation thread therefore grew a payload that was re-serialised,
#: re-transmitted and re-tokenised on every message, until the request was
#: megabytes of history to carry one sentence of question. project_rules.md §3
#: names this directly: never pass unbounded context to the model.
MAX_HISTORY_MESSAGES: int = 40

#: Messages returned to the browser when a chat is opened. The UI pages older
#: messages in on demand rather than materialising an unbounded conversation.
MAX_HISTORY_PAGE_SIZE: int = 200


# ---------------- Ownership Guards ----------------
#
# `agent_email` and `session_id` both reach these endpoints from the browser —
# `agent_email` out of localStorage (agent_chat.js), `session_id` as a plain
# form field. Neither is a credential, so neither may be treated as one. Every
# endpoint below resolves the record first and then proves the caller owns it.


def _assert_signed_in() -> str:
	"""The current ERP user, or a refusal if the caller is anonymous."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("Please sign in to ERPNext first."), frappe.AuthenticationError)
	return user


def get_agent_settings_doc(email: str) -> Optional[Document]:
	"""The CALLER'S OWN Agent Settings record for this email, or None.

	Ownership is the check, not existence. Without it, any signed-in ERP user —
	including a Website User with no accounting permission at all — could name a
	colleague's agent account and drive it: spend their metered quota, run
	queries against the ERP under their credentials, disconnect them, or delete
	their account outright.
	"""
	if not email:
		return None

	record = frappe.db.get_value(
		"Agent Settings", {"email": email}, ["name", "owner"], as_dict=True
	)
	if not record:
		return None
	if record.owner != frappe.session.user and frappe.session.user != "Administrator":
		return None

	return frappe.get_doc("Agent Settings", record.name)


def assert_agent_account_is_connectable(email: str) -> None:
	"""Refuse early, and legibly, when this agent account belongs to someone else.

	Without this the flow fails twice over with two unhelpful messages: sign-in
	reports "not found — please sign up", and the sign-up that follows is
	refused as a duplicate. Neither says what is actually true, which is that
	the account is already paired with a different ERPNext user.
	"""
	owner = frappe.db.get_value("Agent Settings", {"email": email}, "owner")
	if owner and owner != frappe.session.user and frappe.session.user != "Administrator":
		frappe.throw(
			_("The agent account {0} is already connected to a different ERPNext user. "
			  "Ask a System Manager to remove that connection first.").format(email),
			frappe.PermissionError,
		)


def assert_owns_session(session_id: str) -> None:
	"""Refuse unless the caller owns this chat session.

	A chat transcript holds uploaded bank statements, ERP query results and
	audit findings. `session_id` arrives from the client, so an endpoint that
	only checked that the session *exists* would hand any signed-in user any
	other user's financial conversation.
	"""
	if not session_id:
		frappe.throw(_("Session ID is required."), frappe.ValidationError)

	owner = frappe.db.get_value("Agent Chats", session_id, "owner")
	if not owner:
		frappe.throw(_("Chat session not found."), frappe.DoesNotExistError)
	if owner != frappe.session.user:
		frappe.throw(_("You are not authorised to access this chat."), frappe.PermissionError)


# ---------------- Server Communication Helpers ----------------

def register_agent_on_server(email: str, password: str, company_name: str, api_key: str) -> None:
	"""Sends a user registration POST request to the remote agent server."""
	payload = {
		"api_key": api_key,
		"name": company_name,
		"username": email,
		"password": password,
		"company_url": frappe.utils.get_url()
	}
	try:
		response = requests.post(f"{get_agent_server_url()}/users/", json=payload, timeout=15)
		if response.status_code != 201:
			error_msg = response.json().get("detail", "Registration failed.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Agent registration request error: {str(e)}", "Accountant Agent Auth")
		frappe.throw(_("Could not connect to Agent Server. Please make sure the server is running."))


def login_agent_on_server(email: str, password: str) -> str:
	"""Logs in to the agent server and returns the access token."""
	login_payload = {
		"username": email,
		"password": password
	}
	try:
		response = requests.post(f"{get_agent_server_url()}/auth/login", json=login_payload, timeout=15)
		if response.status_code != 200:
			error_msg = response.json().get("detail", "Login failed. Check your email and password.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
		
		token_data = response.json()
		return token_data.get("access_token")
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Agent login request error: {str(e)}", "Accountant Agent Auth")
		frappe.throw(_("Could not connect to Agent Server. Please make sure the server is running."))


def refresh_agent_token_on_server(access_token: str) -> str:
	"""Calls the agent server token refresh endpoint and returns the new access token."""
	refresh_payload = {
		"access_token": access_token
	}
	try:
		response = requests.post(f"{get_agent_server_url()}/auth/refresh", json=refresh_payload, timeout=15)
		if response.status_code == 200:
			return response.json().get("access_token")
	except Exception as e:
		frappe.log_error(f"Agent token refresh request error: {str(e)}", "Accountant Agent Refresh")
	return None


# ---------------- Database Connection Helpers ----------------

def save_agent_settings(
	email: str, api_key: Optional[str] = None, access_token: Optional[str] = None
) -> None:
	"""Create or update the caller's Agent Settings record.

	`api_key_hash` is maintained by AgentSettings.validate, so setting the key
	here is enough to keep the authentication index correct.
	"""
	doc = get_agent_settings_doc(email)

	try:
		if doc:
			if access_token is not None:
				doc.access_token = access_token
			if api_key is not None:
				doc.api_key = api_key
			doc.save(ignore_permissions=True)
		else:
			if not api_key:
				frappe.throw(_("An API key is required to create a new connection."))
			frappe.get_doc({
				"doctype": "Agent Settings",
				"email": email,
				"api_key": api_key,
				"access_token": access_token or "",
			}).insert(ignore_permissions=True)
		frappe.db.commit()
	except (frappe.ValidationError, frappe.DuplicateEntryError, frappe.PermissionError):
		# Refusals the customer can act on — an e-mail already connected to a
		# different ERPNext user, a missing key — must reach them intact. Only
		# unexpected failures are translated into a generic message below.
		#
		# DuplicateEntryError is listed explicitly because it subclasses
		# NameError, NOT ValidationError, in both Frappe 14 and 15
		# (frappe/exceptions.py). Catching ValidationError alone would drop the
		# one refusal this handler most needs to let through.
		raise
	except Exception as exc:
		frappe.db.rollback()
		frappe.log_error(
			title="Accountant Agent: save settings",
			message=f"Could not persist Agent Settings for {email}: {exc}",
		)
		frappe.throw(_("The connection could not be saved. Please try again."))


def deprecate_previous_plans(session_id: str) -> None:
	"""Mark every still-open plan in this session as superseded.

	Filtered in SQL rather than in Python: a plan is the only content that
	starts with that prefix, so fetching the whole transcript to discard almost
	all of it is work the database can skip entirely — and on a long session it
	is the difference between reading four rows and reading four thousand.
	"""
	try:
		plans = frappe.get_all(
			"Agent Chat History",
			filters={
				"session_id": session_id,
				"content": ("like", '{"type": "plan"%'),
			},
			fields=["name", "content"],
			order_by="creation desc",
			limit=50,
		)
		for msg in plans:
			if msg.content and msg.content.startswith('{"type": "plan"'):
				try:
					plan_data = json.loads(msg.content)
					if plan_data.get("status") in ("pending", "refused", "approved"):
						plan_data["status"] = "deprecated"
						frappe.db.set_value(
							"Agent Chat History",
							msg.name,
							"content",
							json.dumps(plan_data, ensure_ascii=False)
						)
				except Exception as json_err:
					frappe.log_error(f"Error deprecating plan message {msg.name}: {str(json_err)}", "Accountant Agent Deprecate Plan")
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"Error in deprecate_previous_plans: {str(e)}", "Accountant Agent Deprecate Plan")


def save_chat_history(session_id: str, sender: str, content: str) -> None:
	"""Saves a message in the Agent Chat History."""
	try:
		# If the new message is a plan, deprecate all previous plans in this session
		if content and content.strip().startswith('{"type": "plan"'):
			deprecate_previous_plans(session_id)

		doc = frappe.get_doc({
			"doctype": "Agent Chat History",
			"creation1": frappe.utils.now_datetime(),
			"session_id": session_id,
			"sender": sender,
			"content": content
		})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"Error saving user message to history: {str(e)}", "Accountant Agent Chat")


def save_chat_event_if_not_duplicate(session_id: str, sender: str, content: str) -> None:
	"""Saves an event/error message in the Agent Chat History, preventing duplicates."""
	try:
		latest_content = frappe.db.get_value(
			"Agent Chat History",
			{"session_id": session_id},
			"content",
			order_by="creation desc"
		)
		if latest_content == content:
			return
		save_chat_history(session_id, sender, content)
	except Exception as e:
		frappe.log_error(f"Error checking/saving chat event: {str(e)}", "Accountant Agent Chat Event")


def build_history_payload(session_id: str) -> str:
	"""The recent conversation, as the JSON transcript the agent server expects.

	Bounded to the most recent MAX_HISTORY_MESSAGES turns. The rows are fetched
	newest-first so the database applies the limit through the session index,
	then reversed, because the agent needs them oldest-first — fetching ascending
	and slicing in Python would read the whole conversation to discard the front
	of it.

	Never raises: a history that cannot be read must degrade the request to a
	context-free one, not fail the customer's message outright.
	"""
	if not session_id:
		return ""

	try:
		recent = frappe.get_all(
			"Agent Chat History",
			filters={"session_id": session_id},
			fields=["sender", "content"],
			order_by="creation desc",
			limit=MAX_HISTORY_MESSAGES,
		)
	except Exception as exc:
		frappe.log_error(
			title="Accountant Agent: history load",
			message=f"Could not load history for session {session_id}: {exc}",
		)
		return ""

	transcript = [
		{
			"role": "user" if row.sender == "human" else "assistant",
			"content": _prose_only(row.content),
		}
		for row in reversed(recent)
	]
	return json.dumps(
		[turn for turn in transcript if turn["content"]], ensure_ascii=False,
	)


#: What the chat page hides in a stored message so its own widgets survive a
#: reload: the base64 question payload, and the fold around it.
_CARRIED_MARKUP = re.compile(
	r'<span[^>]*class="agent-question-data"[^>]*>\s*</span>|</?(?:details|summary)[^>]*>',
	re.IGNORECASE,
)

#: The base64 question payload, wherever it rides — on the fold or on the
#: legacy span. Both shapes carry the identical JSON.
_QUESTION_PAYLOAD = re.compile(r'data-questions="([A-Za-z0-9+/=]+)"')


def _questions_only(content: str) -> str:
	"""The questions out of a stored question turn, and nothing else.

	Returns "" when this turn is not a question, so the caller falls through to
	the ordinary prose path.

	WHY THIS READS THE PAYLOAD RATHER THAN THE PROSE
		The stored turn is prose written for a person, and prose accretes. A
		question the agent had to ask twice carries an apology in front of it —
		*Sorry, I could not match "don't recodr , they are draft, just submit
		them" to a record in your system* — and behind it the standing guidance
		*tell me the name as it appears in your books*.

		Stripping the tags left every one of those lines in the transcript that
		is handed to the model. Four turns of it and the conversation reads as
		an agent whose job is to ask for names, so the model asks for a name:
		the live log shows the same question put three times to a customer who
		had answered it in their first message. *"next nodes can not know whawt
		the user say here"* is exactly that, and it is a signal-to-noise
		problem rather than a plumbing one.

		The payload is STRUCTURED. It holds the question text and nothing that
		was wrapped around it, so reading it cannot pick up prose that has not
		been written yet.

	THE OPTIONS ARE DROPPED HERE TOO, and they are dropped on purpose. The
	choice has been made by the time anybody reads this back; the roads not
	taken sit between the question and the answer making the exchange harder to
	read, and they spend a character budget that the turn carrying the amount
	needs.
	"""
	match = _QUESTION_PAYLOAD.search(content or "")
	if not match:
		return ""
	try:
		questions = json.loads(b64decode(match.group(1)).decode("utf-8"))
	except Exception:
		return ""
	if not isinstance(questions, list):
		return ""
	asked = [
		str(q.get("question") or "").strip()
		for q in questions
		if isinstance(q, dict) and str(q.get("question") or "").strip()
	]
	return "\n".join(asked)


def _prose_only(content: str) -> str:
	"""One stored turn, as the sentence the person actually read.

	THREE THINGS LIVE IN THIS COLUMN AND ONLY ONE OF THEM IS PROSE.
		A plain reply is prose already. A pause is stored as the JSON envelope
		the client needs in order to draw its card. A question carries a
		base64 payload of its questions in an invisible span, or on a fold.

	The agent is sent the transcript so it can remember what was agreed —
	*"it should send to llm in chat history, so it can get the context and
	avoide ask the user the same question twice"*. Handing it a JSON envelope
	teaches it to answer in JSON, and handing it several hundred characters of
	base64 spends a per-turn character budget on nothing at all: the turn that
	gets truncated to make room is the one where the customer said what the
	entry was for.

	A QUESTION TURN IS REDUCED TO THE QUESTION. See `_questions_only`.
	"""
	text = (content or "").strip()
	if not text:
		return ""

	asked = _questions_only(text)
	if asked:
		return asked

	if text.startswith("{"):
		try:
			payload = json.loads(text)
		except (ValueError, TypeError):
			payload = None
		if isinstance(payload, dict):
			spoken = (
				payload.get("plan") or payload.get("question")
				or payload.get("response") or ""
			)
			text = str(spoken).strip() or text
	# AFTER THE JSON, NEVER BEFORE IT. The chat page stores what a person typed
	# HTML-escaped, so `don't` is on disk as `don&#x27;t` and reached the model
	# looking like markup. Unescaping earlier would be a different bug: a
	# `&quot;` inside a stored envelope becomes a real quote and closes the JSON
	# string early, so the parse above fails and the customer's turn is handed
	# over raw.
	return unescape(_CARRIED_MARKUP.sub("", text).strip())


def post_message_to_agent(
	message: str,
	token: str,
	custom_instructions: str = None,
	session_id: str = None,
	agent_type: str = "auto",
	file_urls: list = None,
	history: list = None,
) -> requests.Response:
	"""Sends message to the agent server chat API, with optional attached files and agent_type."""
	headers = {
		"Authorization": f"Bearer {token}",
	}
	history_json = build_history_payload(session_id)

	payload_data = {
		"message": message,
		"history": history_json,
		"custom_instructions": custom_instructions or "",
		"session_id": session_id or "",
		"selected_agent": agent_type or "auto",
	}

	files_list = []
	opened_files = []

	try:
		if file_urls:
			for url in file_urls:
				# Resolved through the same owner-scoped helper the download
				# endpoint uses, so a session_id that names another user's
				# attachment forwards nothing rather than leaking it.
				file_path = resolve_agent_upload_path(url, frappe.session.user)
				if not file_path:
					continue

				handle = open(file_path, "rb")
				opened_files.append(handle)
				files_list.append(
					("files", (_original_filename(os.path.basename(file_path)), handle))
				)

		# Route request directly to chat endpoint
		endpoint_url = f"{get_agent_server_url()}/agent/chat"

		return requests.post(
			endpoint_url,
			data=payload_data,
			files=files_list if files_list else None,
			headers=headers,
			timeout=AGENT_STREAM_TIMEOUT,
		)
	finally:
		for f in opened_files:
			try:
				f.close()
			except Exception:
				pass


def update_chat_timestamp(session_id: str) -> None:
	"""Updates last_update timestamp of the chat session."""
	if session_id and frappe.db.exists("Agent Chats", session_id):
		frappe.db.set_value("Agent Chats", session_id, "last_update", frappe.utils.now_datetime())
		frappe.db.commit()


# ---------------- Whitelisted Page Methods ----------------

@frappe.whitelist()
def get_connection_status(agent_email=None):
	"""Checks if connection status settings are present for the given email."""
	user = frappe.session.user
	if user == "Guest" or not agent_email:
		return {"connected": False, "email": None}
	
	try:
		doc = get_agent_settings_doc(agent_email)
		if doc:
			token = doc.get_password("access_token", raise_exception=False)
			if token:
				return {"connected": True, "email": doc.email}
	except Exception as e:
		frappe.log_error(f"Error checking connection status: {str(e)}", "Accountant Agent Connect")
	
	return {"connected": False, "email": None}


@frappe.whitelist()
def authenticate_agent(mode, email, password, company_name=None):
	"""Handles login or signup requests against the agent server and updates local settings."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to ERPNext first."))
	
	if not email or not password:
		frappe.throw(_("Email and password are required."))

	assert_agent_account_is_connectable(email)
	
	if mode == "signup":
		if not company_name:
			frappe.throw(_("Company Name is required for registration."))
		
		# Generate new API key UUID
		api_key_uuid = str(uuid.uuid4())
		
		if get_agent_settings_doc(email):
			frappe.throw(_(f"Agent Settings record already exists for {email}."))
			
		# Store email & key local first
		save_agent_settings(email, api_key=api_key_uuid)
		
		# Create the user on server
		try:
			register_agent_on_server(email, password, company_name, api_key_uuid)
		except Exception as e:
			frappe.db.rollback()
			doc = get_agent_settings_doc(email)
			if doc:
				frappe.delete_doc("Agent Settings", doc.name, ignore_permissions=True)
				frappe.db.commit()
			raise e
		
		# Automatically login to acquire token
		access_token = login_agent_on_server(email, password)
		save_agent_settings(email, access_token=access_token)
		
	elif mode == "login":
		# Authenticate with agent server
		access_token = login_agent_on_server(email, password)
		
		# Check if local record exists (must exist as requested by user)
		if not get_agent_settings_doc(email):
			frappe.throw(_("Agent settings not found for this email. Please sign up first."))
			
		save_agent_settings(email, access_token=access_token)
		
	else:
		frappe.throw(_("Invalid mode specified."))

	# Retrieve the API key to return to client
	doc = get_agent_settings_doc(email)
	return {
		"success": True,
		"email": email,
		"api_key": doc.get_password("api_key")
	}


def get_latest_plan_message(session_id: str, lock: bool = False):
	"""Find the latest plan message in Agent Chat History for the given session."""
	messages = frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["name", "content"],
		order_by="creation desc",
		limit=20
	)
	for msg in messages:
		if msg.content and msg.content.startswith('{"type": "plan"'):
			if lock:
				try:
					# Apply row-level lock using FOR UPDATE with appropriate error handling
					frappe.db.sql(
						"select name from `tabAgent Chat History` where name=%s for update",
						msg.name
					)
					# Return fresh doc after lock is acquired
					return frappe.get_doc("Agent Chat History", msg.name)
				except Exception as e:
					frappe.log_error(f"Database lock timeout or error: {str(e)}", "Accountant Agent Plan Lock")
					frappe.throw(_("Could not acquire lock on the plan record. Please try again."))
			return msg
	return None


@frappe.whitelist()
def send_message(message, session_id, agent_email, agent_type="auto", file_urls=None):
	"""Proxy message send to agent by enqueuing a background worker to handle streaming."""
	user = _assert_signed_in()
	assert_owns_session(session_id)

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		frappe.throw(_("Not authenticated with Razyyn."))

	access_token = doc.get_password("access_token", raise_exception=False)
	if not access_token:
		frappe.throw(_("Missing access token. Please re-authenticate."))

	# Deserialize file_urls list if sent as JSON string
	parsed_file_urls = _parse_json_list(file_urls)

	# Update pending plan status if any
	latest_plan = get_latest_plan_message(session_id, lock=True)
	if latest_plan:
		try:
			plan_data = json.loads(latest_plan.content)
			if plan_data.get("status") == "pending":
				if message == "Approve":
					plan_data["status"] = "approved"
				else:
					plan_data["status"] = "refused"
				latest_plan.content = json.dumps(plan_data, ensure_ascii=False)
				latest_plan.save(ignore_permissions=True)
				frappe.db.commit()
		except Exception as e:
			frappe.log_error(f"Error updating plan status JSON: {str(e)}", "Accountant Agent Plan Status Update")

	# THE CUSTOMER'S OWN WORDS ALWAYS GO INTO THEIR TRANSCRIPT.
	#
	# An answer to a question used to be dropped entirely: the question was
	# shown, the customer answered it, and their reply appeared nowhere. From
	# their side the exchange had been deleted — *"it also deleted, so user
	# qustions should stored in history wiht user reply"* — and the agent's own
	# history readers could not see what had been agreed either.
	#
	# What is stored is the ANSWER, not the envelope: the reply arrives with the
	# agent's full question wrapped around it, and echoing that back at the
	# customer at full length is why it was being skipped in the first place.
	if message == "Approve":
		pass
	elif message.startswith("Clarification Response:"):
		said = _answer_text(message)
		if said:
			save_chat_history(session_id, "human", said)
			update_chat_timestamp(session_id)
	else:
		save_chat_history(session_id, "human", message)
		update_chat_timestamp(session_id)

	# Enqueue the task to background worker to avoid HTTP timeout
	frappe.enqueue(
		"accountant_agent.accountant_agent.page.agent_chat.agent_chat.process_agent_message_background",
		queue="long",
		timeout=AGENT_TASK_TIMEOUT_SECONDS,
		message=message,
		session_id=session_id,
		agent_email=agent_email,
		agent_type=agent_type,
		file_urls=parsed_file_urls,
		user=user,
	)

	return {"status": "queued", "session_id": session_id}


def process_agent_message_background(
	message: str,
	session_id: str,
	agent_email: str,
	agent_type: str,
	file_urls: list,
	user: str,
) -> None:
	"""Runs agent chat execution in a background worker task and streams progress to client."""
	frappe.set_user(user)

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		error_msg = f"Agent Settings not found for {agent_email}."
		frappe.log_error(error_msg, "Accountant Agent Stream")
		save_chat_event_if_not_duplicate(session_id, "ai", f"⚠️ **Error:** {error_msg}")
		update_chat_timestamp(session_id)
		frappe.publish_realtime(
			event="agent_message_error",
			message={"session_id": session_id, "error": error_msg},
			user=user,
		)
		return

	access_token = doc.get_password("access_token")
	if not access_token:
		error_msg = "Access token missing. Please reconnect."
		save_chat_event_if_not_duplicate(session_id, "ai", f"⚠️ **Error:** {error_msg}")
		update_chat_timestamp(session_id)
		frappe.publish_realtime(
			event="agent_message_error",
			message={"session_id": session_id, "error": error_msg},
			user=user,
		)
		return

	custom_instructions = getattr(doc, "custom_instructions", None) or ""

	headers = {
		"Authorization": f"Bearer {access_token}",
	}
	history_json = build_history_payload(session_id)

	payload_data = {
		"message": message,
		"history": history_json,
		"custom_instructions": custom_instructions,
		"session_id": session_id or "",
		"erp_system": "ERPNext",
		"stream": "true",
		"selected_agent": agent_type or "auto",
	}

	files_list = []
	opened_files = []

	try:
		if file_urls:
			for url in file_urls:
				# Resolved through the same owner-scoped helper the download
				# endpoint uses, so a session_id that names another user's
				# attachment forwards nothing rather than leaking it.
				file_path = resolve_agent_upload_path(url, frappe.session.user)
				if not file_path:
					continue

				handle = open(file_path, "rb")
				opened_files.append(handle)
				files_list.append(
					("files", (_original_filename(os.path.basename(file_path)), handle))
				)

		endpoint_url = f"{get_agent_server_url()}/agent/chat"

		response = requests.post(
			endpoint_url,
			data=payload_data,
			files=files_list if files_list else None,
			headers=headers,
			stream=True,
			timeout=AGENT_STREAM_TIMEOUT,
		)

		if response.status_code == 401:
			new_access_token = refresh_agent_token_on_server(access_token)
			if new_access_token:
				save_agent_settings(agent_email, access_token=new_access_token)
				headers["Authorization"] = f"Bearer {new_access_token}"
				
				# Re-open/reset files
				for f in opened_files:
					f.seek(0)
					
				response = requests.post(
					endpoint_url,
					data=payload_data,
					files=files_list if files_list else None,
					headers=headers,
					stream=True,
					timeout=AGENT_STREAM_TIMEOUT,
				)
			else:
				save_agent_settings(agent_email, access_token="")
				raise Exception("Session expired. Please reconnect.")

		if response.status_code == 499:
			save_chat_event_if_not_duplicate(session_id, "ai", "⚠️ **Cancelled**")
			update_chat_timestamp(session_id)
			frappe.publish_realtime(
				event="agent_message_cancelled",
				message={"session_id": session_id},
				user=user,
			)
			return

		if response.status_code != 200:
			try:
				err_detail = response.json().get("detail", "Error from Agent Server.")
			except Exception:
				err_detail = response.text or "Error from Agent Server."
			raise Exception(err_detail)

		current_event = None
		for line in response.iter_lines(chunk_size=1):
			if not line:
				continue
			line_str = line.decode("utf-8").strip()
			if line_str.startswith("event:"):
				current_event = line_str[6:].strip()
			elif line_str.startswith("data:"):
				data_str = line_str[5:].strip()
				try:
					data_json = json.loads(data_str)
				except Exception:
					data_json = {"text": data_str}

				if isinstance(data_json, dict) and "data" in data_json and isinstance(data_json["data"], dict):
					unwrapped = dict(data_json["data"])
					for k, v in data_json.items():
						if k != "data" and k not in unwrapped:
							unwrapped[k] = v
					data_json = unwrapped

				if current_event == "text":
					frappe.publish_realtime(
						event="agent_message_chunk",
						message={"session_id": session_id, "chunk": data_json.get("text", "")},
						user=user,
					)
				elif current_event == "reasoning":
					frappe.publish_realtime(
						event="agent_message_reasoning",
						message={"session_id": session_id, "chunk": data_json.get("text", "")},
						user=user,
					)
				elif current_event == "node_start":
					# `label` is what the client shows. It is written by the
					# agent in business language, because the alternative is a
					# lookup table in the browser that has to be kept in step
					# with every pipeline rename — and when it falls behind it
					# does not fail, it quietly captions every step
					# "Processing...". The node name still travels for logging.
					frappe.publish_realtime(
						event="agent_node_start",
						message={
							"session_id": session_id,
							"node": data_json.get("node", ""),
							"label": data_json.get("label", ""),
						},
						user=user,
					)
				elif current_event == "tool_start":
					frappe.publish_realtime(
						event="agent_tool_start",
						message={
							"session_id": session_id,
							"tool": data_json.get("tool", ""),
							"label": data_json.get("label", ""),
							"input": data_json.get("input", {}),
						},
						user=user,
					)
				elif current_event == "done":
					ai_response = data_json.get("response", "")

					# WHAT THE AGENT PAUSED FOR, AND WHAT THE PERSON READS, ARE
					# NOT THE SAME STRING.
					#
					# A paused run answers with a JSON envelope, because the
					# client needs the questions and their options as data. That
					# envelope was being saved to the transcript and published as
					# the assistant's message verbatim, so an accountant asking
					# about a laptop sale got a chat bubble opening
					# `{"type": "clarification", "questions": [{"id": ...` and
					# containing the question twice inside it.
					#
					# The envelope carries its own rendered prose. Show that,
					# store that, and keep the structured half for the picker.
					spoken, questions = _readable_response(ai_response)
					if questions:
						spoken = _collapsible_question(spoken, questions)

					save_chat_history(session_id, "ai", spoken)
					update_chat_timestamp(session_id)

					frappe.publish_realtime(
						event="agent_message_done",
						message={"session_id": session_id, "response": spoken},
						user=user,
					)

					# A pause that is a QUESTION opens the answer picker
					# directly, rather than relying on the transcript renderer
					# noticing the payload type. The agent is waiting on a
					# person; the options it offered should be in front of them
					# straight away.
					if questions:
						frappe.publish_realtime(
							event="agent_clarification_requested",
							message={"session_id": session_id, "questions": questions},
							user=user,
						)
				elif current_event == "error":
					raise Exception(data_json.get("detail", "Unknown error in stream"))

	except Exception as e:
		error_msg = str(e)
		save_chat_event_if_not_duplicate(session_id, "ai", f"⚠️ **Error:** {error_msg}")
		update_chat_timestamp(session_id)
		frappe.log_error(f"Error processing agent message in background: {error_msg}", "Accountant Agent Chat Background")
		frappe.publish_realtime(
			event="agent_message_error",
			message={"session_id": session_id, "error": error_msg},
			user=user,
		)
	finally:
		for f in opened_files:
			try:
				f.close()
			except Exception:
				pass


def _readable_response(ai_response: str) -> tuple:
	"""Split an agent reply into what a person reads and what a picker needs.

	Returns (the prose to show and store, the questions to open a picker for).

	WHY THIS EXISTS
		An agent that pauses answers with a JSON envelope — the client needs
		the questions and their options as structured data, and there is no
		way around that. But the envelope was going straight into the chat
		transcript as the assistant's message, so the customer saw:

			{"type": "clarification", "questions": [{"id": "essentials", ...

		...with the question text buried inside it, twice. `project_rules.md`
		§6 forbids exactly this, and it was landing on the one screen the
		product is judged by.

		Every envelope already carries its own rendered prose, written by the
		agent for this purpose. This returns that, and hands the structured
		half to the picker instead of to the renderer.

	ANYTHING UNRECOGNISED IS RETURNED UNTOUCHED. A normal reply is not
	JSON, and a future envelope shape this does not know about must still
	reach the customer rather than being swallowed by a parser.
	"""
	if not ai_response or not ai_response.lstrip().startswith("{"):
		return ai_response, []
	try:
		payload = json.loads(ai_response)
	except (ValueError, TypeError):
		return ai_response, []
	if not isinstance(payload, dict):
		return ai_response, []

	if payload.get("type") != "clarification":
		# A PLAN ENVELOPE IS STORED VERBATIM, AND MUST BE.
		#
		# `get_latest_plan_message` finds a pending approval by looking for a
		# stored message whose content literally starts with `{"type": "plan"`,
		# and `handle_agent_message` then rewrites its `status` field from
		# "pending" to "approved" or "refused". Replacing that message with its
		# own prose makes the plan unfindable, so the status never moves — and
		# the failure is silent, because the caller swallows the parse error.
		#
		# The chat window renders a plan as a card from the same JSON. Only the
		# clarification envelope was ever shown to a customer raw, and only it
		# is rewritten here.
		return ai_response, []

	questions = payload.get("questions")
	spoken = payload.get("question") or ""
	return (spoken or ai_response), (questions if isinstance(questions, list) else [])


def _collapsible_question(spoken: str, questions: list) -> str:
	"""The question as it belongs in a transcript: the question, and nothing else.

	THE REQUEST, IN FOUR PARTS, AND THE FOURTH ONE WAS ANGRY
		*"user qustions should stored in history wiht user reply"*, then
		*"questions to the user should not saved to the chat with its options,
		just save the question and the answer that the user choosed"*, then
		*"it should be collabsable so user can oben or close to save chat window
		space in ui"*, and then: *"i said before the question should saved with
		just user answer, but you saved the question with all option so next
		nodes can not know whawt the user say here, so its the third time i tell
		you that"*.

		The first three were implemented against `spoken`, which is the RENDERED
		CARD — the question with everything the agent wrapped around it. So the
		fold was right and its contents were not:

			Sorry — I could not match "don't recodr , they are draft, just
			submit them" to a record in your system, so I do not want to guess.
			Before I record this, let me check one thing with you.
			Tell me the name as it appears in your books and I will carry on ...

		Three lines of the agent's own scaffolding stored under every question,
		and handed back to the model on the next turn. NOTHING IS BUILT FROM
		`spoken` NOW. The questions are structured data and that is the only
		thing this reads.

	A SINGLE QUESTION IS NOT FOLDED, BECAUSE THERE IS NOTHING LEFT TO FOLD
		One line is already one line. A `<details>` around it would open onto an
		empty box, which is worse than no control at all. The fold appears when
		it earns its place: two or more questions, one summary line, the rest
		tucked away.

	THE PICKER MUST STILL REOPEN AFTER A RELOAD
		The transcript renderer reopens the answer picker when the last stored
		message is a question the agent is still waiting on, and it finds the
		questions by their `data-questions` attribute. So the structured
		questions ride along either way — on the fold when there is one, and on
		the invisible span when there is not. Base64 rather than escaped JSON on
		purpose: the payload then contains no character that HTML, Markdown or
		the no-Markdown fallback can react to, and Arabic question text survives
		it intact.
	"""
	if not spoken or not questions:
		return spoken

	asked = [
		str(question.get("question") or "").strip()
		for question in questions
		if isinstance(question, dict) and str(question.get("question") or "").strip()
	]
	if not asked:
		return spoken

	packed = b64encode(
		json.dumps(questions, ensure_ascii=False).encode("utf-8")
	).decode("ascii")

	if len(asked) == 1:
		return asked[0] + "\n\n" + _QUESTION_DATA.format(packed=packed)

	headline = _("{0} (and {1} more)").format(asked[0], len(asked) - 1)
	folded = "\n".join(
		f"{index}. {question}" for index, question in enumerate(asked[1:], 2)
	)

	# The blank line after </summary> is load-bearing: without it a Markdown
	# renderer treats the body as raw HTML and the content comes out as one
	# unformatted run-on line.
	return (
		f'<details class="agent-question" data-questions="{packed}">\n'
		f"<summary>{escape(headline)}</summary>\n\n"
		f"{folded}\n"
		"</details>"
	)


#: THE CARRIER FOR A QUESTION THAT IS NOT FOLDED. Invisible in the transcript,
#: and the only thing that lets the answer picker reopen after a reload. It
#: predates the fold and was very nearly deleted with it; a lone question has
#: nothing to fold, so it is the right shape again rather than a legacy one.
_QUESTION_DATA = '<span class="agent-question-data" data-questions="{packed}"></span>'


def _answer_text(message: str) -> str:
	"""What the customer actually answered, out of the envelope around it.

	A reply to a question arrives as the agent's own question with the answer
	appended to it:

		Clarification Response:
		* **Got it, we can record the laptop sale as revenue. Two ways ...**: sale invoice

	That whole blob was never stored, so the transcript showed a question and
	then nothing — the customer's own words disappeared from their chat, and
	so did the fact that they had answered at all. Storing the blob verbatim
	would be worse: it repeats the question back at them at full length.

	Returns "" when there is no answer to show, and the caller stores nothing.
	"""
	answers = []
	for line in (message or "").splitlines():
		line = line.strip()
		if not line.startswith("*"):
			continue
		# "* **<question>**: <answer>" — the answer is after the last "**:".
		marker = "**:"
		if marker in line:
			said = line.rsplit(marker, 1)[1].strip()
		else:
			said = line.lstrip("*").strip()
		if said:
			answers.append(said)

	if not answers:
		return ""
	return "\n".join(answers)





@frappe.whitelist()
def cancel_agent(session_id, agent_email):
	"""Proxy cancellation request to the agent server."""
	_assert_signed_in()
	assert_owns_session(session_id)

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		frappe.throw(_("Not authenticated with Razyyn."))

	access_token = doc.get_password("access_token", raise_exception=False)

	if not access_token:
		frappe.throw(_("Missing access token. Please re-authenticate."))

	headers = {
		"Authorization": f"Bearer {access_token}",
		"Content-Type": "application/json"
	}
	payload = {
		"session_id": session_id
	}
	try:
		response = requests.post(f"{get_agent_server_url()}/agent/cancel", json=payload, headers=headers, timeout=15)
		
		# Handle expired token (401)
		if response.status_code == 401:
			new_access_token = refresh_agent_token_on_server(access_token)
			if new_access_token:
				save_agent_settings(agent_email, access_token=new_access_token)
				headers["Authorization"] = f"Bearer {new_access_token}"
				response = requests.post(f"{get_agent_server_url()}/agent/cancel", json=payload, headers=headers, timeout=15)
			else:
				# Clear invalid token to force re-login
				save_agent_settings(agent_email, access_token="")
				frappe.throw(_("Session expired. Please reconnect."))
				
		if response.status_code != 200:
			error_msg = response.json().get("detail", "Error from Agent Server.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
			
		save_chat_event_if_not_duplicate(session_id, "ai", "⚠️ **Cancelled**")
		update_chat_timestamp(session_id)
		return {"success": True, "message": response.json().get("message")}
		
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Agent cancel request exception: {str(e)}", "Accountant Agent Cancel")
		frappe.throw(_("Unable to communicate with Agent Server. Please check if it's running."))


@frappe.whitelist()
def get_chat_history(session_id: str, limit: Optional[int] = None) -> list[dict]:
	"""The caller's own transcript for one session, most recent page first.

	Bounded: a year-old reconciliation thread is not something to serialise in
	full into a single HTTP response.
	"""
	assert_owns_session(session_id)

	page_size = min(int(limit or MAX_HISTORY_PAGE_SIZE), MAX_HISTORY_PAGE_SIZE)

	recent = frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["name", "sender", "content", "creation1", "creation"],
		order_by="creation desc",
		limit=page_size,
	)
	return list(reversed(recent))


@frappe.whitelist()
def disconnect_agent(agent_email):
	"""Disconnects the agent for the given email by clearing the access token locally."""
	if not agent_email:
		return {"success": False}
		
	_assert_signed_in()

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		return {"success": False}

	# Removed from __Auth directly: Document.save skips empty Password fields,
	# so clearing the field alone would leave a usable token behind.
	frappe.db.sql(
		"delete from `__Auth` where `doctype`='Agent Settings' and `name`=%s and `fieldname`='access_token'",
		doc.name,
	)
	frappe.db.set_value("Agent Settings", doc.name, "access_token", "")
	frappe.db.commit()
	return {"success": True}


@frappe.whitelist()
def delete_agent_account(agent_email: str) -> dict:
	"""Delete the caller's own agent account on the platform, then locally.

	Guard clauses over nesting (project_rules.md §1): the ownership check in
	get_agent_settings_doc is what stops one signed-in user from deleting
	another user's paid account by naming their e-mail.
	"""
	if not agent_email:
		return {"success": False}

	_assert_signed_in()

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		return {"success": False}

	access_token = doc.get_password("access_token", raise_exception=False)
	user_id = decode_jwt_payload(access_token).get("sub") if access_token else None

	# Fall back to the API key for accounts created before tokens carried a sub.
	if not user_id:
		user_id = doc.get_password("api_key", raise_exception=False)

	if user_id:
		try:
			response = requests.delete(
				f"{get_agent_server_url()}/users/{user_id}", timeout=15
			)
			if response.status_code not in (200, 404):
				frappe.throw(_("The agent account could not be deleted. Please try again."))
		except requests.exceptions.RequestException as exc:
			frappe.log_error(
				title="Accountant Agent: account deletion",
				message=f"Could not reach the agent server to delete {agent_email}: {exc}",
			)
			frappe.throw(_("Could not reach the Razyyn service. Please try again."))

	frappe.delete_doc("Agent Settings", doc.name, ignore_permissions=True)
	frappe.db.commit()
	return {"success": True}


# ---------------- Chat Session Management Endpoints ----------------

@frappe.whitelist()
def get_chats():
	"""Retrieves all chat sessions owned by the logged-in user."""
	user = frappe.session.user
	if user == "Guest":
		return []
		
	return frappe.get_all(
		"Agent Chats",
		filters={"owner": user},
		fields=["name", "session_id", "title", "last_update", "creation"],
		order_by="last_update desc, creation desc"
	)


@frappe.whitelist()
def create_chat(title=None):
	"""Creates a new chat session and returns it."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to ERPNext first."))
		
	session_id = str(uuid.uuid4())
	
	doc = frappe.get_doc({
		"doctype": "Agent Chats",
		"session_id": session_id,
		"title": title or _("New Chat"),
		"last_update": frappe.utils.now_datetime()
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	
	return {
		"name": doc.name,
		"session_id": doc.session_id,
		"title": doc.title,
		"last_update": doc.last_update
	}


@frappe.whitelist()
def update_chat_title(session_id, title):
	"""Updates the title of a chat session."""
	if not session_id or not title:
		frappe.throw(_("Session ID and Title are required."))
		
	if not frappe.db.exists("Agent Chats", session_id):
		frappe.throw(_("Chat session not found."))
		
	doc = frappe.get_doc("Agent Chats", session_id)
	if doc.owner != frappe.session.user:
		frappe.throw(_("Not authorized to rename this chat."))
		
	doc.title = title
	doc.last_update = frappe.utils.now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	
	return {
		"name": doc.name,
		"session_id": doc.session_id,
		"title": doc.title,
		"last_update": doc.last_update
	}


@frappe.whitelist()
def delete_chat(session_id):
	"""Deletes a chat session (cascade deletion of messages is handled by the before_delete hook)."""
	if not session_id:
		return {"success": False}
		
	if not frappe.db.exists("Agent Chats", session_id):
		return {"success": False}
		
	doc = frappe.get_doc("Agent Chats", session_id)
	if doc.owner != frappe.session.user:
		frappe.throw(_("Not authorized to delete this chat."))
		
	frappe.delete_doc("Agent Chats", session_id, ignore_permissions=True)
	frappe.db.commit()
	
	return {"success": True}


@frappe.whitelist()
def create_chat_with_id(session_id, title=None):
	"""Creates a new chat session with a pre-defined session_id."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to ERPNext first."))
		
	if not session_id:
		frappe.throw(_("Session ID is required."))
		
	if frappe.db.exists("Agent Chats", session_id):
		frappe.throw(_("Chat session already exists."))

	# The client picks this identifier, so it must look like one the client
	# generated rather than an arbitrary string that could collide with, or be
	# confused for, another customer's session key.
	try:
		uuid.UUID(str(session_id))
	except (ValueError, AttributeError, TypeError):
		frappe.throw(_("Invalid session identifier."), frappe.ValidationError)
		
	doc = frappe.get_doc({
		"doctype": "Agent Chats",
		"session_id": session_id,
		"title": title or _("New Chat"),
		"last_update": frappe.utils.now_datetime()
	})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	
	return {
		"name": doc.name,
		"session_id": doc.session_id,
		"title": doc.title,
		"last_update": doc.last_update
	}


# ─── Utility Helpers ────────────────────────────────────────────────────────

def _parse_json_list(value) -> list | None:
	"""Safely parse a JSON string into a list. Returns None if empty or invalid."""
	if not value:
		return None
	if isinstance(value, list):
		return value
	try:
		parsed = json.loads(value)
		if isinstance(parsed, list):
			return parsed
	except (json.JSONDecodeError, TypeError):
		pass
	return None


# ─── Agent File Upload Endpoint ─────────────────────────────────────────────

AGENT_UPLOAD_DIR: str = "agent_uploads"
#: How long a single agent request may take, end to end.
#:
#: A customer can legitimately ask for something that runs for hours - a
#: reconciliation across a full year, an import of several hundred entries, an
#: audit sweep over a large ledger. Cutting that off at ten minutes does not
#: protect anything; it destroys work that was progressing normally and gives
#: the customer a timeout error to explain.
AGENT_TASK_TIMEOUT_SECONDS: int = 3 * 60 * 60      # 3 hours

#: Splitting connect from read is the point.
#:
#: Failing to REACH the server is immediate and worth reporting quickly, so the
#: connect budget stays short. Once connected, a gap between chunks means the
#: agent is thinking, or the customer is on a slow link - neither is an error,
#: and `requests` applies the read budget per chunk rather than to the whole
#: response. A single number forces one of the two to be wrong.
AGENT_CONNECT_TIMEOUT_SECONDS: int = 30
AGENT_STREAM_TIMEOUT: tuple[int, int] = (
	AGENT_CONNECT_TIMEOUT_SECONDS,
	AGENT_TASK_TIMEOUT_SECONDS,
)

MAX_UPLOAD_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB: full-year ledger exports are large

#: Every document, data and image type an accountant legitimately sends, and
#: nothing that carries executable code.
#:
#: WHY AN ALLOWLIST AND NOT A BLOCKLIST
#:     A blocklist must be right about every dangerous extension that exists,
#:     including the ones invented after this line was written. An allowlist
#:     must be right about the ones we accept. Only the second is a property
#:     this code can actually hold. The set is therefore deliberately broad -
#:     the goal is that a real accountant never meets a refusal - but it stays
#:     a closed set, and anything not named here is declined.
#:
#: WHAT IS DELIBERATELY ABSENT, AND WHY
#:     Source and script files (.py .js .sh .php .rb .pl .ps1 .bat .cmd .vbs),
#:     executables and libraries (.exe .dll .so .msi .jar .apk .bin), and
#:     macro-enabled Office formats (.xlsm .xlsb .docm .pptm) - a workbook does
#:     not need a macro to be read, and a file that runs is not a document.
#:     Markup a browser will execute (.html .svg .xhtml .hta) is excluded for
#:     the same reason: these are rendered, and rendering is execution.
#:
#:     Archives (.zip .7z .rar) are absent for a different reason - the agent
#:     cannot read inside one, so accepting it would mean an upload that
#:     succeeds and then cannot be used, plus a decompression surface to
#:     defend. Supporting them properly means bounded extraction, and that is
#:     a feature rather than a line in a set.
#:
#: THIS SET MUST EQUAL THE SERVER'S, AND FOR A WHILE IT DID NOT
#:     The picker only decides what the file dialog offers. The refusal that
#:     matters is `file_text_receiver.ALLOWED_EXTENSIONS` on the agent platform,
#:     and an extension offered here but absent there is not a lenient picker -
#:     it is an accountant choosing a file, waiting for the upload, and then
#:     being told it is not supported.
#:
#:     That is exactly what shipped. Archive support (`extract_archive`) was
#:     built, .zip was added here citing it, and the extraction path was later
#:     removed from the platform - the function no longer exists and its test
#:     was deleted - while this set kept advertising it. Twenty-four extensions
#:     drifted apart the same way: .doc .xls .ppt .rtf, the config-text formats
#:     (.log .yaml .yml .toml .ini .cfg .conf .rst), mail (.eml .msg .mbox .ics
#:     .vcf) and the images the vision path cannot decode (.bmp .tif .tiff
#:     .heic .heif .avif).
#:
#:     Legacy Office binaries (.doc .xls .ppt) are not an oversight in the
#:     platform's list: the modern container is XML and macro-free, the 1997
#:     binary is neither. Send .docx/.xlsx/.pptx.
#:
#:     api/tests/test_upload_types.py::test_the_picker_and_the_server_agree
#:     compares the two sets and fails if they ever separate again.
ALLOWED_ACCOUNTANT_EXTENSIONS: frozenset[str] = frozenset({
	# Portable documents and word processing, macro-free formats only
	".pdf", ".docx", ".odt",
	# Spreadsheets, macro-free formats only
	".xlsx", ".ods",
	# Presentations, macro-free formats only
	".pptx", ".odp",
	# Plain text, notes and structured data
	".txt", ".md", ".markdown", ".csv", ".tsv", ".psv", ".dat",
	".json", ".jsonl", ".ndjson", ".xml",
	# Accounting and banking interchange formats
	".ofx", ".qfx", ".qbo", ".qif", ".mt940", ".sta", ".camt", ".aba",
	".bai", ".bai2", ".edi", ".x12", ".iif", ".xbrl", ".ubl",
	# Scans and photographed receipts. Exactly the set the platform's vision
	# path decodes - an image accepted here but absent there is stored and
	# then silently unreadable.
	".png", ".jpg", ".jpeg", ".gif", ".webp",
})
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({
	".png", ".jpg", ".jpeg", ".gif", ".webp",
})

#: The whitelisted route every stored attachment URL points at. Storing the
#: endpoint rather than a filesystem path is what lets the file itself live in
#: the private store while the chat UI keeps rendering a plain link.
_DOWNLOAD_ENDPOINT: str = (
	"/api/method/accountant_agent.accountant_agent.page.agent_chat.agent_chat.download_file"
)


def _owner_token(user: str) -> str:
	"""A stable, opaque directory name for one user's uploads.

	Ownership is enforced by the shape of the path, not by the secrecy of this
	value. `resolve_agent_upload_path` takes only the BASENAME from the caller's
	URL and joins it beneath the directory of whoever is authenticated — the
	caller can never supply the directory component. A user asking for a
	colleague's filename therefore looks in their own directory and finds
	nothing, whether or not they can guess what the colleague's token is.

	Because secrecy buys nothing here, this is deliberately NOT keyed on the
	site's encryption key. Keying it there would mean that rotating that key —
	which Frappe supports, and which re-encrypts `__Auth` — silently orphaned
	every attachment in every live conversation, since the recomputed token
	would point at a directory that no longer exists. A plain digest of the
	user id is stable for the life of the site.

	The digest rather than the raw e-mail keeps addresses out of directory
	listings and off any path that reaches a log.
	"""
	return hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]


def _upload_root(user: str) -> str:
	"""Absolute path of one user's private upload directory."""
	return frappe.get_site_path("private", "files", AGENT_UPLOAD_DIR, _owner_token(user))


def resolve_agent_upload_path(file_url: str, user: str) -> Optional[str]:
	"""Filesystem path for a stored attachment URL, or None if it is not the caller's.

	Accepts the private form written since this app started storing uploads
	privately, and still resolves the legacy public form so conversations that
	predate the change keep rendering their attachments.
	"""
	if not file_url:
		return None

	name = os.path.basename(file_url.split("?")[-1] if "file_url=" in file_url else file_url)
	# Defence in depth behind basename: a traversal attempt should be visible
	# as a refusal, not silently normalised away.
	if not name or name in (".", "..") or "/" in name or "\\" in name:
		return None

	private_path = os.path.join(_upload_root(user), name)
	if os.path.exists(private_path):
		return private_path

	# Legacy: uploads written to the public store before this app moved them.
	if AGENT_UPLOAD_DIR in file_url:
		legacy = frappe.get_site_path("public", "files", AGENT_UPLOAD_DIR, name)
	else:
		legacy = frappe.get_site_path("public", "files", name)

	return legacy if os.path.exists(legacy) else None


def _original_filename(stored_name: str) -> str:
	"""Strip the collision-avoidance prefix back off a stored filename."""
	if len(stored_name) > 13 and stored_name[12] == "_":
		return stored_name[13:]
	return stored_name


@frappe.whitelist()
def upload_agent_file() -> dict:
	"""Store one attachment for the signed-in user and return its download URL.

	WHY THE PRIVATE STORE
	    These are bank statements, trial balances and fixed-asset registers.
	    Frappe serves everything under `<site>/public/files/` to anonymous
	    callers by URL alone — no session, no permission check — so a file
	    written there is published to anyone who ever sees the link, including
	    through a browser referrer header or a copied chat transcript. The
	    unguessable filename was doing all the work, and an unguessable
	    identifier is not an access control.

	Returns:
		dict with keys: file_url (str), filename (str), is_image (bool).
	"""
	user = _assert_signed_in()

	uploaded_file = frappe.request.files.get("file")
	if not uploaded_file:
		frappe.throw(_("No file was uploaded."), frappe.ValidationError)

	filename = uploaded_file.filename
	if not filename:
		frappe.throw(_("Filename is missing."), frappe.ValidationError)

	extension = os.path.splitext(filename.lower())[1]
	if extension not in ALLOWED_ACCOUNTANT_EXTENSIONS:
		frappe.throw(
			_("File type '{0}' is not permitted. Allowed types: {1}").format(
				extension, ", ".join(sorted(ALLOWED_ACCOUNTANT_EXTENSIONS))
			),
			frappe.ValidationError,
		)

	content = uploaded_file.read()
	if len(content) > MAX_UPLOAD_SIZE_BYTES:
		frappe.throw(
			_("File exceeds the maximum allowed size of {0} MB. Your file is {1:.1f} MB.").format(
				MAX_UPLOAD_SIZE_BYTES // (1024 * 1024), len(content) / (1024 * 1024)
			),
			frappe.ValidationError,
		)

	upload_dir = _upload_root(user)
	os.makedirs(upload_dir, exist_ok=True)

	# Frappe never serves the private store directly, but a directory listing
	# left world-readable on a shared host would defeat the point of moving here.
	os.chmod(upload_dir, 0o700)

	stored_name = f"{uuid.uuid4().hex[:12]}_{os.path.basename(filename)}"
	with open(os.path.join(upload_dir, stored_name), "wb") as handle:
		handle.write(content)

	return {
		"file_url": f"{_DOWNLOAD_ENDPOINT}?file_url={AGENT_UPLOAD_DIR}/{stored_name}",
		"filename": filename,
		"is_image": extension in _IMAGE_EXTENSIONS,
	}


@frappe.whitelist()
def download_file(file_url: str) -> None:
	"""Serve one of the caller's own attachments, inline where the browser can.

	The only route to an agent attachment. `resolve_agent_upload_path` scopes
	the lookup to the caller's own directory, so a signed-in user asking for
	somebody else's filename gets the same answer as one asking for a filename
	that never existed.
	"""
	user = _assert_signed_in()

	if not file_url:
		frappe.throw(_("File URL is required."), frappe.ValidationError)

	file_path = resolve_agent_upload_path(file_url, user)
	if not file_path:
		frappe.throw(_("File not found."), frappe.DoesNotExistError)

	try:
		with open(file_path, "rb") as handle:
			frappe.local.response.filecontent = handle.read()
	except OSError:
		frappe.throw(_("Unable to read file content."), frappe.ValidationError)

	stored_name = os.path.basename(file_path)
	content_type, _encoding = mimetypes.guess_type(file_path)

	frappe.local.response.filename = _original_filename(stored_name)
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	if content_type:
		frappe.local.response.content_type = content_type
