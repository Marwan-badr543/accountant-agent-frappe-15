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
		this.processing_sessions = new Set();
		this.cancelled_sessions = new Set();
		this.drafts = {};
		this.clarifications = {};
	}

	save_draft(session_id) {
		if (!session_id) return;
		let text = this.chat.textarea.val() || '';
		let attachments = [];
		if (this.chat.file_upload_handler) {
			attachments = [...this.chat.file_upload_handler.pending_attachments];
		}
		this.drafts[session_id] = {
			text: text,
			attachments: attachments
		};
	}

	restore_draft(session_id) {
		if (!session_id) return;
		let draft = this.drafts[session_id] || { text: '', attachments: [] };
		this.chat.textarea.val(draft.text);
		this.chat.textarea.trigger('input');

		if (this.chat.file_upload_handler) {
			this.chat.file_upload_handler.$preview_area.find('.agent-upload-preview-items').empty();
			this.chat.file_upload_handler.$preview_area.hide();
			this.chat.file_upload_handler.pending_attachments = [...draft.attachments];

			if (draft.attachments.length > 0) {
				draft.attachments.forEach(att => {
					let is_img = att.is_image;
					let type = is_img ? 'image' : 'file';
					let icon_html;
					if (type === 'image') {
						icon_html = `<img src="${att.url}" class="agent-preview-thumb" alt="${att.name}" />`;
					} else {
						icon_html = `<span class="agent-preview-icon">${this.chat.file_upload_handler._get_file_icon(att.name)}</span>`;
					}

					let $item = $(`
						<div class="agent-preview-item success" id="${att.id}" data-type="${type}" data-name="${att.name}">
							${icon_html}
							<span class="agent-preview-name" title="${att.name}">${this.chat.file_upload_handler._truncate_name(att.name, 20)}</span>
							<button class="agent-preview-remove" title="${__('Remove')}">
								<i class="fa fa-times"></i>
							</button>
						</div>
					`);

					$item.find('.agent-preview-remove').on('click', (e) => {
						e.stopPropagation();
						this.chat.file_upload_handler._remove_attachment(att.id);
					});

					this.chat.file_upload_handler.$preview_area.find('.agent-upload-preview-items').append($item);
				});
				this.chat.file_upload_handler.$preview_area.show();
			}
		}
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

		let active_session_id = session_id;

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
				await this.chat.session_manager.load_chats(false);
			} catch (e) {
				console.error("Failed to initialize chat session:", e);
				frappe.dom.unfreeze();
				return;
			} finally {
				frappe.dom.unfreeze();
			}
		}

		let sanitised_message = message;

		if (this.chat.session_manager.session_id === active_session_id) {
			if (message !== "Approve" && (!message.startsWith || !message.startsWith("Clarification Response:"))) {
				this.chat.ui_manager.append_message(this.chat.msg_box, 'user', message, false, new Date().toISOString());
			}

			// Sanitise for backend storage and network requests
			sanitised_message = frappe.utils.xss_sanitise(message);
			
			// Initialize the stream bubble immediately with "Thinking..." status
			let stream_id = `stream-${this.chat.generate_uuid()}`;
			this.chat.active_streams = this.chat.active_streams || {};
			this.chat.active_streams[active_session_id] = {
				bubble_id: stream_id,
				accumulated: "",
				reasoning: "",
				steps: [{ name: __("Thinking..."), type: 'node' }],
				status: __("Thinking..."),
				start_time: Date.now(),
				elapsed_seconds: 0
			};
			this.chat.start_stream_timer(active_session_id);
			
			this.chat.ui_manager.create_stream_bubble(this.chat.msg_box, stream_id, active_session_id);
			this.chat.ui_manager.update_stream_status(this.chat.msg_box, stream_id, __("Thinking..."), [{ name: __("Thinking..."), type: 'node' }]);
			this.set_button_state('cancel');
		}

		this.processing_sessions.add(active_session_id);
		// Falls back to 'auto', never to a named desk: without a selector the
		// server should choose, not be told the general Q&A desk was asked for.
		let agent_type = this.chat.agent_selector ? this.chat.agent_selector.get_selected_agent() : 'auto';

		try {
			let agent_email = localStorage.getItem('connected_agent_email');
			let res = await frappe.xcall(
				'accountant_agent.accountant_agent.page.agent_chat.agent_chat.send_message',
				{
					message: sanitised_message,
					session_id: active_session_id,
					agent_email: agent_email,
					agent_type: agent_type,
					file_urls: file_urls ? JSON.stringify(file_urls) : null
				}
			);

			if (this.cancelled_sessions.has(active_session_id)) {
				this.cancelled_sessions.delete(active_session_id);
				return;
			}

			if (this.chat.session_manager.session_id === active_session_id) {
				if (res && res.status === 'queued') {
					// Bubble and timer are already running, render background sidebar list and return
					this.chat.session_manager.render_chat_list();
					return;
				}

				this.set_button_state('send');
				if (res && res.response) {
					await this.chat.session_manager.load_chats(false);
					
					// If completed synchronously without streaming, finalize the existing stream bubble
					if (this.chat.active_streams && this.chat.active_streams[active_session_id]) {
						let stream = this.chat.active_streams[active_session_id];
						this.chat.ui_manager.finalize_stream_bubble(
							this.chat.msg_box,
							stream.bubble_id,
							res.response,
							new Date().toISOString(),
							__("Completed")
						);
						delete this.chat.active_streams[active_session_id];
					} else {
						await this.chat.ui_manager.append_message(this.chat.msg_box, 'ai', res.response, true, new Date().toISOString());
					}
				} else {
					await this.chat.session_manager.load_chats(false);
				}
			} else {
				await this.chat.session_manager.load_chats(false);
			}
		} catch (err) {
			if (this.cancelled_sessions.has(active_session_id)) {
				this.cancelled_sessions.delete(active_session_id);
				return;
			}

			if (this.chat.session_manager.session_id === active_session_id) {
				this.set_button_state('send');
				console.error("Message send failed:", err);
				let error_msg = err.message || '';
				if (!error_msg.includes('cancelled') && !error_msg.includes('cancellation')) {
					let final_err = error_msg || __('Unable to get response from Razyn.');
					if (this.chat.active_streams && this.chat.active_streams[active_session_id]) {
						let stream = this.chat.active_streams[active_session_id];
						this.chat.ui_manager.finalize_stream_bubble(
							this.chat.msg_box,
							stream.bubble_id,
							`⚠️ **Error:** ${final_err}`,
							new Date().toISOString(),
							__("Failed")
						);
						delete this.chat.active_streams[active_session_id];
					} else {
						this.chat.ui_manager.append_message(this.chat.msg_box, 'ai', `⚠️ **Error:** ${final_err}`);
					}
				}
			}
		} finally {
			this.processing_sessions.delete(active_session_id);
			delete this.clarifications[active_session_id];
		}
	}

	set_button_state(state) {
		let btn = this.chat.layout.find('#agent-send-trigger');
		if (state === 'cancel') {
			btn.removeClass('agent-send-btn').addClass('agent-cancel-btn');
			btn.attr('title', __('Cancel Execution'));
			btn.html(`<svg viewBox="0 0 24 24"><path d="M6 6h12v12H6z"/></svg>`);
			this.chat.textarea.prop('disabled', false);
		} else {
			btn.removeClass('agent-cancel-btn').addClass('agent-send-btn');
			btn.attr('title', __('Send Message'));
			btn.html(`<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>`);
			btn.prop('disabled', false).css('opacity', 1);
			this.chat.textarea.prop('disabled', false);
			this.chat.textarea.focus();
			this.chat.textarea.trigger('input');
		}
	}

	cancel_agent_execution() {
		let session_id = this.chat.session_manager.session_id;
		if (!session_id) return;

		let agent_email = localStorage.getItem('connected_agent_email');
		if (!agent_email) return;

		this.cancelled_sessions.add(session_id);
		this.chat.stop_stream_timer(session_id);

		if (this.chat.active_streams && this.chat.active_streams[session_id]) {
			let stream = this.chat.active_streams[session_id];
			this.chat.ui_manager.finalize_stream_bubble(
				this.chat.msg_box,
				stream.bubble_id,
				`⚠️ **Cancelled**`,
				new Date().toISOString(),
				__("Cancelled")
			);
			delete this.chat.active_streams[session_id];
		}

		this.chat.ui_manager.hide_typing_indicator(this.chat.msg_box);

		if (this.chat.popup_container) {
			this.chat.popup_container.hide().empty();
		}

		this.set_button_state('send');
		this.chat.session_manager.render_chat_list();

		frappe.xcall(
			'accountant_agent.accountant_agent.page.agent_chat.agent_chat.cancel_agent',
			{ session_id: session_id, agent_email: agent_email }
		).catch(err => {
			console.error("Cancellation background request failed:", err);
		});
	}

	show_clarification_popup(questions, session_id = null) {
		if (!session_id) {
			session_id = this.chat.session_manager.session_id;
		}
		if (!session_id || !questions || questions.length === 0) return;

		this.clarifications[session_id] = {
			questions: questions,
			index: 0,
			answers: {}
		};

		if (this.chat.session_manager.session_id === session_id) {
			this.set_button_state('cancel');
			this.render_popup_question(session_id);
		}
	}

	render_popup_question(session_id) {
		let state = this.clarifications[session_id];
		if (!state) return;

		let q = state.questions[state.index];
		let total = state.questions.length;
		let current_num = state.index + 1;

		let options_html = '';
		if (q.options && q.options.length > 0) {
			let rows = q.options.map((opt, idx) => {
				let is_active = state.answers[q.id] === opt;
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
			let is_custom_active = state.answers[q.id] && (!q.options || !q.options.includes(state.answers[q.id]));
			let custom_val = is_custom_active ? state.answers[q.id] : '';
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
		this.setup_popup_question_events(q, session_id);
	}

	setup_popup_question_events(q, session_id) {
		let self = this;
		let state = this.clarifications[session_id];
		if (!state) return;

		this.chat.popup_container.find('.btn-prev').on('click', (e) => {
			e.preventDefault();
			if (state.index > 0) {
				state.index--;
				this.render_popup_question(session_id);
			}
		});

		this.chat.popup_container.find('.btn-next').on('click', (e) => {
			e.preventDefault();
			if (state.index < state.questions.length - 1) {
				state.index++;
				this.render_popup_question(session_id);
			}
		});

		this.chat.popup_container.find('.clarification-row-option:not(.custom-option-row)').on('click', function(e) {
			e.preventDefault();
			let val = $(this).attr('data-value');
			state.answers[q.id] = val;
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
			state.answers[q.id] = val;
		});

		this.chat.popup_container.find('.btn-skip').on('click', (e) => {
			e.preventDefault();
			state.answers[q.id] = '';
			self.advance_or_submit(session_id);
		});

		this.chat.popup_container.find('.btn-continue').on('click', (e) => {
			e.preventDefault();
			self.advance_or_submit(session_id);
		});
	}

	advance_or_submit(session_id) {
		let state = this.clarifications[session_id];
		if (!state) return;

		if (state.index < state.questions.length - 1) {
			state.index++;
			this.render_popup_question(session_id);
		} else {
			this.submit_clarification_popup(session_id);
		}
	}

	async submit_clarification_popup(session_id) {
		let state = this.clarifications[session_id];
		if (!state) return;

		let response_parts = [];
		state.questions.forEach(q => {
			let ans = state.answers[q.id] || '';
			response_parts.push(`* **${q.question}**: ${ans}`);
		});

		let response_msg = `Clarification Response:\n${response_parts.join('\n')}`;
		
		if (this.chat.session_manager.session_id === session_id) {
			this.chat.popup_container.hide().empty();
		}
		
		delete this.clarifications[session_id];
		await this.send_chat_message(response_msg);
	}
}
