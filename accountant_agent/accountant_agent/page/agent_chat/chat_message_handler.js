/**
 * Chat Message Handler Module
 * ----------------------------
 * Handles sending user messages to the backend proxy with agent_type and file URLs,
 * managing response typing animation, execution cancellation, and clarification questions.
 */

class ChatMessageHandler {
	constructor(chat_instance) {
		this.chat = chat_instance;
		this.is_cancelled = false;
	}

	async send_user_message() {
		let session_id = this.chat.session_manager.session_id;
		if (!session_id) return;

		let message = this.chat.textarea.val();
		let has_attachments = this.chat.file_upload_handler && this.chat.file_upload_handler.has_attachments();

		if ((!message || message.trim() === '') && !has_attachments) return;

		if (message && message.length > 10000) {
			frappe.show_alert({ message: __('Message length exceeds the 10000 character limit.'), indicator: 'red' });
			return;
		}

		let file_urls = null;
		let attachment_markers = '';

		if (has_attachments) {
			file_urls = this.chat.file_upload_handler.get_file_urls();
			attachment_markers = this.chat.file_upload_handler.build_attachment_markers();
		}

		// Reset Textarea
		this.chat.textarea.val('');
		this.chat.textarea.css('height', '44px');
		this.chat.layout.find('.agent-char-counter').text('0 / 10000');

		if (this.chat.file_upload_handler) {
			this.chat.file_upload_handler.clear_attachments();
		}

		let full_message = attachment_markers + (message || '');
		await this.send_chat_message(full_message.trim(), file_urls);
	}

	async send_chat_message(message, file_urls = null) {
		let session_id = this.chat.session_manager.session_id;
		if (!session_id) return;

		message = frappe.utils.xss_sanitise(message);
		let active_session_id = session_id;
		this.is_cancelled = false;

		// Handle unsaved draft
		if (this.chat.session_manager.is_new_chat_draft) {
			let clean_msg = message.replace(/\[FILE:[^\]]+\]/g, '').replace(/\[IMAGE:[^\]]+\]/g, '').trim();
			let chat_title = (clean_msg || 'File upload').substring(0, 30);
			if (clean_msg.length > 30) chat_title += '...';

