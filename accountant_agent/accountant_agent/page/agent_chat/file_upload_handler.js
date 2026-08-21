/**
 * File Upload Handler Module (Refactored)
 * ----------------------------------------
 * Manages file/image selection UI, client-side image compression,
 * file uploading to the Frappe server, and agent-specific file size/type validation.
 *
 * Rules:
 *   - Whitelisted safe file extensions only (accountant safe types).
 *   - File count and size budgets come from the selected agent's entry in
 *     AgentSelector.AGENT_DEFINITIONS, which mirrors that agent's AgentSettings
 *     on the server. Adding an agent therefore needs no change to this file.
 *   - Agents with is_aggregate = false (Auto) enforce a per-file ceiling.
 *     Agents with is_aggregate = true enforce separate aggregate budgets for
 *     Excel and non-Excel attachments.
 */

class FileUploadHandler {
	constructor(chat_instance) {
		this.chat = chat_instance;
		this.$container = null;
		this.$textarea = null;
		this.$preview_area = null;
		this.$attach_btn = null;

		// Pending attachments array: [{ id, name, url, size, is_image, is_excel, file }]
		this.pending_attachments = [];

		// Processing lock
		this.is_processing = false;

		// Every document, data and image type an accountant legitimately
		// sends, and nothing that carries executable code.
		//
		// This list MUST stay in step with ALLOWED_ACCOUNTANT_EXTENSIONS in
		// agent_chat.py. That is the one that actually protects the server;
		// this one exists so the file picker filters sensibly and a refusal
		// happens before a 100 MB upload rather than after it.
		//
		// Deliberately absent: source and script files, executables, and
		// macro-enabled Office formats (.xlsm .xlsb .docm .pptm), which are
		// spreadsheets that run code when opened. Also absent is markup a
		// browser executes (.html .svg), because rendering is execution.
		this.ALLOWED_EXTENSIONS = new Set([
			// Portable documents and word processing
			'.pdf', '.doc', '.docx', '.odt', '.rtf',
			// Spreadsheets, macro-free
			'.xls', '.xlsx', '.ods',
			// Presentations
			'.ppt', '.pptx', '.odp',
			// Plain text, notes and structured data
			'.txt', '.md', '.markdown', '.rst', '.log', '.csv', '.tsv', '.psv',
			'.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.toml', '.ini',
			'.cfg', '.conf', '.xml',
			// Accounting and banking interchange formats
			'.ofx', '.qfx', '.qbo', '.qif', '.mt940', '.sta', '.camt', '.aba',
			'.bai', '.bai2', '.edi', '.x12', '.iif', '.xbrl', '.ubl', '.dat',
			// Correspondence attached as evidence
			'.eml', '.msg', '.mbox', '.ics', '.vcf',
			// Images and scans
			'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff',
			'.heic', '.heif', '.avif',
			// Unpacked server-side; only permitted types inside survive.
			'.zip'
		]);

		this.EXCEL_EXTENSIONS = new Set(['.xlsx', '.xls', '.ods']);
		this.IMAGE_EXTENSIONS = new Set([
			'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff',
			'.heic', '.heif', '.avif'
		]);
	}

	// ─── Initialization ────────────────────────────────────────────────────
	init($input_container, $textarea) {
		this.$container = $input_container;
		this.$textarea = $textarea;

		this._render_attach_button();
		this._render_preview_area();
		this._bind_events();
	}

	// ─── UI Rendering ──────────────────────────────────────────────────────
	_render_attach_button() {
		this.$attach_btn = $(`
			<button class="agent-attach-btn" type="button" title="${__('Attach files or images (up to 5 files)')}">
				<svg viewBox="0 0 24 24" width="20" height="20">
					<path d="M16.5 6v11.5a4 4 0 0 1-8 0V5a2.5 2.5 0 0 1 5 0v10.5a1 1 0 0 1-2 0V6h-1v9.5a2 2 0 0 0 4 0V5a3.5 3.5 0 0 0-7 0v12.5a5 5 0 0 0 10 0V6h-1z" fill="currentColor"/>
				</svg>
			</button>
		`);

		let $flex_row = this.$container.find('.agent-input-footer-left').first();
		if (!$flex_row.length) {
			$flex_row = this.$container.find('div[style*="display: flex"]').first();
		}
		if ($flex_row.length) {
			$flex_row.prepend(this.$attach_btn);
		}
	}

