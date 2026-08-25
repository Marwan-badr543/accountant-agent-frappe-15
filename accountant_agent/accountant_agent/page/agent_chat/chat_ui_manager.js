/**
 * Chat UI Manager Module
 * ----------------------
 * Handles chat layout rendering, message bubbles, typing indicators,
 * live stream typing animation, markdown parsing, and popup rendering.
 */

class ChatUIManager {
	constructor(chat_instance) {
		this.chat = chat_instance;
		this.typing_timers = [];
	}

	clear_typing_timers() {
		if (this.typing_timers && this.typing_timers.length > 0) {
			this.typing_timers.forEach(timer => clearTimeout(timer));
			this.typing_timers = [];
		}
	}

	render_welcome(msg_box) {
		msg_box.empty();
		let welcome_html = `
			<div class="agent-welcome-state">
				<div class="agent-welcome-icon">
					<svg width="64" height="64" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
						<defs>
							<linearGradient id="robotGrad" x1="0%" y1="0%" x2="100%" y2="100%">
								<stop offset="0%" stop-color="#4f46e5" />
								<stop offset="100%" stop-color="#6366f1" />
							</linearGradient>
						</defs>
						<rect x="8" y="16" width="48" height="36" rx="10" fill="url(#robotGrad)" />
						<rect x="28" y="6" width="8" height="10" fill="url(#robotGrad)" />
						<circle cx="32" cy="5" r="4" fill="url(#robotGrad)" />
						<rect x="4" y="26" width="4" height="16" rx="2" fill="url(#robotGrad)" />
						<rect x="56" y="26" width="4" height="16" rx="2" fill="url(#robotGrad)" />
						<rect x="13" y="21" width="38" height="26" rx="6" fill="#ffffff" />
						<circle cx="23" cy="30" r="4" fill="url(#robotGrad)" />
						<circle cx="41" cy="30" r="4" fill="url(#robotGrad)" />
						<path d="M22 38 Q32 43 42 38" stroke="url(#robotGrad)" stroke-width="3" stroke-linecap="round" fill="none" />
					</svg>
				</div>
				<h3>${__('Welcome to Razyyn')}</h3>
				<p class="text-muted" style="max-width: 440px; margin: 0 auto; font-size: 14px; line-height: 1.5;">
					${__('Select your agent mode (Ask, Analyse, or Audit) and upload financial documents or ask questions directly.')}
				</p>
				<div class="agent-suggestions-grid">
					<div class="agent-suggestion-card" data-prompt="${__('Compare bank statements and ledger entries to find any discrepancies.')}">
						<div class="agent-suggestion-card-header">
							<i class="fa fa-balance-scale"></i>
							${__('Reconcile bank entries')}
						</div>
						<div class="agent-suggestion-card-desc">
							${__('Compare bank statements and ledger entries to find any discrepancies.')}
						</div>
					</div>
					<div class="agent-suggestion-card" data-prompt="${__('Provide a complete summary of our cash flow status and highlight any liquidity risks.')}">
						<div class="agent-suggestion-card-header">
							<i class="fa fa-bar-chart"></i>
							${__('Analyze Cash Flow')}
						</div>
						<div class="agent-suggestion-card-desc">
							${__('Provide a complete summary of our cash flow status and highlight any liquidity risks.')}
						</div>
					</div>
					<div class="agent-suggestion-card" data-prompt="${__('Run an audit check on all expenses from the past 30 days and flag any policy violations.')}">
						<div class="agent-suggestion-card-header">
							<i class="fa fa-shield"></i>
							${__('Audit recent expenses')}
						</div>
						<div class="agent-suggestion-card-desc">
							${__('Run an audit check on all expenses from the past 30 days and flag any policy violations.')}
						</div>
					</div>
					<div class="agent-suggestion-card" data-prompt="${__('Can you list all outstanding invoices that are overdue and calculate the total amount?')}">
						<div class="agent-suggestion-card-header">
							<i class="fa fa-file-text-o"></i>
							${__('Overdue Invoices')}
						</div>
						<div class="agent-suggestion-card-desc">
							${__('Can you list all outstanding invoices that are overdue and calculate the total amount?')}
						</div>
					</div>
				</div>
			</div>
		`;
		msg_box.append(welcome_html);

		// Bind events to the suggestion cards
		let self = this;
		msg_box.find('.agent-suggestion-card').on('click', function(e) {
			let prompt = $(this).data('prompt');
			self.chat.textarea.val(prompt);
			self.chat.textarea.trigger('input');
			self.chat.textarea.focus();
		});
	}

	format_time(datetime_str) {
		if (!datetime_str) return '';
		try {
			let clean_str = typeof datetime_str === 'string' ? datetime_str.replace(' ', 'T') : datetime_str;
			let date_obj = new Date(clean_str);
			if (isNaN(date_obj.getTime())) return datetime_str;
			return date_obj.toLocaleString(undefined, {
				month: 'short', day: 'numeric',
				hour: '2-digit', minute: '2-digit', hour12: true
			});
		} catch (e) {
			return datetime_str;
		}
	}