			frappe.dom.freeze(__('Starting conversation...'));
			try {
				await frappe.xcall(
					'accountant_agent.accountant_agent.page.agent_chat.agent_chat.create_chat_with_id',
					{ session_id: active_session_id, title: chat_title }
				);
				if (this.chat.session_manager.session_id === active_session_id) {
					this.chat.session_manager.is_new_chat_draft = false;
				}
			} catch (e) {
				console.error("Failed to initialize chat session:", e);
				frappe.dom.unfreeze();
				return;
			} finally {
				frappe.dom.unfreeze();
			}
		}

		if (this.chat.session_manager.session_id === active_session_id) {
			if (!message.startswith || !message.startswith("Clarification Response:")) {
				this.chat.ui_manager.append_message(this.chat.msg_box, 'user', message, false, new Date().toISOString());
			}
			this.chat.ui_manager.show_typing_indicator(this.chat.msg_box);
			this.set_button_state('cancel');
		}

		let agent_type = this.chat.agent_selector ? this.chat.agent_selector.get_selected_agent() : 'ask';

		try {
			let agent_email = localStorage.getItem('connected_agent_email');
			let res = await frappe.xcall(
				'accountant_agent.accountant_agent.page.agent_chat.agent_chat.send_message',
				{
					message: message,
					session_id: active_session_id,
					agent_email: agent_email,
					agent_type: agent_type,
					file_urls: file_urls ? JSON.stringify(file_urls) : null
				}
			);

			if (this.is_cancelled) {
				this.is_cancelled = false;
				return;
			}

			if (this.chat.session_manager.session_id === active_session_id) {
				this.chat.ui_manager.hide_typing_indicator(this.chat.msg_box);
				this.set_button_state('send');
				if (res && res.response) {
					await this.chat.session_manager.load_chats(false);
					await this.chat.ui_manager.append_message(this.chat.msg_box, 'ai', res.response, true, new Date().toISOString());
				} else {
					await this.chat.session_manager.load_chats(false);
				}
			} else {
				await this.chat.session_manager.load_chats(false);
			}
		} catch (err) {
			if (this.is_cancelled) {
				this.is_cancelled = false;
				return;
			}

			if (this.chat.session_manager.session_id === active_session_id) {
				this.chat.ui_manager.hide_typing_indicator(this.chat.msg_box);
				this.set_button_state('send');
				console.error("Message send failed:", err);
				let error_msg = err.message || '';
				if (!error_msg.includes('cancelled') && !error_msg.includes('cancellation')) {
					let final_err = error_msg || __('Unable to get response from Accountant Agent.');
					this.chat.ui_manager.append_message(this.chat.msg_box, 'ai', `⚠️ **Error:** ${final_err}`);
				}
			}
		}
	}

	set_button_state(state) {
		let btn = this.chat.layout.find('#agent-send-trigger');
		if (state === 'cancel') {
			btn.removeClass('agent-send-btn').addClass('agent-cancel-btn');
			btn.attr('title', __('Cancel Execution'));
			btn.html(`<svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>`);
			this.chat.textarea.prop('disabled', true);
		} else {
			btn.removeClass('agent-cancel-btn').addClass('agent-send-btn');
			btn.attr('title', __('Send Message'));
			btn.html(`<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`);
			btn.prop('disabled', false).css('opacity', 1);
			this.chat.textarea.prop('disabled', false);
			this.chat.textarea.focus();
		}
	}

	cancel_agent_execution() {
		let session_id = this.chat.session_manager.session_id;
		if (!session_id) return;

		let agent_email = localStorage.getItem('connected_agent_email');
		if (!agent_email) return;

		this.is_cancelled = true;
		this.chat.ui_manager.hide_typing_indicator(this.chat.msg_box);

		if (this.chat.popup_container) {
			this.chat.popup_container.hide().empty();
		}

		this.set_button_state('send');

		frappe.xcall(
			'accountant_agent.accountant_agent.page.agent_chat.agent_chat.cancel_agent',
			{ session_id: session_id, agent_email: agent_email }
		).catch(err => {
			console.error("Cancellation background request failed:", err);
		});
	}

	show_clarification_popup(questions) {
		if (!questions || questions.length === 0) return;

		this.clarification_questions = questions;
		this.clarification_index = 0;
		this.clarification_answers = {};

		this.set_button_state('cancel');
		this.render_popup_question();
	}

	render_popup_question() {
		let q = this.clarification_questions[this.clarification_index];
		let total = this.clarification_questions.length;
		let current_num = this.clarification_index + 1;

		let options_html = '';
		if (q.options && q.options.length > 0) {
			let rows = q.options.map((opt, idx) => {
				let is_active = this.clarification_answers[q.id] === opt;
				return `
					<div class="clarification-row-option ${is_active ? 'active' : ''}" data-value="${opt}">
						<span class="option-num">${idx + 1}</span>
						<span class="option-text">${opt}</span>
					</div>
				`;
			}).join('');
			options_html = `<div class="clarification-options-list" style="display: flex; flex-direction: column; gap: 8px; margin: 12px 0;">${rows}</div>`;
		}

		let custom_input_html = '';
		if (q.allow_custom !== false) {
			let is_custom_active = this.clarification_answers[q.id] && (!q.options || !q.options.includes(this.clarification_answers[q.id]));
			let custom_val = is_custom_active ? this.clarification_answers[q.id] : '';
			custom_input_html = `
				<div class="clarification-row-option custom-option-row ${is_custom_active ? 'active' : ''}" style="flex-direction: column; align-items: stretch; gap: 8px;">
					<div style="display: flex; align-items: center; gap: 10px;">
						<span class="option-num">${(q.options || []).length + 1}</span>
						<span class="option-text">${__('Other (write your answer)')}</span>
					</div>
					<input type="text" class="clarification-popup-custom-input" 
						placeholder="${__('Type your custom answer here...')}" 
						value="${custom_val}"
						style="font-size: 13px; border-radius: 6px; padding: 6px 12px; display: ${is_custom_active ? 'block' : 'none'}; width: 100%; border: 1px solid var(--chat-border); background-color: var(--chat-bg); color: var(--chat-text);" />
				</div>
			`;
		}

		let popup_html = `
			<div class="clarification-popup-header">
				<span class="clarification-popup-title">
					<i class="fa fa-question-circle" style="color: var(--chat-primary);"></i>
					${q.question}
				</span>
				<div class="clarification-popup-nav">
					<button class="clarification-nav-btn btn-prev" ${current_num === 1 ? 'disabled' : ''}>
						<i class="fa fa-chevron-left"></i>
					</button>
					<span>${current_num} ${__('of')} ${total}</span>
					<button class="clarification-nav-btn btn-next" ${current_num === total ? 'disabled' : ''}>
						<i class="fa fa-chevron-right"></i>
					</button>
				</div>
			</div>
			<div class="clarification-popup-body" style="max-height: 250px; overflow-y: auto;">
				${options_html}
				${custom_input_html}
			</div>
			<div class="clarification-popup-footer">
				<button class="clarification-popup-btn btn-skip">${__('Skip')}</button>
				<button class="clarification-popup-btn btn-continue">${current_num === total ? __('Submit') : __('Continue')}</button>
			</div>
		`;

		this.chat.popup_container.html(popup_html).show();
		this.setup_popup_question_events(q);
	}

	setup_popup_question_events(q) {
		let self = this;

		this.chat.popup_container.find('.btn-prev').on('click', (e) => {
			e.preventDefault();
			if (this.clarification_index > 0) {
				this.clarification_index--;
				this.render_popup_question();
			}
		});

		this.chat.popup_container.find('.btn-next').on('click', (e) => {
			e.preventDefault();
			if (this.clarification_index < this.clarification_questions.length - 1) {
				this.clarification_index++;
				this.render_popup_question();
			}
		});

		this.chat.popup_container.find('.clarification-row-option:not(.custom-option-row)').on('click', function(e) {
			e.preventDefault();
			let val = $(this).attr('data-value');
			self.clarification_answers[q.id] = val;
			self.chat.popup_container.find('.clarification-row-option').removeClass('active');
			$(this).addClass('active');
			self.chat.popup_container.find('.clarification-popup-custom-input').hide().val('');
		});

		this.chat.popup_container.find('.custom-option-row').on('click', function(e) {
			if ($(e.target).hasClass('clarification-popup-custom-input')) return;
			e.preventDefault();
			self.chat.popup_container.find('.clarification-row-option').removeClass('active');
			$(this).addClass('active');
			let input = $(this).find('.clarification-popup-custom-input');
			input.show().focus();
		});

		this.chat.popup_container.find('.clarification-popup-custom-input').on('input', function() {
			let val = $(this).val().trim();
			self.clarification_answers[q.id] = val;
		});

		this.chat.popup_container.find('.btn-skip').on('click', (e) => {
			e.preventDefault();
			self.clarification_answers[q.id] = '';
			self.advance_or_submit();
		});

		this.chat.popup_container.find('.btn-continue').on('click', (e) => {
			e.preventDefault();
			self.advance_or_submit();
		});
	}

	advance_or_submit() {
		if (this.clarification_index < this.clarification_questions.length - 1) {
			this.clarification_index++;
			this.render_popup_question();
		} else {
			this.submit_clarification_popup();
		}
	}

	async submit_clarification_popup() {
		let response_parts = [];
		this.clarification_questions.forEach(q => {
			let ans = this.clarification_answers[q.id] || '';
			response_parts.push(`* **${q.question}**: ${ans}`);
		});

		let response_msg = `Clarification Response:\n${response_parts.join('\n')}`;
		this.chat.popup_container.hide().empty();
		await this.send_chat_message(response_msg);
	}
}