	_render_preview_area() {
		this.$preview_area = $(`
			<div class="agent-upload-preview-area" style="display: none;">
				<div class="agent-upload-preview-items"></div>
			</div>
		`);

		let $flex_row = this.$container.find('.agent-input-footer').first();
		if (!$flex_row.length) {
			$flex_row = this.$container.find('div[style*="display: flex"]').first();
		}
		if ($flex_row.length) {
			$flex_row.before(this.$preview_area);
		}
	}

	// ─── Event Bindings ────────────────────────────────────────────────────
	_bind_events() {
		this.$attach_btn.on('click', () => {
			if (this.is_processing) return;
			this._open_file_picker();
		});

		this.$textarea.on('dragover', (e) => {
			e.preventDefault();
			e.stopPropagation();
			this.$textarea.addClass('agent-drag-over');
		});

		this.$textarea.on('dragleave drop', (e) => {
			e.preventDefault();
			e.stopPropagation();
			this.$textarea.removeClass('agent-drag-over');

			if (e.type === 'drop') {
				let files = e.originalEvent.dataTransfer.files;
				if (files && files.length > 0) {
					this._handle_files(files);
				}
			}
		});
	}

	_open_file_picker() {
		let accept_pattern = Array.from(this.ALLOWED_EXTENSIONS).join(',');
		let $input = $(`<input type="file" multiple accept="${accept_pattern}" />`);
		$input.on('change', (e) => {
			let files = e.target.files;
			if (files && files.length > 0) {
				this._handle_files(files);
			}
		});
		$input.trigger('click');
	}

	// ─── Validation Helpers ─────────────────────────────────────────────────

	_get_file_ext(filename) {
		return filename.substring(filename.lastIndexOf('.')).toLowerCase();
	}

	_is_allowed_type(filename) {
		let ext = this._get_file_ext(filename);
		return this.ALLOWED_EXTENSIONS.has(ext);
	}

	_is_excel(filename) {
		let ext = this._get_file_ext(filename);
		return this.EXCEL_EXTENSIONS.has(ext);
	}

	_is_image(filename) {
		let ext = this._get_file_ext(filename);
		return this.IMAGE_EXTENSIONS.has(ext);
	}

	/**
	 * Validates candidate file batch against the selected agent's rules.
	 *
	 * Limits are read from AgentSelector.AGENT_DEFINITIONS rather than repeated
	 * here. They were previously hardcoded, which let the page/ and public/
	 * copies of this file drift apart (10 MB vs 15 MB for the same agent) and
	 * meant every new agent silently inherited the wrong budget.
	 */
	_validate_batch(incoming_files) {
		let selector = this.chat && this.chat.agent_selector ? this.chat.agent_selector : null;
		let agent_type = selector ? selector.get_selected_agent() : 'ask';

		// Fall back to the most restrictive profile if the selector is missing,
		// so a UI failure can never widen an upload budget.
		let rules = (selector && typeof selector.get_rules === 'function' && selector.get_rules())
			|| { max_files: 5, max_per_file_mb: 1, max_non_excel_total_mb: 1, max_excel_total_mb: 1, is_aggregate: false };

		let agent_name = (selector && selector.AGENT_DEFINITIONS && selector.AGENT_DEFINITIONS[agent_type])
			? selector.AGENT_DEFINITIONS[agent_type].name
			: agent_type;

		// 1. Total file count
		let total_count = this.pending_attachments.length + incoming_files.length;
		if (total_count > rules.max_files) {
			frappe.show_alert({
				message: __('Maximum {0} files allowed for {1}. You currently have {2} attached and tried to add {3}.',
					[rules.max_files, agent_name, this.pending_attachments.length, incoming_files.length]),
				indicator: 'orange'
			}, 7);
			return false;
		}

		// 2. Extension check
		for (let file of incoming_files) {
			if (!this._is_allowed_type(file.name)) {
				let ext = this._get_file_ext(file.name);
				frappe.show_alert({
					message: __('File "{0}" has unpermitted type ({1}). Only standard accounting document types are allowed.',
						[file.name, ext]),
					indicator: 'red'
				}, 7);
				return false;
			}
		}

		// 3. Size validation
		if (!rules.is_aggregate) {
			// Per-file budget (Auto agent).
			let max_bytes = rules.max_per_file_mb * 1024 * 1024;
			for (let file of incoming_files) {
				if (file.size > max_bytes) {
					frappe.show_alert({
						message: __('"{0}" is {1} MB. For {2}, each file must not exceed {3} MB.',
							[file.name, (file.size / (1024 * 1024)).toFixed(2), agent_name, rules.max_per_file_mb]),
						indicator: 'orange'
					}, 7);
					return false;
				}
			}
			return true;
		}

		// Aggregate budgets, tracked separately for Excel and non-Excel.
		let current_non_excel = 0;
		let current_excel = 0;
		this.pending_attachments.forEach(att => {
			if (att.is_excel) current_excel += att.size;
			else current_non_excel += att.size;
		});

		let new_non_excel = 0;
		let new_excel = 0;
		for (let file of incoming_files) {
			if (this._is_excel(file.name)) new_excel += file.size;
			else new_non_excel += file.size;
		}

		let max_non_excel_bytes = rules.max_non_excel_total_mb * 1024 * 1024;
		let max_excel_bytes = rules.max_excel_total_mb * 1024 * 1024;

		if ((current_non_excel + new_non_excel) > max_non_excel_bytes) {
			let total_mb = ((current_non_excel + new_non_excel) / (1024 * 1024)).toFixed(2);
			frappe.show_alert({
				message: __('Total non-Excel files ({0} MB) exceed the {1} MB aggregate limit for {2}.',
					[total_mb, rules.max_non_excel_total_mb, agent_name]),
				indicator: 'orange'
			}, 7);
			return false;
		}

		if ((current_excel + new_excel) > max_excel_bytes) {
			let total_mb = ((current_excel + new_excel) / (1024 * 1024)).toFixed(2);
			frappe.show_alert({
				message: __('Total Excel files ({0} MB) exceed the {1} MB aggregate limit for {2}.',
					[total_mb, rules.max_excel_total_mb, agent_name]),
				indicator: 'orange'
			}, 7);
			return false;
		}

		return true;
	}

