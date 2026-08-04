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
								<stop offset="0%" stop-color="#10a37f" />
								<stop offset="100%" stop-color="#0d8a6a" />
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
				<h3>${__('Welcome to Accountant Agent')}</h3>
				<p class="text-muted" style="max-width: 440px; margin: 0 auto; font-size: 14px; line-height: 1.5;">
					${__('Select your agent mode (Ask, Analyse, or Audit) and upload financial documents or ask questions directly.')}
				</p>
			</div>
		`;
		msg_box.append(welcome_html);
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

	append_message(msg_box, sender, content, animate = false, datetime = null, has_subsequent = false) {
		msg_box.find('.agent-welcome-state').remove();

		if (!content) content = '';
		let formatted_time = datetime ? this.format_time(datetime) : '';

		let attachments_html = '';
		let display_content = content;
		if (this.chat.attachments_renderer && this.chat.attachments_renderer.has_attachments(content)) {
			let parsed = this.chat.attachments_renderer.parse_and_render(content);
			display_content = parsed.text;
			attachments_html = parsed.attachments_html;
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
				this.scroll_to_bottom(msg_box);

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
			return Promise.resolve();
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
		this.scroll_to_bottom(msg_box);
	}

	hide_typing_indicator(msg_box) {
		msg_box.find('#agent-typing-row').remove();
	}

	scroll_to_bottom(msg_box) {
		msg_box.scrollTop(msg_box[0].scrollHeight);
	}

	parse_markdown(text) {
		if (!text) return '';
		let output = text
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;");

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
		temp_output = temp_output.replace(/```(.*?)```/gs, '<pre><code>$1</code></pre>');
		temp_output = temp_output.replace(/`(.*?)`/g, '<code>$1</code>');

		return temp_output;
	}
}
