# -*- coding: utf-8 -*-
# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see license.txt

import base64
import json
import os
import uuid
import requests
import frappe
from frappe import _

AGENT_SERVER_URL = "http://127.0.0.1:4000"


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
		frappe.log_error(f"JWT decode error: {str(e)}", "Accountant Agent JWT Decode")
	return {}


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
		response = requests.post(f"{AGENT_SERVER_URL}/users/", json=payload, timeout=15)
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
		response = requests.post(f"{AGENT_SERVER_URL}/auth/login", json=login_payload, timeout=15)
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
		response = requests.post(f"{AGENT_SERVER_URL}/auth/refresh", json=refresh_payload, timeout=15)
		if response.status_code == 200:
			return response.json().get("access_token")
	except Exception as e:
		frappe.log_error(f"Agent token refresh request error: {str(e)}", "Accountant Agent Refresh")
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


def save_agent_settings(email: str, api_key: str = None, access_token: str = None) -> None:
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
			doc = frappe.get_doc({
				"doctype": "Agent Settings",
				"email": email,
				"api_key": api_key,
				"access_token": access_token or ""
			})
			doc.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(f"Error saving Agent Settings: {str(e)}", "Accountant Agent Save Settings")
		frappe.throw(_(f"Failed to save credentials locally: {str(e)}"))


def save_chat_history(session_id: str, sender: str, content: str) -> None:
	"""Saves a message in the Agent Chat History."""
	try:
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


def get_history_payload(session_id: str) -> list:
	"""Retrieves the last 10 messages to build chat history context."""
	history_records = frappe.get_all(
		"Agent Chat History",
		filters={"session_id": session_id},
		fields=["sender", "content", "creation"],
		order_by="creation desc",
		limit=10
	)
	# Reverse to restore chronological order (oldest first)
	history_records.reverse()
	
	payload = []
	for rec in history_records:
		content = rec.content or ""
		# Format clarification json if present
		if rec.sender == "ai" and content.startswith('{"type": "clarification"'):
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

		payload.append({
			"role": "user" if rec.sender == "human" else "assistant",
			"content": content
		})
	return payload


