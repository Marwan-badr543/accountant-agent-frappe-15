/**
 * Chat Attachments Renderer Module
 * ---------------------------------
 * Parses special markers [FILE:name:url] and [IMAGE:name:url] in chat message
 * content and renders them as clickable file chips or image thumbnails.
 *
 * Usage:
 *   let renderer = new ChatAttachmentsRenderer();
 *   let { text, attachments_html } = renderer.parse_and_render(content);
 *   // text = clean message text without markers
 *   // attachments_html = HTML string of rendered attachments
 */

class ChatAttachmentsRenderer {

	constructor() {
		// Regex patterns for attachment markers
		this.FILE_PATTERN = /\[FILE:([^:]+):([^\]]+)\]/g;
		this.IMAGE_PATTERN = /\[IMAGE:([^:]+):([^\]]+)\]/g;
	}

	/**
	 * Parse message content for attachment markers and return separated text + attachment HTML.
	 *
	 * @param {string} content - Raw message content potentially containing markers.
	 * @returns {{ text: string, attachments_html: string }}
	 */
	parse_and_render(content) {
		if (!content) return { text: '', attachments_html: '' };

		// Reset regex lastIndex to 0 for fresh matches
		this.FILE_PATTERN.lastIndex = 0;
		this.IMAGE_PATTERN.lastIndex = 0;

		let files = [];
		let images = [];

		// Extract FILE markers
		let match;
		while ((match = this.FILE_PATTERN.exec(content)) !== null) {
			files.push({ name: match[1], url: match[2] });
		}

		// Extract IMAGE markers
		while ((match = this.IMAGE_PATTERN.exec(content)) !== null) {
			images.push({ name: match[1], url: match[2] });
		}

		// Remove markers from text
		let clean_text = content
			.replace(this.FILE_PATTERN, '')
			.replace(this.IMAGE_PATTERN, '')
			.trim();

		// Build attachments HTML
		let attachments_html = '';
		if (files.length > 0 || images.length > 0) {
			attachments_html = '<div class="agent-chat-attachments">';

			files.forEach(f => {
				attachments_html += this._render_file_chip(f.name, f.url);
			});

			images.forEach(img => {
				attachments_html += this._render_image_thumbnail(img.name, img.url);
			});

			attachments_html += '</div>';
		}

		return { text: clean_text, attachments_html };
	}

	/**
	 * Check if content contains any attachment markers.
	 * @param {string} content
	 * @returns {boolean}
	 */
	has_attachments(content) {
		if (!content) return false;
		// Reset regex lastIndex for fresh test
		this.FILE_PATTERN.lastIndex = 0;
		this.IMAGE_PATTERN.lastIndex = 0;
		return this.FILE_PATTERN.test(content) || this.IMAGE_PATTERN.test(content);
	}

	// ─── Private Renderers ─────────────────────────────────────────────────

	_render_file_chip(name, url) {
		let icon = this._get_file_icon(name);
		let escaped_name = this._escape_html(name);

		return `
			<div class="agent-attachment-chip file-chip unclickable" title="${escaped_name}">
				<span class="attachment-icon">${icon}</span>
				<span class="attachment-name">${this._truncate_name(escaped_name, 25)}</span>
			</div>
		`;
	}

	_render_image_thumbnail(name, url) {
		let escaped_name = this._escape_html(name);
		let icon = '🖼️'; // A different icon for images

		return `
			<div class="agent-attachment-chip image-chip unclickable" title="${escaped_name}">
				<span class="attachment-icon">${icon}</span>
				<span class="attachment-name">${this._truncate_name(escaped_name, 25)}</span>
			</div>
		`;
	}

	// ─── Helpers ───────────────────────────────────────────────────────────

	_get_file_icon(filename) {
		let ext = filename.split('.').pop().toLowerCase();
		let icon_map = {
			'pdf': '📄',
			'docx': '📝',
			'doc': '📝',
			'xlsx': '📊',
			'xls': '📊',
			'pptx': '📑',
			'ppt': '📑',
			'txt': '📃',
			'csv': '📊',
		};
		return icon_map[ext] || '📎';
	}

	_truncate_name(name, max_length) {
		if (name.length <= max_length) return name;
		let ext_idx = name.lastIndexOf('.');
		if (ext_idx === -1) return name.substring(0, max_length - 3) + '...';
		let ext = name.substring(ext_idx);
		let base = name.substring(0, max_length - ext.length - 3);
		return `${base}...${ext}`;
	}

	_escape_html(text) {
		let div = document.createElement('div');
		div.textContent = text;
		return div.innerHTML;
	}
}
