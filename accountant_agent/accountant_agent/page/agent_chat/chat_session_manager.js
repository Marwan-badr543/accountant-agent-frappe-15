/**
 * Chat Session Manager Module
 * ----------------------------
 * Encapsulates chat session state and server communications:
 * list loading, session selection, draft creation, renaming, and deletion.
 */

class ChatSessionManager {
	constructor(chat_instance) {
		this.chat = chat_instance;
		this.chats = [];
		this.session_id = null;
		this.is_new_chat_draft = false;
	}

	async load_chats(reload_active = true) {
		try {
			this.chats = await frappe.xcall(
				'accountant_agent.accountant_agent.page.agent_chat.agent_chat.get_chats'
			);

			this.render_chat_list();

			if (this.chats.length > 0) {
				let active_exists = this.chats.some(c => c.session_id === this.session_id);
				if (!active_exists) {
					this.session_id = this.chats[0].session_id;
					this.is_new_chat_draft = false;
					await this.select_chat(this.session_id);
				} else if (reload_active) {
					await this.select_chat(this.session_id);
				}
			} else {
				this.set_new_chat_draft();
			}
		} catch (e) {
			console.error("Error loading chats:", e);
		}
	}

	render_chat_list() {
		let $sidebar = this.chat.sidebar;
		if (!$sidebar) return;

		let list_container = $sidebar.find('.agent-chat-list').empty();

		this.chats.forEach(chat => {
			let chat_item = $(`
				<div class="agent-chat-item ${chat.session_id === this.session_id ? 'active' : ''}" data-id="${chat.session_id}">
					<div class="chat-item-title-wrapper">
						<i class="fa fa-comment-o chat-icon"></i>
						<span class="chat-title">${chat.title || __('New Chat')}</span>
					</div>
					<div class="chat-item-actions">
						<button class="chat-action-btn rename-btn" title="${__('Rename')}">
							<i class="fa fa-pencil"></i>
						</button>
						<button class="chat-action-btn delete-btn" title="${__('Delete')}">
							<i class="fa fa-trash-o"></i>
						</button>
					</div>
				</div>
			`);

			chat_item.on('click', (e) => {
				if ($(e.target).closest('.chat-action-btn').length === 0) {
					this.select_chat(chat.session_id);
				}
			});

			chat_item.find('.rename-btn').on('click', () => {
				this.rename_chat(chat.session_id, chat.title);
			});

			chat_item.find('.delete-btn').on('click', () => {
				this.delete_chat_session(chat.session_id);
			});

			list_container.append(chat_item);
		});
	}

	async select_chat(session_id) {
		this.session_id = session_id;
		this.is_new_chat_draft = false;

		if (this.chat.sidebar) {
			this.chat.sidebar.find('.agent-chat-item').removeClass('active');
			this.chat.sidebar.find(`.agent-chat-item[data-id="${session_id}"]`).addClass('active');
		}

		await this.load_chat_history();
	}

	set_new_chat_draft() {
		this.chat.ui_manager.clear_typing_timers();
		this.is_new_chat_draft = true;
		this.session_id = this.chat.generate_uuid();

		if (this.chat.sidebar) {
			this.chat.sidebar.find('.agent-chat-item').removeClass('active');
		}

		this.chat.ui_manager.render_welcome(this.chat.msg_box);
	}

	rename_chat(session_id, current_title) {
		frappe.prompt(
			[
				{
					label: __('Chat Title'),
					fieldname: 'title',
					fieldtype: 'Data',
					default: current_title,
					reqd: 1
				}
			],
			async (values) => {
				frappe.dom.freeze(__('Renaming...'));
				try {
					await frappe.xcall(
						'accountant_agent.accountant_agent.page.agent_chat.agent_chat.update_chat_title',
						{ session_id: session_id, title: values.title }
					);
					await this.load_chats();
				} catch (e) {
					console.error("Rename chat failed:", e);
				} finally {
					frappe.dom.unfreeze();
				}
			},
			__('Rename Chat'),
			__('Save')
		);
	}

	delete_chat_session(session_id) {
		frappe.confirm(
			__('Are you sure you want to delete this chat session? All its messages will be permanently deleted.'),
			async () => {
				frappe.dom.freeze(__('Deleting chat...'));
				try {
					await frappe.xcall(
						'accountant_agent.accountant_agent.page.agent_chat.agent_chat.delete_chat',
						{ session_id: session_id }
					);

					if (this.session_id === session_id) {
						this.session_id = null;
					}
					await this.load_chats();
				} catch (e) {
					console.error("Delete chat failed:", e);
				} finally {
					frappe.dom.unfreeze();
				}
			}
		);
	}

	async load_chat_history() {
		this.chat.ui_manager.clear_typing_timers();
		this.chat.msg_box.empty();
		if (!this.session_id || this.is_new_chat_draft) {
			this.chat.ui_manager.render_welcome(this.chat.msg_box);
			return;
		}

		try {
			let messages = await frappe.xcall(
				'accountant_agent.accountant_agent.page.agent_chat.agent_chat.get_chat_history',
				{ session_id: this.session_id }
			);

			if (messages && messages.length > 0) {
				messages.forEach((msg, idx) => {
					let has_subsequent = (idx < messages.length - 1);
					this.chat.ui_manager.append_message(this.chat.msg_box, msg.sender, msg.content, false, msg.creation || msg.creation1, has_subsequent);
				});
			} else {
				this.chat.ui_manager.render_welcome(this.chat.msg_box);
			}
		} catch (e) {
			console.error("Error loading chat history:", e);
			this.chat.ui_manager.render_welcome(this.chat.msg_box);
		}
	}
}