	render_plan_card(bubble_el, content_json, datetime = null) {
		try {
			let data = typeof content_json === 'string' ? JSON.parse(content_json) : content_json;
			if (!data || data.type !== 'plan') return false;

			let plan_text = data.plan || '';
			let status = data.status || 'pending';
			let parsed_markdown = this.parse_markdown(plan_text);

			let header_id = `plan-hdr-${this.chat.generate_uuid()}`;
			let body_id = `plan-body-${this.chat.generate_uuid()}`;
			let container_id = `plan-container-${this.chat.generate_uuid()}`;
			let btn_id = `plan-btn-${this.chat.generate_uuid()}`;

			let actions_html = '';
			if (status === 'pending') {
				actions_html = `
					<div class="plan-actions-wrapper">
						<button class="plan-btn-approve" id="${btn_id}">
							<i class="fa fa-play"></i>
							<span>${__('Approve & Run')}</span>
						</button>
					</div>
				`;
			}

			let plan_html = `
				<div class="plan-card-container collapsed" id="${container_id}">
					<div class="plan-card-header" id="${header_id}">
						<div class="plan-title-wrapper">
							<i class="fa fa-list-alt" style="color: var(--chat-primary);"></i>
							<span>${__('Proposed Execution Plan')}</span>
						</div>
						<i class="fa fa-chevron-down plan-caret-icon"></i>
					</div>
					<div class="plan-card-body" id="${body_id}">
						<div class="plan-content-markdown">${parsed_markdown}</div>
						${actions_html}
					</div>
				</div>
			`;

			bubble_el.empty().append(plan_html);
			
			// Click to expand/collapse
			let container = bubble_el.find(`#${container_id}`);
			bubble_el.find(`#${header_id}`).on('click', () => {
				container.toggleClass('collapsed');
			});

			// Approve button handler
			if (status === 'pending') {
				let btn = bubble_el.find(`#${btn_id}`);
				btn.on('click', (e) => {
					e.stopPropagation();
					
					// Disable button immediately to prevent double-clicks
					btn.attr('disabled', 'disabled');
					btn.css('opacity', '0.6');
					btn.find('span').text(__('Resuming...'));
					btn.find('i').removeClass('fa-play').addClass('fa-spinner fa-spin');
					

					// Send "Approve" message to resume the agent
					this.chat.message_handler.send_chat_message("Approve");
				});
			}

			return true;
		} catch (e) {
			console.error("Error parsing plan card JSON:", e);
			return false;
		}
	}