	// ─── File Handling Pipeline ────────────────────────────────────────────
	async _handle_files(file_list) {
		if (this.is_processing) return;

		let files_array = Array.from(file_list);

		// Validate batch rules first
		if (!this._validate_batch(files_array)) {
			return;
		}

		for (let file of files_array) {
			await this._process_and_upload(file);
		}
	}

	async _process_and_upload(file) {
		this.is_processing = true;
		let is_img = this._is_image(file.name);
		let is_exc = this._is_excel(file.name);
		let preview_id = this._add_preview_item(file.name, is_img ? 'image' : 'file', 'uploading', file);

		try {
			let upload_file = file;

			// If image, compress client-side first
			if (is_img) {
				try {
					let compressed_blob = await this._compress_image_client(file);
					upload_file = new File([compressed_blob], file.name, { type: 'image/jpeg' });
				} catch (img_err) {
					console.warn("Client image compression fallback to raw file:", img_err);
				}
			}

			// Upload file directly to Frappe endpoint
			let upload_result = await this._upload_to_server(upload_file);

			// Store in pending attachments
			let item = {
				id: preview_id,
				name: file.name,
				url: upload_result.file_url,
				size: upload_file.size,
				is_image: is_img,
				is_excel: is_exc
			};

			this.pending_attachments.push(item);
			this._update_preview_item(preview_id, 'success');
		} catch (err) {
			console.error('File upload failed:', err);
			this._update_preview_item(preview_id, 'error');
			frappe.show_alert({
				message: __(`Failed to upload "${file.name}": ${err.message || 'Unknown error'}`),
				indicator: 'red'
			}, 7);
		} finally {
			this.is_processing = false;
		}
	}

	// ─── Server Upload ─────────────────────────────────────────────────────
	_upload_to_server(file) {
		return new Promise((resolve, reject) => {
			let form_data = new FormData();
			form_data.append('file', file, file.name);

			$.ajax({
				url: '/api/method/accountant_agent.accountant_agent.page.agent_chat.agent_chat.upload_agent_file',
				type: 'POST',
				data: form_data,
				processData: false,
				contentType: false,
				headers: {
					'X-Frappe-CSRF-Token': frappe.csrf_token
				},
				success: (response) => {
					if (response.message) {
						resolve(response.message);
					} else {
						reject(new Error('Upload returned empty response.'));
					}
				},
				error: (xhr) => {
					let msg = 'Upload failed.';
					try {
						let resp = JSON.parse(xhr.responseText);
						msg = resp._server_messages
							? JSON.parse(resp._server_messages)[0]
							: resp.exc_type || msg;
					} catch (e) { /* ignore parse errors */ }
					reject(new Error(msg));
				}
			});
		});
	}

