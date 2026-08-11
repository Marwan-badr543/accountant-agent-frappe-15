# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import base64
import json
import os
import uuid

import frappe
import requests
from frappe import _

from accountant_agent.accountant_agent.doctype.agent_settings.agent_settings import get_agent_server_url

# ---------------- JWT Helpers ----------------


def decode_jwt_payload(token: str) -> dict:
	"""
	Decodes the JWT token payload without validating the signature.
	Returns the payload dictionary, or an empty dictionary if invalid.
	"""
	try:
		parts = token.split(".")
		if len(parts) == 3:
			payload_b64 = parts[1]
			# Add base64 padding
			payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
			payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
			return json.loads(payload_json)
	except Exception as e:
		frappe.log_error(title="Accountant Agent JWT Decode", message=f"JWT decode error: {e!s}")
	return {}


# ---------------- Server Communication Helpers ----------------


def register_agent_on_server(email: str, password: str, company_name: str, api_key: str) -> None:
	"""Sends a user registration POST request to the remote agent server."""
	payload = {
		"api_key": api_key,
		"name": company_name,
		"username": email,
		"password": password,
		"company_url": frappe.utils.get_url(),
	}
	try:
		response = requests.post(f"{get_agent_server_url()}/users/", json=payload, timeout=15)
		if response.status_code != 201:
			error_msg = response.json().get("detail", "Registration failed.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
	except requests.exceptions.RequestException as e:
		frappe.log_error(title="Accountant Agent Auth", message=f"Agent registration request error: {e!s}")
		frappe.throw(_("Could not connect to Agent Server. Please make sure the server is running."))


def login_agent_on_server(email: str, password: str) -> str:
	"""Logs in to the agent server and returns the access token."""
	login_payload = {"username": email, "password": password}
	try:
		response = requests.post(f"{get_agent_server_url()}/auth/login", json=login_payload, timeout=15)
		if response.status_code != 200:
			error_msg = response.json().get("detail", "Login failed. Check your email and password.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))

		token_data = response.json()
		return token_data.get("access_token")
	except requests.exceptions.RequestException as e:
		frappe.log_error(title="Accountant Agent Auth", message=f"Agent login request error: {e!s}")
		frappe.throw(_("Could not connect to Agent Server. Please make sure the server is running."))


def refresh_agent_token_on_server(access_token: str) -> str:
	"""Calls the agent server token refresh endpoint and returns the new access token."""
	refresh_payload = {"access_token": access_token}
	try:
		response = requests.post(f"{get_agent_server_url()}/auth/refresh", json=refresh_payload, timeout=15)
		if response.status_code == 200:
			return response.json().get("access_token")
	except Exception as e:
		frappe.log_error(
			title="Accountant Agent Refresh", message=f"Agent token refresh request error: {e!s}"
		)
	return None


# ---------------- Database Connection Helpers ----------------


def get_agent_settings_doc(email: str):
	"""Finds and returns the Agent Settings document matching the given email, or None."""
	if not email:
		return None
	name = frappe.db.get_value("Agent Settings", {"email": email}, "name")
	if name:
		return frappe.get_doc("Agent Settings", name)
	return None


def save_agent_settings(email: str, api_key: str | None = None, access_token: str | None = None) -> None:
	"""Creates or updates the Agent Settings record for the given agent email."""
	try:
		doc = get_agent_settings_doc(email)
		if doc:
			if access_token is not None:
				doc.access_token = access_token
			if api_key is not None:
				doc.api_key = api_key
			doc.save(ignore_permissions=True)
		else:
			if not api_key:
				frappe.throw(_("API Key is required to create new Agent Settings."))
			doc = frappe.get_doc(
				{
					"doctype": "Agent Settings",
					"email": email,
					"api_key": api_key,
					"access_token": access_token or "",
				}
			)
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(
			title="Accountant Agent Save Settings", message=f"Error saving Agent Settings: {e!s}"
		)
		frappe.throw(_(f"Failed to save credentials locally: {e!s}"))


def save_chat_history(session_id: str, sender: str, content: str) -> None:
	"""Saves a message in the Agent Chat History."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Agent Chat History",
				"creation1": frappe.utils.now_datetime(),
				"session_id": session_id,
				"sender": sender,
				"content": content,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(
			title="Accountant Agent Chat", message=f"Error saving user message to history: {e!s}"
		)


def get_history_payload(session_id: str) -> list:
	"""Retrieves the last 10 messages to build chat history context, formatting JSON structures cleanly."""
	history_records = frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["sender", "content", "creation"],
		order_by="creation desc",
		limit=30,
	)

	payload = []
	for rec in history_records:
		content = rec.content or ""
		# Format plan JSON if present
		if rec.sender == "ai" and content.startswith('{"type": "plan"'):
			try:
				data = json.loads(content)
				plan_text = data.get("plan", "")
				status = data.get("status", "pending")
				content = (
					f"Proposed Analysis Plan:\n{plan_text}\n\nUser Confirmation Status: {status.capitalize()}"
				)
			except Exception:
				pass
		# Format clarification json if present
		elif rec.sender == "ai" and content.startswith('{"type": "clarification"'):
			try:
				data = json.loads(content)
				formatted_qs = []
				for idx, q in enumerate(data.get("questions", [])):
					q_text = f"{idx + 1}. Question: {q.get('question')}"
					if q.get("options"):
						q_text += f"\nOptions: {', '.join(q.get('options'))}"
					formatted_qs.append(q_text)
				content = "Requested Clarifications:\n" + "\n\n".join(formatted_qs)
			except Exception:
				pass

		payload.append({"role": "user" if rec.sender == "human" else "assistant", "content": content})

	# Slice the last 10 messages (or fewer if not available)
	payload = payload[:10]
	# Reverse to restore chronological order (oldest first)
	payload.reverse()
	return payload


def post_message_to_agent(
	message: str,
	history: list,
	token: str,
	custom_instructions: str | None = None,
	session_id: str | None = None,
	agent_type: str = "ask",
	file_urls: list | None = None,
) -> requests.Response:
	"""Sends message to the agent server ask API, with optional attached files and agent_type."""
	headers = {
		"Authorization": f"Bearer {token}",
	}
	payload_data = {
		"message": message,
		"history": json.dumps(history) if history else "",
		"custom_instructions": custom_instructions or "",
		"session_id": session_id or "",
		"agent_type": agent_type or "ask",
	}

	files_list = []
	opened_files = []

	try:
		if file_urls:
			for url in file_urls:
				filename = os.path.basename(url)
				if "agent_uploads" in url:
					file_path = frappe.get_site_path("public", "files", "agent_uploads", filename)
				else:
					file_path = frappe.get_site_path("public", "files", filename)

				if os.path.exists(file_path):
					f_obj = open(file_path, "rb")
					opened_files.append(f_obj)
					# Strip unique hex prefix if present for original filename
					clean_filename = (
						filename[13:] if (len(filename) > 13 and filename[12] == "_") else filename
					)
					files_list.append(("files", (clean_filename, f_obj)))

		# Route request directly to specific endpoint based on agent_type
		agent_endpoint = agent_type if agent_type in ("ask", "analyse", "audit") else "ask"
		endpoint_url = f"{get_agent_server_url()}/agent/{agent_endpoint}"

		return requests.post(
			endpoint_url,
			data=payload_data,
			files=files_list if files_list else None,
			headers=headers,
			timeout=180,
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
			token = doc.get_password("access_token")
			if token:
				return {"connected": True, "email": doc.email}
	except Exception as e:
		frappe.log_error(title="Accountant Agent Connect", message=f"Error checking connection status: {e!s}")

	return {"connected": False, "email": None}


@frappe.whitelist()
def authenticate_agent(mode, email, password, company_name=None):
	"""Handles login or signup requests against the agent server and updates local settings."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to ERPNext first."))

	if not email or not password:
		frappe.throw(_("Email and password are required."))

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
	return {"success": True, "email": email, "api_key": doc.get_password("api_key")}