	append_message(msg_box, sender, content, animate = false, datetime = null, has_subsequent = false) {
		msg_box.find('.agent-welcome-state').remove();

		if (!content) content = '';
		let formatted_time = datetime ? this.format_time(datetime) : '';



		let is_plan = false;
		let parsed_data = null;
		if (content) {
			if (typeof content === 'object') {
				if (content.type === 'plan') {
					is_plan = true;
					parsed_data = content;
				}
			} else if (typeof content === 'string') {
				let trimmed = content.trim();
				if (trimmed.startsWith('{') && (trimmed.includes('"type": "plan"') || trimmed.includes('"type":"plan"'))) {
					try {
						let parsed = JSON.parse(trimmed);
						if (parsed && parsed.type === 'plan') {
							is_plan = true;
							parsed_data = parsed;
						}
					} catch(e) {}
				}
			}
		}

		if (is_plan) {
			let bubble_id = `msg-${this.chat.generate_uuid()}`;
			let time_id = `time-${this.chat.generate_uuid()}`;
			let bubble_html = `
				<div class="agent-msg-row ai">
					<div class="agent-msg-bubble" id="${bubble_id}" style="background: transparent; border: none; padding: 0; box-shadow: none; max-width: 100%;">
					</div>
					${formatted_time ? `<div class="agent-msg-time" id="${time_id}" style="font-size: 10.5px; color: var(--chat-text-muted); margin-top: 4px; padding: 0 4px;">${formatted_time}</div>` : ''}
				</div>
			`;
			msg_box.append(bubble_html);
			let bubble_el = msg_box.find(`#${bubble_id}`);
			this.render_plan_card(bubble_el, parsed_data, datetime);
			this.scroll_to_bottom(msg_box);
			return Promise.resolve();
		}

		let attachments_html = '';
		let display_content = content;
		if (this.chat.attachments_renderer && this.chat.attachments_renderer.has_attachments(content)) {
			let parsed = this.chat.attachments_renderer.parse_and_render(content);
			display_content = parsed.text;
			attachments_html = parsed.attachments_html;
		}

		// A STORED QUESTION STILL OFFERS ITS ANSWERS AFTER A RELOAD.
		//
		// The picker used to be reopened by parsing the raw envelope out of
		// the transcript — which only worked because the envelope was being
		// stored, and being stored is what put `{"type": "clarification", ...`
		// in a customer's chat window. The prose is stored now, with the
		// questions packed into the block as base64, so both can be true: the
		// exchange stays readable in the transcript AND the agent, which is
		// still paused and waiting, still shows you what it is waiting for.
		//
		// Unlike the branch below this one, it does NOT return early: the
		// folded question is a real message and gets drawn like one.
		// AND ONLY WHILE IT IS STILL OPEN.
		//
		// The answer now lives INSIDE the question block, so a settled exchange
		// is still the last message in the session — `has_subsequent` is false
		// for it, and without this test the picker would reopen on a question
		// the customer answered ten minutes ago. `data-answered` is written by
		// the server when it folds the reply in.
		if (sender === 'ai' && !has_subsequent
			&& display_content.indexOf('data-questions="') !== -1
			&& display_content.indexOf('data-answered="1"') === -1) {
			try {
				let packed = display_content.match(/data-questions="([A-Za-z0-9+/=]*)"/);
				if (packed && packed[1]) {
					// Base64 -> bytes -> UTF-8, so a question written in
					// Arabic survives the round trip. `atob` alone would
					// mangle every non-Latin character.
					let bytes = Uint8Array.from(atob(packed[1]), c => c.charCodeAt(0));
					let questions = JSON.parse(new TextDecoder('utf-8').decode(bytes));
					if (Array.isArray(questions) && questions.length) {
						this.chat.show_clarification_popup(questions);
					}
				}
			} catch (e) {
				// A question we cannot reopen is not a reason to lose the
				// message: the customer can still read it and type an answer.
				console.error("Could not reopen the stored question:", e);
			}
		}

		if (sender === 'ai' && display_content.startsWith('{"type": "clarification"')) {
			try {
				let data = JSON.parse(content);
				if (data && data.type === 'clarification') {
					if (!has_subsequent) {
						this.chat.show_clarification_popup(data.questions);
					}
					return Promise.resolve();
				}
			} catch (e) {
				console.error("Failed to parse clarification message JSON:", e);
			}
		}

		if (animate && sender === 'ai') {
			return new Promise((resolve) => {
				let bubble_id = `msg-${this.chat.generate_uuid()}`;
				let time_id = `time-${this.chat.generate_uuid()}`;
				let bubble_html = `
					<div class="agent-msg-row ai">
						<div class="agent-msg-bubble typing-active" id="${bubble_id}">
							${attachments_html}
						</div>
						${formatted_time ? `<div class="agent-msg-time" id="${time_id}" style="font-size: 10.5px; color: var(--chat-text-muted); margin-top: 4px; padding: 0 4px; display: none;">${formatted_time}</div>` : ''}
					</div>
				`;

				msg_box.append(bubble_html);
				this.force_scroll_to_bottom(msg_box);

				let bubble_el = msg_box.find(`#${bubble_id}`);
				let time_el = msg_box.find(`#${time_id}`);
				let current_text = "";
				let index = 0;
				let self = this;

				let chars_per_tick = 1;
				let base_delay = 20;

				if (content.length > 3000) { chars_per_tick = 4; base_delay = 5; }
				else if (content.length > 1500) { chars_per_tick = 3; base_delay = 10; }
				else if (content.length > 600) { chars_per_tick = 2; base_delay = 15; }

				function type() {
					if (!self.typing_timers.includes(timerId)) {
						resolve();
						return;
					}

					if (index < content.length) {
						let chunk = content.slice(index, index + chars_per_tick);
						current_text += chunk;
						index += chars_per_tick;

						let parsed = self.parse_markdown(current_text);
						let text_el = bubble_el.find('.agent-msg-text-content');
						if (!text_el.length) {
							bubble_el.append('<div class="agent-msg-text-content"></div>');
							text_el = bubble_el.find('.agent-msg-text-content');
						}
						text_el.html(parsed);

						if (index % (chars_per_tick * 3) === 0 || index >= content.length) {
							self.scroll_to_bottom(msg_box);
						}

						let last_char = chunk[chunk.length - 1];
						let delay = base_delay + Math.random() * (base_delay * 0.5);
						if (last_char === ' ') delay += base_delay * 0.3;
						else if (['.', ',', '?', '!', ';'].includes(last_char)) delay += Math.min(100, base_delay * 3);
						else if (last_char === '\n') delay += Math.min(150, base_delay * 4);

						let timeout = setTimeout(type, delay);
						self.typing_timers = self.typing_timers.map(t => t === timerId ? timeout : t);
						timerId = timeout;
					} else {
						self.typing_timers = self.typing_timers.filter(t => t !== timerId);
						bubble_el.removeClass('typing-active');
						let parsed = self.parse_markdown(content);
						let text_el = bubble_el.find('.agent-msg-text-content');
						if (!text_el.length) {
							bubble_el.append('<div class="agent-msg-text-content"></div>');
							text_el = bubble_el.find('.agent-msg-text-content');
						}
						text_el.html(parsed);
						if (time_el.length) time_el.fadeIn(300);
						self.scroll_to_bottom(msg_box);
						self.post_process_rendered_bubble(msg_box);
						self.render_mermaid_diagrams(msg_box);
						self.render_chartjs_diagrams(msg_box);
						resolve();
					}
				}

				let timerId = setTimeout(type, 30);
				this.typing_timers.push(timerId);
			});
		} else {
			let parsed_content = this.parse_markdown(display_content);
			let bubble_html = `
				<div class="agent-msg-row ${sender}">
					<div class="agent-msg-bubble">
						${attachments_html}
						${parsed_content ? `<div class="agent-msg-text-content">${parsed_content}</div>` : ''}
					</div>
					${formatted_time ? `<div class="agent-msg-time" style="font-size: 10.5px; color: var(--chat-text-muted); margin-top: 4px; padding: 0 4px;">${formatted_time}</div>` : ''}
				</div>
			`;

			msg_box.append(bubble_html);
			this.scroll_to_bottom(msg_box);
			this.post_process_rendered_bubble(msg_box);
			this.render_mermaid_diagrams(msg_box);
			this.render_chartjs_diagrams(msg_box);
			return Promise.resolve();
		}
	}