	// ─── Client-Side Image Compression ─────────────────────────────────────
	_compress_image_client(file) {
		return new Promise((resolve, reject) => {
			let reader = new FileReader();
			reader.onload = (e) => {
				let img = new Image();
				img.onload = () => {
					try {
						let canvas = document.createElement('canvas');
						let ctx = canvas.getContext('2d');

						let max_dim = 1920;
						let width = img.width;
						let height = img.height;

						if (width > max_dim || height > max_dim) {
							let ratio = Math.min(max_dim / width, max_dim / height);
							width = Math.round(width * ratio);
							height = Math.round(height * ratio);
						}

						canvas.width = width;
						canvas.height = height;
						ctx.drawImage(img, 0, 0, width, height);

						canvas.toBlob(
							(blob) => {
								if (blob) {
									resolve(blob);
								} else {
									reject(new Error('Canvas compression produced empty blob.'));
								}
							},
							'image/jpeg',
							0.82
						);
					} catch (err) {
						reject(err);
					}
				};
				img.onerror = () => reject(new Error('Failed to load image for compression.'));
				img.src = e.target.result;
			};
			reader.onerror = () => reject(new Error('Failed to read image file.'));
			reader.readAsDataURL(file);
		});
	}

	// ─── Preview Area Management ───────────────────────────────────────────
	_add_preview_item(filename, type, status, file = null) {
		let preview_id = `preview-${Math.random().toString(36).substr(2, 9)}`;

		let icon_html;
		if (type === 'image' && file) {
			let thumb_url = URL.createObjectURL(file);
			icon_html = `<img src="${thumb_url}" class="agent-preview-thumb" alt="${filename}" />`;
		} else {
			icon_html = `<span class="agent-preview-icon">${this._get_file_icon(filename)}</span>`;
		}

		let status_html = status === 'uploading'
			? '<span class="agent-preview-status uploading"><i class="fa fa-spinner fa-spin"></i></span>'
			: '';

		let $item = $(`
			<div class="agent-preview-item ${status}" id="${preview_id}" data-type="${type}" data-name="${filename}">
				${icon_html}
				<span class="agent-preview-name" title="${filename}">${this._truncate_name(filename, 20)}</span>
				${status_html}
				<button class="agent-preview-remove" title="${__('Remove')}">
					<i class="fa fa-times"></i>
				</button>
			</div>
		`);

		$item.find('.agent-preview-remove').on('click', (e) => {
			e.stopPropagation();
			this._remove_attachment(preview_id);
		});

		this.$preview_area.find('.agent-upload-preview-items').append($item);
		this.$preview_area.show();

		return preview_id;
	}

	_update_preview_item(preview_id, status) {
		let $item = $(`#${preview_id}`);
		$item.removeClass('uploading error success').addClass(status);
		$item.find('.agent-preview-status').remove();

		if (status === 'error') {
			$item.append('<span class="agent-preview-status error"><i class="fa fa-exclamation-circle"></i></span>');
		} else if (status === 'success') {
			$item.append('<span class="agent-preview-status success"><i class="fa fa-check-circle"></i></span>');
			setTimeout(() => {
				$item.find('.agent-preview-status.success').fadeOut(300);
			}, 2000);
		}
	}

	_remove_attachment(preview_id) {
		$(`#${preview_id}`).fadeOut(200, function () {
			$(this).remove();
		});

		this.pending_attachments = this.pending_attachments.filter(a => a.id !== preview_id);

		setTimeout(() => {
			if (this.pending_attachments.length === 0) {
				this.$preview_area.hide();
			}
		}, 250);
	}

	// ─── Public API ────────────────────────────────────────────────────────

	get_file_urls() {
		return this.pending_attachments.map(a => a.url);
	}

	build_attachment_markers() {
		let markers = [];
		this.pending_attachments.forEach(att => {
			let type = att.is_image ? 'IMAGE' : 'FILE';
			markers.push(`[${type}:${att.name}:${att.url}]`);
		});
		return markers.length > 0 ? markers.join('\n') + '\n' : '';
	}

	has_attachments() {
		return this.pending_attachments.length > 0;
	}

	clear_attachments() {
		this.pending_attachments = [];
		this.$preview_area.find('.agent-upload-preview-items').empty();
		this.$preview_area.hide();
	}

	_get_file_icon(filename) {
		let ext = filename.split('.').pop().toLowerCase();
		let icon_map = {
			'pdf': '📄', 'docx': '📝', 'doc': '📝',
			'xlsx': '📊', 'xls': '📊', 'pptx': '📑',
			'ppt': '📑', 'txt': '📃', 'csv': '📊',
		};
		return icon_map[ext] || '📎';
	}

	_truncate_name(name, max_length) {
		if (name.length <= max_length) return name;
		let ext = name.split('.').pop();
		let base = name.substring(0, max_length - ext.length - 4);
		return `${base}...${ext}`;
	}
}