def get_latest_plan_message(session_id: str, lock: bool = False):
	"""Find the latest plan message in Agent Chat History for the given session."""
	messages = frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["name", "content"],
		order_by="creation desc",
		limit=20,
	)
	for msg in messages:
		if msg.content and msg.content.startswith('{"type": "plan"'):
			if lock:
				try:
					# Apply row-level lock using FOR UPDATE with appropriate error handling
					frappe.db.sql(
						"select name from `tabAgent Chat History` where name=%s for update", msg.name
					)
					# Return fresh doc after lock is acquired
					return frappe.get_doc("Agent Chat History", msg.name)
				except Exception as e:
					frappe.log_error(
						title="Accountant Agent Plan Lock",
						message=f"Database lock timeout or error: {e!s}",
					)
					frappe.throw(_("Could not acquire lock on the plan record. Please try again."))
			return msg
	return None


@frappe.whitelist()
def send_message(message, session_id, agent_email, agent_type="ask", file_urls=None):
	"""Proxy message send to agent by enqueuing a background worker to handle streaming."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not authenticated with ERPNext."))

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		frappe.throw(_("Not authenticated with Accountant Agent."))

	access_token = doc.get_password("access_token")
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
			frappe.log_error(
				title="Accountant Agent Plan Status Update",
				message=f"Error updating plan status JSON: {e!s}",
			)

	# Save user message to client DB if not "Approve" and not a clarification response
	if message != "Approve" and not message.startswith("Clarification Response:"):
		save_chat_history(session_id, "human", message)
		update_chat_timestamp(session_id)

	# Enqueue the task to background worker to avoid HTTP timeout
	frappe.enqueue(
		"accountant_agent.accountant_agent.page.agent_chat.agent_chat.process_agent_message_background",
		queue="long",
		timeout=600,
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
		frappe.log_error(title="Accountant Agent Stream", message=error_msg)
		frappe.publish_realtime(
			event="agent_message_error",
			message={"session_id": session_id, "error": error_msg},
			user=user,
		)
		return

	access_token = doc.get_password("access_token")
	if not access_token:
		error_msg = "Access token missing. Please reconnect."
		frappe.publish_realtime(
			event="agent_message_error",
			message={"session_id": session_id, "error": error_msg},
			user=user,
		)
		return

	custom_instructions = getattr(doc, "custom_instructions", None) or ""
	history_payload = get_history_payload(session_id)

	headers = {
		"Authorization": f"Bearer {access_token}",
	}
	payload_data = {
		"message": message,
		"history": json.dumps(history_payload) if history_payload else "",
		"custom_instructions": custom_instructions,
		"session_id": session_id or "",
		"erp_system": "ERPNext",
		"stream": "true",
	}

	files_list = []
	opened_files = []

	try:
		if file_urls:
			for url in file_urls:
				filename = os.path.basename(url)
				if "agent_uploads" in url:
					file_path = frappe.get_site_path("public", "files", "agent_uploads", filename)
				else:
					file_path = frappe.get_site_path("public", "files", filename)

				if os.path.exists(file_path):
					f_obj = open(file_path, "rb")
					opened_files.append(f_obj)
					clean_filename = (
						filename[13:] if (len(filename) > 13 and filename[12] == "_") else filename
					)
					files_list.append(("files", (clean_filename, f_obj)))

		agent_endpoint = agent_type if agent_type in ("ask", "analyse", "audit") else "ask"
		endpoint_url = f"{get_agent_server_url()}/agent/{agent_endpoint}"

		response = requests.post(
			endpoint_url,
			data=payload_data,
			files=files_list if files_list else None,
			headers=headers,
			stream=True,
			timeout=600,
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
					timeout=600,
				)
			else:
				save_agent_settings(agent_email, access_token="")
				raise Exception("Session expired. Please reconnect.")

		if response.status_code == 499:
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
		for line in response.iter_lines():
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
					frappe.publish_realtime(
						event="agent_node_start",
						message={"session_id": session_id, "node": data_json.get("node", "")},
						user=user,
					)
				elif current_event == "tool_start":
					frappe.publish_realtime(
						event="agent_tool_start",
						message={
							"session_id": session_id,
							"tool": data_json.get("tool", ""),
							"input": data_json.get("input", {}),
						},
						user=user,
					)
				elif current_event == "done":
					ai_response = data_json.get("response", "")
					save_chat_history(session_id, "ai", ai_response)
					update_chat_timestamp(session_id)

					frappe.publish_realtime(
						event="agent_message_done",
						message={"session_id": session_id, "response": ai_response},
						user=user,
					)
				elif current_event == "error":
					raise Exception(data_json.get("detail", "Unknown error in stream"))

	except Exception as e:
		error_msg = str(e)
		frappe.log_error(
			title="Accountant Agent Chat Background",
			message=f"Error processing agent message in background: {error_msg}",
		)
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


@frappe.whitelist()
def cancel_agent(session_id, agent_email):
	"""Proxy cancellation request to the agent server."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not authenticated with ERPNext."))

	doc = get_agent_settings_doc(agent_email)
	if not doc:
		frappe.throw(_("Not authenticated with Accountant Agent."))

	access_token = doc.get_password("access_token")

	if not access_token:
		frappe.throw(_("Missing access token. Please re-authenticate."))

	headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
	payload = {"session_id": session_id}
	try:
		response = requests.post(
			f"{get_agent_server_url()}/agent/cancel", json=payload, headers=headers, timeout=15
		)

		# Handle expired token (401)
		if response.status_code == 401:
			new_access_token = refresh_agent_token_on_server(access_token)
			if new_access_token:
				save_agent_settings(agent_email, access_token=new_access_token)
				headers["Authorization"] = f"Bearer {new_access_token}"
				response = requests.post(
					f"{get_agent_server_url()}/agent/cancel", json=payload, headers=headers, timeout=15
				)
			else:
				# Clear invalid token to force re-login
				save_agent_settings(agent_email, access_token="")
				frappe.throw(_("Session expired. Please reconnect."))

		if response.status_code != 200:
			error_msg = response.json().get("detail", "Error from Agent Server.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))

		return {"success": True, "message": response.json().get("message")}

	except requests.exceptions.RequestException as e:
		frappe.log_error(title="Accountant Agent Cancel", message=f"Agent cancel request exception: {e!s}")
		frappe.throw(_("Unable to communicate with Agent Server. Please check if it's running."))


@frappe.whitelist()
def get_chat_history(session_id):
	"""Retrieves chat history messages for a specific session."""
	if not session_id:
		return []

	return frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["name", "sender", "content", "creation1", "creation"],
		order_by="creation asc",
	)