	create_stream_bubble(msg_box, bubble_id, session_id) {
		this.hide_typing_indicator(msg_box);
		msg_box.find('.agent-welcome-state').remove();
		let bubble_html = `
			<div class="agent-msg-row ai" id="row-${bubble_id}" data-session-id="${session_id}">
				<div class="agent-msg-bubble streaming-active" id="${bubble_id}">
					<!-- Collapsible Thinking Wrapper -->
					<div class="agent-thinking-wrapper" style="display: none;">
						<div class="thinking-header-toggle">
							<div class="thinking-header-left">
								<span class="thinking-header-icon" style="transform: rotate(90deg);"><i class="fa fa-chevron-right"></i></span>
								<span class="thinking-header-title">${__('Thinking...')}</span>
							</div>
							<span class="thinking-header-timer">0s</span>
						</div>
						<div class="thinking-body-content" style="display: block;">
							<div class="thinking-steps-list"></div>
							<div class="thinking-reasoning-block" style="display: none;"></div>
						</div>
					</div>
					<!-- Main Response Content -->
					<div class="agent-msg-text-content"></div>
				</div>
				<div class="agent-msg-time" style="font-size: 10.5px; color: var(--chat-text-muted); margin-top: 4px; padding: 0 4px; display: none;"></div>
			</div>
		`;
		msg_box.append(bubble_html);
		this.scroll_to_bottom(msg_box);

		// Accordion toggle click handler
		let row = msg_box.find(`#row-${bubble_id}`);
		row.find('.thinking-header-toggle').on('click', () => {
			let body = row.find('.thinking-body-content');
			let icon = row.find('.thinking-header-icon');
			if (body.is(':visible')) {
				body.slideUp(150);
				icon.css('transform', 'rotate(0deg)');
			} else {
				body.slideDown(150);
				icon.css('transform', 'rotate(90deg)');
			}
		});
	}

	update_stream_bubble(msg_box, bubble_id, content) {
		this.hide_typing_indicator(msg_box);
		let bubble_el = msg_box.find(`#${bubble_id}`);
		if (bubble_el.length) {
			let text_el = bubble_el.find('.agent-msg-text-content');
			let msg_box_was_near_bottom = this.is_near_bottom(msg_box);
			let parsed = this.parse_markdown(content);
			text_el.html(parsed);
			if (msg_box_was_near_bottom) {
				this.force_scroll_to_bottom(msg_box);
			}
		}
	}

	update_stream_status(msg_box, bubble_id, status_text, steps = []) {
		this.hide_typing_indicator(msg_box);
		let bubble_el = msg_box.find(`#${bubble_id}`);
		if (bubble_el.length) {
			let steps_list = bubble_el.find('.thinking-steps-list');
			let msg_box_was_near_bottom = this.is_near_bottom(msg_box);
			steps_list.empty();
			
			if (steps && steps.length > 0) {
				steps.forEach((step, idx) => {
					let is_last = (idx === steps.length - 1);
					let icon_class = is_last ? 'fa-cog fa-spin' : 'fa-check';
					let icon_color = is_last ? 'var(--chat-primary)' : '#10a37f';
					steps_list.append(`
						<div class="thinking-step-item">
							<i class="fa ${icon_class}" style="color: ${icon_color}; font-size: 11px;"></i>
							<span>${step.name}</span>
						</div>
					`);
				});
			} else if (status_text) {
				steps_list.append(`
					<div class="thinking-step-item">
						<i class="fa fa-cog fa-spin" style="color: var(--chat-primary); font-size: 11px;"></i>
						<span>${status_text}</span>
					</div>
				`);
			}
			bubble_el.find('.agent-thinking-wrapper').show();
			if (msg_box_was_near_bottom) {
				this.force_scroll_to_bottom(msg_box);
			}
		}
	}

	update_stream_reasoning(msg_box, bubble_id, reasoning_text) {
		this.hide_typing_indicator(msg_box);
		let bubble_el = msg_box.find(`#${bubble_id}`);
		if (bubble_el.length) {
			let reasoning_block = bubble_el.find('.thinking-reasoning-block');
			let body_content = bubble_el.find('.thinking-body-content');

			let msg_box_was_near_bottom = this.is_near_bottom(msg_box);
			let body_was_near_bottom = this.is_near_bottom(body_content);

			let parsed = this.parse_markdown(reasoning_text);
			reasoning_block.html(parsed).show();
			bubble_el.find('.agent-thinking-wrapper').show();

			if (body_was_near_bottom && body_content.length) {
				body_content.scrollTop(body_content[0].scrollHeight);
			}
			if (msg_box_was_near_bottom) {
				this.force_scroll_to_bottom(msg_box);
			}
		}
	}