def post_message_to_agent(
	message: str,
	history: list,
	token: str,
	custom_instructions: str = None,
	session_id: str = None,
	agent_type: str = "ask",
	file_urls: list = None,
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
					clean_filename = filename[13:] if (len(filename) > 13 and filename[12] == '_') else filename
					files_list.append(("files", (clean_filename, f_obj)))

		# Route request directly to specific endpoint based on agent_type
		agent_endpoint = agent_type if agent_type in ("ask", "analyse", "audit") else "ask"
		endpoint_url = f"{AGENT_SERVER_URL}/agent/{agent_endpoint}"

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


@frappe.whitelist()
def send_message(message, session_id, agent_email, agent_type="ask", file_urls=None):
	"""Proxy message send to agent, handle token expiration/refresh, and save chat history."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not authenticated with ERPNext."))
		
	doc = get_agent_settings_doc(agent_email)
	if not doc:
		frappe.throw(_("Not authenticated with Accountant Agent."))
		
	access_token = doc.get_password("access_token")
	
	if not access_token:
		frappe.throw(_("Missing access token. Please re-authenticate."))

	custom_instructions = getattr(doc, "custom_instructions", None) or ""

	# Deserialize file_urls list if sent as JSON string
	parsed_file_urls = _parse_json_list(file_urls)

	# 1. Save user message to client DB if not a clarification response
	if not message.startswith("Clarification Response:"):
		save_chat_history(session_id, "human", message)
		update_chat_timestamp(session_id)

	# 2. Get chat history payload
	history_payload = get_history_payload(session_id)

	# 3. Call agent server ask API
	try:
		response = post_message_to_agent(
			message, history_payload, access_token,
			custom_instructions=custom_instructions,
			session_id=session_id,
			agent_type=agent_type,
			file_urls=parsed_file_urls,
		)
		
		# 4. Handle expired token (401)
		if response.status_code == 401:
			new_access_token = refresh_agent_token_on_server(access_token)
			if new_access_token:
				save_agent_settings(agent_email, access_token=new_access_token)
				response = post_message_to_agent(
					message, history_payload, new_access_token,
					custom_instructions=custom_instructions,
					session_id=session_id,
					agent_type=agent_type,
					file_urls=parsed_file_urls,
				)
			else:
				# Clear invalid token to force re-login
				save_agent_settings(agent_email, access_token="")
				frappe.throw(_("Session expired. Please reconnect."))
				
		if response.status_code == 499:
			# Silent cancellation - do not throw error
			return {"cancelled": True}

		if response.status_code != 200:
			error_msg = response.json().get("detail", "Error from Agent Server.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
			
		ai_response = response.json().get("response")
		
		# 5. Store AI response in Client DB
		save_chat_history(session_id, "ai", ai_response)
		update_chat_timestamp(session_id)
		
		return {"response": ai_response}
		
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Agent chat request exception: {str(e)}", "Accountant Agent Chat")
		frappe.throw(_("Unable to communicate with Agent Server. Please check if it's running."))


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

	headers = {
		"Authorization": f"Bearer {access_token}",
		"Content-Type": "application/json"
	}
	payload = {
		"session_id": session_id
	}
	try:
		response = requests.post(f"{AGENT_SERVER_URL}/agent/cancel", json=payload, headers=headers, timeout=15)
		
		# Handle expired token (401)
		if response.status_code == 401:
			new_access_token = refresh_agent_token_on_server(access_token)
			if new_access_token:
				save_agent_settings(agent_email, access_token=new_access_token)
				headers["Authorization"] = f"Bearer {new_access_token}"
				response = requests.post(f"{AGENT_SERVER_URL}/agent/cancel", json=payload, headers=headers, timeout=15)
			else:
				# Clear invalid token to force re-login
				save_agent_settings(agent_email, access_token="")
				frappe.throw(_("Session expired. Please reconnect."))
				
		if response.status_code != 200:
			error_msg = response.json().get("detail", "Error from Agent Server.")
			frappe.throw(_(f"Agent Server Error: {error_msg}"))
			
		return {"success": True, "message": response.json().get("message")}
		
	except requests.exceptions.RequestException as e:
		frappe.log_error(f"Agent cancel request exception: {str(e)}", "Accountant Agent Cancel")
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
		order_by="creation asc"
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
			frappe.db.sql("delete from `__Auth` where `doctype`='Agent Settings' and `name`=%s and `fieldname`='access_token'", doc.name)
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
					response = requests.delete(f"{AGENT_SERVER_URL}/users/{user_id}", timeout=15)
					if response.status_code not in (200, 404):
						error_msg = response.json().get("detail", "Failed to delete account on Agent Server.")
						frappe.throw(_(f"Agent Server Error: {error_msg}"))
				except requests.exceptions.RequestException as e:
					frappe.log_error(f"Agent account deletion request error: {str(e)}", "Accountant Agent Delete Account")
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

AGENT_UPLOAD_DIR = "agent_uploads"
MAX_UPLOAD_SIZE_BYTES: int = 20 * 1024 * 1024  # 20 MB endpoint cap to support Excel uploads
ALLOWED_ACCOUNTANT_EXTENSIONS: set = {
	".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt",
	".pptx", ".ppt", ".png", ".jpg", ".jpeg", ".gif", ".webp"
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
			_(f"File type '{ext}' is not permitted for security reasons. Allowed types: {', '.join(sorted(ALLOWED_ACCOUNTANT_EXTENSIONS))}"),
			frappe.ValidationError,
		)

	# Read file content and validate size
	file_content = uploaded_file.read()
	file_size = len(file_content)

	if file_size > MAX_UPLOAD_SIZE_BYTES:
		size_mb = file_size / (1024 * 1024)
		frappe.throw(
			_(f"File exceeds the maximum allowed size of 20 MB. Your file is {size_mb:.1f} MB. Please upload a smaller file."),
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

	print(f"[DEBUG] download_file: file_url={file_url}, site={frappe.local.site}, file_path={file_path}, exists={os.path.exists(file_path)}")

	if not os.path.exists(file_path):
		frappe.throw(_("File not found."), frappe.DoesNotExistError)

	# Guess mime type for correct browser rendering/inline preview
	import mimetypes
	content_type, _ = mimetypes.guess_type(file_path)

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