@frappe.whitelist()
def disconnect_agent(agent_email):
	"""Disconnects the agent for the given email by clearing the access token locally."""
	if not agent_email:
		return {"success": False}

	user = frappe.session.user
	if user != "Guest":
		doc = get_agent_settings_doc(agent_email)
		if doc:
			# Directly delete the token from the __Auth table as Frappe's save ignores empty password fields
			frappe.db.sql(
				"delete from `__Auth` where `doctype`='Agent Settings' and `name`=%s and `fieldname`='access_token'",
				doc.name,
			)
			frappe.db.set_value("Agent Settings", doc.name, "access_token", "")
			frappe.db.commit()
			return {"success": True}
	return {"success": False}


@frappe.whitelist()
def delete_agent_account(agent_email):
	"""Deletes the agent account on the agent server, then deletes the local settings document."""
	if not agent_email:
		return {"success": False}

	user = frappe.session.user
	if user != "Guest":
		doc = get_agent_settings_doc(agent_email)
		if doc:
			access_token = doc.get_password("access_token")
			user_id = None
			if access_token:
				payload = decode_jwt_payload(access_token)
				user_id = payload.get("sub")

			# Fallback to api_key for legacy users
			if not user_id:
				user_id = doc.get_password("api_key")

			if user_id:
				try:
					response = requests.delete(f"{get_agent_server_url()}/users/{user_id}", timeout=15)
					if response.status_code not in (200, 404):
						error_msg = response.json().get("detail", "Failed to delete account on Agent Server.")
						frappe.throw(_(f"Agent Server Error: {error_msg}"))
				except requests.exceptions.RequestException as e:
					frappe.log_error(
						title="Accountant Agent Delete Account",
						message=f"Agent account deletion request error: {e!s}",
					)
					frappe.throw(_("Could not connect to Agent Server to delete account. Please try again."))

			frappe.delete_doc("Agent Settings", doc.name, ignore_permissions=True)
			frappe.db.commit()
			return {"success": True}
	return {"success": False}


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
		order_by="last_update desc, creation desc",
	)