	update_thinking_duration(msg_box, bubble_id, seconds) {
		let bubble_el = msg_box.find(`#${bubble_id}`);
		if (bubble_el.length) {
			bubble_el.find('.thinking-header-timer').text(`${seconds}s`);
		}
	}

	finalize_stream_bubble(msg_box, bubble_id, content, datetime, header_title) {
		this.hide_typing_indicator(msg_box);
		let bubble_el = msg_box.find(`#${bubble_id}`);
		let row_el = msg_box.find(`#row-${bubble_id}`);
		if (bubble_el.length) {
			bubble_el.removeClass('streaming-active');
			
			// Turn all step icons to checkmarks
			let steps_list = bubble_el.find('.thinking-steps-list');
			steps_list.find('.thinking-step-item i').removeClass('fa-cog fa-spin').addClass('fa-check').css('color', '#10a37f');
			
			if (header_title) {
				bubble_el.find('.thinking-header-title').text(header_title);
			}
			
			// Auto collapse accordion to clean the page but keep it toggleable
			let body = bubble_el.find('.thinking-body-content');
			let icon = bubble_el.find('.thinking-header-icon');
			body.slideUp(150);
			icon.css('transform', 'rotate(0deg)');



			let is_plan = false;
			let parsed_data = null;
			if (content) {
				if (typeof content === 'object') {
					if (content.type === 'plan') {
						is_plan = true;
						parsed_data = content;
					}
				} else if (typeof content === 'string') {
					let trimmed = content.trim();
					if (trimmed.startsWith('{') && (trimmed.includes('"type": "plan"') || trimmed.includes('"type":"plan"'))) {
						try {
							let parsed = JSON.parse(trimmed);
							if (parsed && parsed.type === 'plan') {
								is_plan = true;
								parsed_data = parsed;
							}
						} catch(e) {}
					}
				}
			}

			if (is_plan) {
				bubble_el.css({
					'background': 'transparent',
					'border': 'none',
					'padding': '0',
					'box-shadow': 'none',
					'max-width': '100%'
				});
				this.render_plan_card(bubble_el, parsed_data, datetime);
				bubble_el.find('.agent-thinking-wrapper').hide();
			} else {
				let text_el = bubble_el.find('.agent-msg-text-content');
				let parsed = this.parse_markdown(content);
				text_el.html(parsed);
			}

			if (datetime) {
				let formatted_time = this.format_time(datetime);
				let time_el = row_el.find('.agent-msg-time');
				if (time_el.length) {
					time_el.text(formatted_time).fadeIn(300);
				}
			}

			this.force_scroll_to_bottom(msg_box);
			this.post_process_rendered_bubble(msg_box);
			this.render_mermaid_diagrams(msg_box);
			this.render_chartjs_diagrams(msg_box);
		}
	}

	show_typing_indicator(msg_box) {
		this.hide_typing_indicator(msg_box);
		let indicator_html = `
			<div class="agent-msg-row ai" id="agent-typing-row">
				<div class="agent-msg-bubble">
					<div class="agent-typing-indicator">
						<div class="agent-typing-dot"></div>
						<div class="agent-typing-dot"></div>
						<div class="agent-typing-dot"></div>
					</div>
				</div>
			</div>
		`;
		msg_box.append(indicator_html);
		this.force_scroll_to_bottom(msg_box);
	}

	hide_typing_indicator(msg_box) {
		msg_box.find('#agent-typing-row').remove();
	}

	is_near_bottom(el, threshold = 60) {
		if (!el || !el.length || !el[0]) return false;
		let dom_el = el[0];
		return (dom_el.scrollHeight - dom_el.scrollTop - dom_el.clientHeight) <= threshold;
	}

	scroll_to_bottom(msg_box) {
		if (!msg_box || !msg_box[0]) return;
		let el = msg_box[0];
		let is_at_bottom = this.is_near_bottom(msg_box, 100);
		if (is_at_bottom) {
			msg_box.scrollTop(el.scrollHeight);
		}
	}

	force_scroll_to_bottom(msg_box) {
		if (!msg_box || !msg_box[0]) return;
		msg_box.scrollTop(msg_box[0].scrollHeight);
	}