@frappe.whitelist()
def create_chat(title=None):
	"""Creates a new chat session and returns it."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Please log in to ERPNext first."))

	session_id = str(uuid.uuid4())

	doc = frappe.get_doc(
		{
			"doctype": "Agent Chats",
			"session_id": session_id,
			"title": title or _("New Chat"),
			"last_update": frappe.utils.now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"session_id": doc.session_id,
		"title": doc.title,
		"last_update": doc.last_update,
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
		"last_update": doc.last_update,
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

	doc = frappe.get_doc(
		{
			"doctype": "Agent Chats",
			"session_id": session_id,
			"title": title or _("New Chat"),
			"last_update": frappe.utils.now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"name": doc.name,
		"session_id": doc.session_id,
		"title": doc.title,
		"last_update": doc.last_update,
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

AGENT_UPLOAD_DIR = "agent_uploads"
MAX_UPLOAD_SIZE_BYTES: int = 20 * 1024 * 1024  # 20 MB endpoint cap to support Excel uploads
ALLOWED_ACCOUNTANT_EXTENSIONS: set = {
	".pdf",
	".docx",
	".doc",
	".xlsx",
	".xls",
	".csv",
	".txt",
	".pptx",
	".ppt",
	".png",
	".jpg",
	".jpeg",
	".gif",
	".webp",
}


@frappe.whitelist()
def upload_agent_file():
	"""
	Custom file upload endpoint that saves files to a dedicated agent_uploads directory.

	Reads the file from the request (multipart form data), validates safe file type and size,
	saves to <site>/public/files/agent_uploads/, and returns the file URL.

	Returns:
		dict with keys: file_url (str), filename (str), is_image (bool).
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.AuthenticationError)

	uploaded_file = frappe.request.files.get("file")
	if not uploaded_file:
		frappe.throw(_("No file was uploaded."), frappe.ValidationError)

	filename = uploaded_file.filename
	if not filename:
		frappe.throw(_("Filename is missing."), frappe.ValidationError)

	# Validate safe file extension
	ext = os.path.splitext(filename.lower())[1]
	if ext not in ALLOWED_ACCOUNTANT_EXTENSIONS:
		frappe.throw(
			_(
				f"File type '{ext}' is not permitted for security reasons. Allowed types: {', '.join(sorted(ALLOWED_ACCOUNTANT_EXTENSIONS))}"
			),
			frappe.ValidationError,
		)

	# Read file content and validate size
	file_content = uploaded_file.read()
	file_size = len(file_content)

	if file_size > MAX_UPLOAD_SIZE_BYTES:
		size_mb = file_size / (1024 * 1024)
		frappe.throw(
			_(
				f"File exceeds the maximum allowed size of 20 MB. Your file is {size_mb:.1f} MB. Please upload a smaller file."
			),
			frappe.ValidationError,
		)

	# Determine if file is an image
	image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
	is_image = ext in image_extensions

	# Ensure the agent_uploads directory exists
	upload_dir = frappe.get_site_path("public", "files", AGENT_UPLOAD_DIR)
	os.makedirs(upload_dir, exist_ok=True)

	# Generate a unique filename to prevent collisions
	unique_name = f"{uuid.uuid4().hex[:12]}_{filename}"
	file_path = os.path.join(upload_dir, unique_name)

	# Write file to disk
	with open(file_path, "wb") as f:
		f.write(file_content)

	file_url = f"/files/{AGENT_UPLOAD_DIR}/{unique_name}"

	return {
		"file_url": file_url,
		"filename": filename,
		"is_image": is_image,
	}


@frappe.whitelist()
def download_file(file_url: str):
	"""
	Downloads or serves a file from the agent_uploads directory or standard files.
	Supports inline display for images/PDFs.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Authentication required."), frappe.AuthenticationError)

	if not file_url:
		frappe.throw(_("File URL is required."), frappe.ValidationError)

	filename = os.path.basename(file_url)

	# Safety checks: prevent directory traversal
	if ".." in filename or "/" in filename or "\\" in filename:
		frappe.throw(_("Invalid filename."), frappe.ValidationError)

	if "agent_uploads" in file_url:
		file_path = frappe.get_site_path("public", "files", AGENT_UPLOAD_DIR, filename)
	else:
		file_path = frappe.get_site_path("public", "files", filename)

	if not os.path.exists(file_path):
		frappe.throw(_("File not found."), frappe.DoesNotExistError)

	# Guess mime type for correct browser rendering/inline preview
	import mimetypes

	content_type, _encoding = mimetypes.guess_type(file_path)

	frappe.local.response.filename = filename

	try:
		with open(file_path, "rb") as f:
			frappe.local.response.filecontent = f.read()
	except OSError:
		frappe.throw(_("Unable to read file content."), frappe.ValidationError)

	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"

	if content_type:
		frappe.local.response.content_type = content_type