	render_mermaid_diagrams(container) {
		if (typeof mermaid === 'undefined') return;
		try {
			mermaid.initialize(this.get_mermaid_config());
		} catch (err) {
			console.error("Mermaid initialization error:", err);
		}
		let self = this;
		container.find('.mermaid-container[data-processed="false"]').each(function() {
			let $this = $(this);
			let code = decodeURIComponent($this.attr('data-code'));
			
			// Remove Mermaid comment lines (starting with %%) and strip trailing whitespace/newlines
			let clean_code = code.replace(/%%.*$/gm, '').trim();

			if (!clean_code) {
				$this.attr('data-processed', 'true');
				$this.hide();
				return;
			}
			
			// Sanitize transition labels inside |label| to prevent parentheses and brackets from breaking Mermaid parser
			code = code.replace(/\|([^|\n\r]+)\|/g, function(match, label) {
				let trimmed = label.trim();
				if ((trimmed.includes('(') || trimmed.includes(')') || trimmed.includes('[') || trimmed.includes(']') || trimmed.includes('{') || trimmed.includes('}')) && 
					!(trimmed.startsWith('"') && trimmed.endsWith('"'))) {
					return `|"${trimmed.replace(/"/g, '\\"')}"|`;
				}
				return match;
			});

			// Sanitize unquoted node labels to prevent syntax errors on special characters/spaces
			// 1. Double brackets: A[[label]] -> A[["label"]]
			code = code.replace(/\b([a-zA-Z0-9_-]+)\[\[([^"\]\n\r]+)\]\]/g, '$1[["$2"]]');
			// 2. Double parentheses: A((label)) -> A(("label"))
			code = code.replace(/\b([a-zA-Z0-9_-]+)\(\(([^"\)\n\r]+)\)\)/g, '$1(("$2"))');
			// 3. Stadium: A([label]) -> A(["label"])
			code = code.replace(/\b([a-zA-Z0-9_-]+)\(\[([^"\]\n\r]+)\]\)/g, '$1(["$2"])');
			// 4. Cylinder: A[(label)] -> A([\"label\"])
			code = code.replace(/\b([a-zA-Z0-9_-]+)\[\(([^"\)\n\r]+)\)\]/g, '$1[("$2")]');
			// 5. Rectangular: A[label] -> A["label"]
			code = code.replace(/\b([a-zA-Z0-9_-]+)\[([^"\]\n\r]+)\]/g, '$1["$2"]');
			// 6. Round: A(label) -> A("label")
			code = code.replace(/\b([a-zA-Z0-9_-]+)\(([^"\)\n\r]+)\)/g, '$1("$2")');
			// 7. Curly: A{label} -> A{"label"}
			code = code.replace(/\b([a-zA-Z0-9_-]+)\{([^"\}\n\r]+)\}/g, '$1{"$2"}');
			// 8. Asymmetric: A>label] -> A>"label"]
			code = code.replace(/\b([a-zA-Z0-9_-]+)\>([^"\]\n\r]+)\]/g, '$1>"$2"]');

			$this.attr('data-processed', 'true');
			let id = 'mermaid-' + self.chat.generate_uuid();
			try {
				mermaid.render(id, code).then(({ svg }) => {
					$this.html(svg);
					self.scroll_to_bottom(container);
				}).catch(err => {
					console.error("Mermaid render error:", err);
					$this.html(`<pre style="color: var(--chat-cancel, #e11d48); background-color: var(--ai-bubble); padding: 10px; border-radius: 6px; font-size: 11px;">Error rendering chart: ${err.message || err}</pre>`);
					$('#d' + id).remove();
				});
			} catch (e) {
				console.error("Mermaid exception:", e);
				$this.html(`<pre style="color: var(--chat-cancel, #e11d48); background-color: var(--ai-bubble); padding: 10px; border-radius: 6px; font-size: 11px;">Error rendering chart: ${e.message || e}</pre>`);
			}
		});
	}

	render_chartjs_diagrams(container) {
		let self = this;
		if (typeof Chart === 'undefined') {
			// Poll CDN download every 100ms until loaded
			setTimeout(() => self.render_chartjs_diagrams(container), 100);
			return;
		}

		container.find('.chartjs-container[data-processed="false"]').each(function() {
			let $this = $(this);
			let code = decodeURIComponent($this.attr('data-code'));
			
			// Defensive formatting cleanup
			let cleanCode = code.trim();
			cleanCode = cleanCode.replace(/,\s*([\]}])/g, '$1'); // Clean trailing commas
			cleanCode = cleanCode.replace(/^```json\s*/i, '').replace(/```$/, '');

			$this.attr('data-processed', 'true');

			try {
				let chartConfig = JSON.parse(cleanCode);

				if (!chartConfig.type) chartConfig.type = 'bar';
				if (!chartConfig.data) chartConfig.data = { labels: [], datasets: [] };
				if (!chartConfig.data.datasets) chartConfig.data.datasets = [];

				// Palette definition
				const palette = [
					'#10a37f', // Emerald Green
					'#3b82f6', // Ocean Blue
					'#f59e0b', // Amber Yellow
					'#8b5cf6', // Indigo
					'#ec4899', // Pink
					'#ef4444', // Red
					'#06b6d4', // Cyan
					'#14b8a6'  // Teal
				];
				const hoverPalette = [
					'#0d8a6a',
					'#2563eb',
					'#d97706',
					'#7c3aed',
					'#db2777',
					'#dc2626',
					'#0891b2',
					'#0d9488'
				];

				// Intercept & Auto-Theme Datasets
				chartConfig.data.datasets.forEach((dataset, idx) => {
					if (['pie', 'doughnut', 'polarArea'].includes(chartConfig.type)) {
						const dataLen = dataset.data ? dataset.data.length : 0;
						dataset.backgroundColor = palette.slice(0, dataLen);
						dataset.hoverBackgroundColor = hoverPalette.slice(0, dataLen);
						dataset.borderColor = '#ffffff';
						dataset.borderWidth = 2;
					} else {
						const color = palette[idx % palette.length];
						const hoverColor = hoverPalette[idx % hoverPalette.length];
						dataset.backgroundColor = color;
						dataset.borderColor = color;

						if (['line', 'radar'].includes(chartConfig.type)) {
							dataset.fill = dataset.fill || false;
							dataset.tension = 0.3;
							dataset.backgroundColor = color + '22';
							dataset.pointBackgroundColor = color;
							dataset.pointBorderColor = '#ffffff';
							dataset.pointHoverBackgroundColor = '#ffffff';
							dataset.pointHoverBorderColor = color;
						} else if (chartConfig.type === 'bar') {
							dataset.hoverBackgroundColor = hoverColor;
							dataset.borderRadius = 6;
						}
					}
				});

				// Auto Merge Responsive Configuration Options
				const defaultOptions = {
					responsive: true,
					maintainAspectRatio: false,
					plugins: {
						legend: {
							display: true,
							position: 'bottom',
							labels: {
								boxWidth: 12,
								usePointStyle: true,
								font: {
									family: 'Inter, sans-serif',
									size: 11
								},
								color: '#4b5563'
							}
						}
					}
				};
				chartConfig.options = $.extend(true, {}, defaultOptions, chartConfig.options || {});

				// Safely destroy existing chart instance on reuse
				let canvasEl = $this.find('canvas')[0];
				if (canvasEl) {
					const existingChart = Chart.getChart(canvasEl);
					if (existingChart) {
						existingChart.destroy();
					}
				}

				// Build canvas and mount chart instance
				$this.empty().html('<canvas style="width:100% !important; height:100% !important;"></canvas>');
				canvasEl = $this.find('canvas')[0];
				new Chart(canvasEl, chartConfig);
				self.scroll_to_bottom(container);
			} catch (err) {
				console.error("Chart.js render error:", err);
				$this.html(`<pre style="color: var(--chat-cancel, #e11d48); background-color: var(--ai-bubble); padding: 10px; border-radius: 6px; font-size: 11px;">Error parsing chart data: ${err.message || err}</pre>`);
			}
		});
	}

	parse_markdown(text) {
		if (!text) return '';

		if (window.marked) {
			try {
				return window.marked.parse(text);
			} catch (err) {
				console.error("Marked parsing error:", err);
			}
		}

		// Fallback: Crude basic parsing (original logic)
		let output = text
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;");

		// A FOLDED QUESTION MUST STILL FOLD WITH NO MARKDOWN LIBRARY.
		//
		// marked.js is fetched from a CDN, so this branch runs whenever that
		// fetch failed — offline, blocked, or a slow first paint. Everything
		// is escaped above, which is right for every tag except the two that
		// carry a stored question: without this the customer reads a literal
		// "<details>" in their chat instead of a heading they can open.
		//
		// Restored by exact match, so no attribute and no other tag can ride
		// in with them. The span is the carrier for a question that had
		// nothing to fold — invisible, and it must stay invisible here too:
		// left escaped, the customer reads a tag where their question should
		// be, which is the exact complaint this whole path exists to answer.
		output = output
			.replace(/&lt;span class="agent-question-data" data-questions="([A-Za-z0-9+/=]*)"&gt;&lt;\/span&gt;/g,
				'<span class="agent-question-data" data-questions="$1"></span>')
			// The settled fold carries `data-answered`, which is how the picker
			// below tells an open question from a finished exchange. Matched
			// before the plain one so the flag is not stripped off it.
			.replace(/&lt;details class="agent-question" data-questions="([A-Za-z0-9+/=]*)" data-answered="1"&gt;/g,
				'<details class="agent-question" data-questions="$1" data-answered="1">')
			.replace(/&lt;details class="agent-question" data-questions="([A-Za-z0-9+/=]*)"&gt;/g,
				'<details class="agent-question" data-questions="$1">')
			.replace(/&lt;details class="agent-question"&gt;/g,
				'<details class="agent-question">')
			// WHAT THEY ANSWERED, inside the question that asked it. The class is
			// what the renderer and the server both match on; the label inside it
			// is translated and neither of them reads it.
			.replace(/&lt;span class="agent-answer"&gt;/g, '<span class="agent-answer">')
			.replace(/&lt;\/span&gt;/g, '</span>')
			.replace(/&lt;\/details&gt;/g, '</details>')
			.replace(/&lt;summary&gt;/g, '<summary>')
			.replace(/&lt;\/summary&gt;/g, '</summary>');

		let lines = output.split('\n');
		let in_table = false;
		let table_html = '';
		let updated_lines = [];

		for (let i = 0; i < lines.length; i++) {
			let line = lines[i].trim();
			if (line.startsWith('|') && line.endsWith('|')) {
				if (!in_table) {
					in_table = true;
					table_html = '<table><thead>';
					let headers = line.split('|')
						.map(x => x.trim())
						.filter((x, index, arr) => index > 0 && index < arr.length - 1);
					table_html += '<tr>' + headers.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>';
				} else if (line.includes('---') || line.includes('-:-')) {
					continue;
				} else {
					let cells = line.split('|')
						.map(x => x.trim())
						.filter((x, index, arr) => index > 0 && index < arr.length - 1);
					table_html += '<tr>' + cells.map(c => `<td>${c}</td>`).join('') + '</tr>';
				}
			} else {
				if (in_table) {
					table_html += '</tbody></table>';
					updated_lines.push(table_html);
					in_table = false;
					table_html = '';
				}
				updated_lines.push(line);
			}
		}

		if (in_table) {
			table_html += '</tbody></table>';
			updated_lines.push(table_html);
		}

		output = updated_lines.join('<br>');
		let temp_output = output;

		let code_block_count = (temp_output.match(/```/g) || []).length;
		if (code_block_count % 2 !== 0) temp_output += '\n```';

		let bold_count = (temp_output.match(/\*\*/g) || []).length;
		if (bold_count % 2 !== 0) temp_output += '**';

		temp_output = temp_output.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
		
		// Parse Mermaid diagrams first
		temp_output = temp_output.replace(/```mermaid\s*(?:<br>)?([\s\S]*?)(?:<br>)?```/gi, function(match, code) {
			let raw_code = code.replace(/<br\s*\/?>/gi, '\n');
			raw_code = raw_code
				.replace(/&amp;/g, '&')
				.replace(/&lt;/g, '<')
				.replace(/&gt;/g, '>')
				.replace(/&quot;/g, '"');
			let escaped_code = encodeURIComponent(raw_code.trim());
			return `<div class="mermaid-container" data-processed="false" data-code="${escaped_code}"></div>`;
		});

		// Parse Chart.js diagrams
		temp_output = temp_output.replace(/```chartjs\s*(?:<br>)?([\s\S]*?)(?:<br>)?```/gi, function(match, code) {
			let raw_code = code.replace(/<br\s*\/?>/gi, '\n');
			raw_code = raw_code
				.replace(/&amp;/g, '&')
				.replace(/&lt;/g, '<')
				.replace(/&gt;/g, '>')
				.replace(/&quot;/g, '"');
			let escaped_code = encodeURIComponent(raw_code.trim());
			return `<div class="chartjs-container" data-processed="false" data-code="${escaped_code}"></div>`;
		});

		temp_output = temp_output.replace(/```(.*?)```/gs, '<pre><code>$1</code></pre>');
		temp_output = temp_output.replace(/`(.*?)`/g, '<code>$1</code>');

		return temp_output;
	}

	post_process_rendered_bubble(container) {
		let self = this;
		
		// 1. Wrap tables in responsive div and align columns
		container.find('table').each(function() {
			let table = $(this);
			
			// Prevent double-wrapping
			if (!table.parent().hasClass('agent-table-wrapper')) {
				table.wrap('<div class="agent-table-wrapper"></div>');
			}
			
			// Detect numeric columns dynamically
			let first_row = table.find('tr:first');
			if (first_row.length) {
				let col_count = first_row.find('th, td').length;
				let is_numeric_col = new Array(col_count).fill(true);
				
				let rows = table.find('tbody tr');
				if (rows.length === 0) {
					rows = table.find('tr').slice(1); // skip first row
				}
				
				rows.each(function() {
					$(this).find('td').each(function(i) {
						let text = $(this).text().trim().replace(/[\$,€,£,¥]/g, '').trim();
						if (text && !/^-?[\d,\.\s%]+$/.test(text)) {
							is_numeric_col[i] = false;
						}
					});
				});
				
				// Apply right alignment and numeric classes
				table.find('tr').each(function() {
					$(this).find('th, td').each(function(i) {
						if (is_numeric_col[i]) {
							$(this).css('text-align', 'right');
							$(this).addClass('font-numeric');
						}
					});
				});
			}

			// Style total / balance rows
			table.find('tr').each(function() {
				let row = $(this);
				let row_text = row.text().toLowerCase();
				if (row_text.includes('total') || row_text.includes('balance') || row_text.includes('reconciliation difference')) {
					row.addClass('table-total-row');
				}
			});
		});
	}

	get_mermaid_config() {
		let is_dark = document.documentElement.getAttribute('data-theme') === 'dark' || 
		              document.body.getAttribute('data-theme') === 'dark' || 
		              $('body').attr('data-theme') === 'dark';
		
		if (is_dark) {
			return {
				startOnLoad: false,
				theme: 'dark',
				themeVariables: {
					primaryColor: '#312e81', // Dark Indigo
					primaryTextColor: '#f8fafc', // Light slate text
					nodeTextColor: '#f8fafc',
					primaryBorderColor: '#4f46e5', // Indigo border
					lineColor: '#94a3b8', // Light slate lines
					textColor: '#f8fafc',
					background: '#0f172a'
				}
			};
		} else {
			return {
				startOnLoad: false,
				theme: 'base',
				themeVariables: {
					primaryColor: '#e0e7ff', // Light Indigo
					primaryTextColor: '#0f172a', // Dark slate text
					nodeTextColor: '#0f172a',
					primaryBorderColor: '#4f46e5', // Indigo border
					lineColor: '#64748b', // Cool gray lines
					textColor: '#0f172a',
					background: '#ffffff'
				}
			};
		}
	}
}
