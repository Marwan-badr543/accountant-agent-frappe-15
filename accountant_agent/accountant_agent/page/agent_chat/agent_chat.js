frappe.pages['agent-chat'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('AI Accountant Agent'),
		single_column: true
	});

	frappe.require([
		'assets/accountant_agent/js/agent_selector.js',
		'assets/accountant_agent/js/file_upload_handler.js',
		'assets/accountant_agent/js/chat_attachments_renderer.js',
		'assets/accountant_agent/js/chat_ui_manager.js',
		'assets/accountant_agent/js/chat_session_manager.js',
		'assets/accountant_agent/js/chat_message_handler.js'
	], () => {
		new AccountantAgentChat(wrapper, page);
	});
};

class AccountantAgentChat {
	constructor(wrapper, page) {
		this.wrapper = $(wrapper);
		this.page = page;

		this.connected = false;
		this.connected_email = null;
		this.active_tab = 'login';

		// Instantiate Sub-Managers (Separation of Responsibilities)
		this.agent_selector = null;
		this.file_upload_handler = null;
		this.attachments_renderer = new ChatAttachmentsRenderer();
		this.ui_manager = new ChatUIManager(this);
		this.session_manager = new ChatSessionManager(this);
		this.message_handler = new ChatMessageHandler(this);

		frappe.realtime.on("agent_clarification_requested", (data) => {
			if (data && data.session_id === this.session_manager.session_id) {
				this.ui_manager.hide_typing_indicator(this.msg_box);
				this.show_clarification_popup(data.questions);
			}
		});

		this.init();
	}

	async init() {
		this.ui_manager.clear_typing_timers();
		this.wrapper.find('.page-content').empty();
		this.page.clear_primary_action();

		this.container = $('<div class="agent-chat-container"></div>').appendTo(this.wrapper.find('.page-content'));

		await this.check_connection();

		if (this.connected) {
			await this.render_chat_view();
		} else {
			this.render_auth_card();
		}
	}

	async check_connection() {
		frappe.dom.freeze(__('Checking agent connection...'));
		try {
			let agent_email = localStorage.getItem('connected_agent_email');
			let res = await frappe.xcall(
				'accountant_agent.accountant_agent.page.agent_chat.agent_chat.get_connection_status',
				{ agent_email: agent_email }
			);
			this.connected = res.connected;
			this.connected_email = res.email;
		} catch (e) {
			console.error("Connection check failed:", e);
			this.connected = false;
			this.connected_email = null;
		} finally {
			frappe.dom.unfreeze();
		}
	}

	render_auth_card() {
		this.container.empty();

		let card_html = `
			<div class="agent-auth-card">
				<h3 class="text-center" style="margin-top: 0; margin-bottom: 20px; font-weight: 700; color: var(--chat-primary);">
					Accountant Agent
				</h3>
				<ul class="nav nav-tabs d-flex justify-content-center">
					<li class="nav-item">
						<a class="nav-link ${this.active_tab === 'login' ? 'active' : ''}" data-tab="login">${__('Login')}</a>
					</li>
					<li class="nav-item">
						<a class="nav-link ${this.active_tab === 'signup' ? 'active' : ''}" data-tab="signup">${__('Sign Up')}</a>
					</li>
				</ul>
				
				<div class="auth-form-container">
					<form id="agent-auth-form">
						<div class="form-group signup-field" style="display: ${this.active_tab === 'signup' ? 'block' : 'none'};">
							<label for="auth-company">${__('Company Name')}</label>
							<input type="text" id="auth-company" placeholder="e.g. My Company Corp">
						</div>
						
						<div class="form-group">
							<label for="auth-email">${__('Email Address')}</label>
							<input type="email" id="auth-email" placeholder="email@example.com" required>
						</div>
						
						<div class="form-group">
							<label for="auth-password">${__('Password')}</label>
							<input type="password" id="auth-password" placeholder="••••••••" required>
						</div>
						
						<button type="submit" class="agent-auth-btn">
							${this.active_tab === 'login' ? __('Connect') : __('Create Account')}
						</button>
					</form>
				</div>
			</div>
		`;

		let $card = $(card_html).appendTo(this.container);
		this.setup_auth_events($card);
	}

	setup_auth_events($card) {
		$card.find('.nav-link').on('click', (e) => {
			let tab = $(e.currentTarget).data('tab');
			this.active_tab = tab;
			$card.find('.nav-link').removeClass('active');
			$(e.currentTarget).addClass('active');

			if (tab === 'signup') {
				$card.find('.signup-field').slideDown(200);
				$card.find('.agent-auth-btn').text(__('Create Account'));
			} else {
				$card.find('.signup-field').slideUp(200);
				$card.find('.agent-auth-btn').text(__('Connect'));
			}
		});

		$card.find('#agent-auth-form').on('submit', async (e) => {
			e.preventDefault();

			let company_name = $card.find('#auth-company').val();
			let email = $card.find('#auth-email').val();
			let password = $card.find('#auth-password').val();

			frappe.dom.freeze(this.active_tab === 'login' ? __('Connecting...') : __('Creating Account...'));

			try {
				let res = await frappe.xcall(
					'accountant_agent.accountant_agent.page.agent_chat.agent_chat.authenticate_agent',
					{
						mode: this.active_tab,
						email: email,
						password: password,
						company_name: company_name
					}
				);

				if (res && res.success) {
					frappe.show_alert({ message: __('Connected successfully!'), indicator: 'green' });
					this.connected = true;
					this.connected_email = res.email;
					localStorage.setItem('connected_agent_email', res.email);
					await this.render_chat_view();
				}
			} catch (err) {
				console.error(err);
			} finally {
				frappe.dom.unfreeze();
			}
		});
	}

	async render_chat_view() {
		this.container.empty();

		let chat_html = `
			<div class="agent-chat-layout">
				<!-- Left Sidebar (Sessions List) -->
				<div class="agent-sidebar">
					<button class="new-chat-btn">
						<i class="fa fa-plus"></i> ${__('New Chat')}
					</button>
					<div class="agent-chat-list"></div>
				</div>
				
				<!-- Main Chat View Area -->
				<div class="agent-chat-view">
					<!-- Header -->
					<div class="agent-chat-header">
						<div class="agent-status-container">
							<div class="agent-status-badge">
								<div class="agent-status-dot"></div>
								${__('Connected:')} <a href="javascript:void(0)" class="agent-email-link" title="${__('Click to view Agent Settings')}"><strong>${this.connected_email}</strong> <i class="fa fa-cog" style="font-size: 11px; margin-left: 3px;"></i></a>
							</div>
						</div>
						<div class="agent-header-actions" style="display: flex; align-items: center; gap: 12px;">
							<button class="agent-settings-btn btn btn-xs btn-default" style="display: flex; align-items: center; gap: 4px; padding: 5px 10px; border-radius: 6px;">
								<i class="fa fa-cog"></i> ${__('Settings')}
							</button>
						</div>
					</div>
					
					<!-- Messages Container -->
					<div class="agent-messages-container" id="agent-msg-box"></div>
					
					<!-- Input Area -->
					<div class="agent-input-container" style="flex-direction: column; align-items: stretch; gap: 8px; position: relative;">
						<div class="agent-clarification-popup" style="display: none;"></div>
						<div style="display: flex; gap: 15px; align-items: flex-end; width: 100%;">
							<div class="agent-selector-container"></div>
							<textarea class="agent-textarea" placeholder="${__('Type your financial question or query here...')}" id="agent-input-msg" maxlength="10000"></textarea>
							<button class="agent-send-btn" id="agent-send-trigger" title="${__('Send Message')}">
								<svg viewBox="0 0 24 24">
									<path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
								</svg>
							</button>
						</div>
						<div class="agent-char-counter" style="align-self: flex-end; font-size: 11px; color: var(--chat-text-muted); margin-right: 61px;">
							0 / 10000
						</div>
					</div>
				</div>
			</div>
		`;

		this.layout = $(chat_html).appendTo(this.container);
		this.sidebar = this.layout.find('.agent-sidebar');
		this.msg_box = this.layout.find('#agent-msg-box');
		this.textarea = this.layout.find('#agent-input-msg');
		this.popup_container = this.layout.find('.agent-clarification-popup');

		// Initialize Agent Selector UI
		this.agent_selector = new AgentSelector({ default_agent: 'ask' });
		this.agent_selector.render(this.layout.find('.agent-selector-container'));

		// Initialize File Upload Handler
		this.file_upload_handler = new FileUploadHandler(this);
		this.file_upload_handler.init(
			this.layout.find('.agent-input-container'),
			this.textarea
		);

		this.setup_chat_events();

		await this.session_manager.load_chats();
	}

	setup_chat_events() {
		this.layout.find('.agent-email-link').on('click', async (e) => {
			e.preventDefault();
			if (!this.connected_email) return;
			try {
				let doc_name = await frappe.xcall(
					'accountant_agent.accountant_agent.doctype.agent_settings.agent_settings.get_agent_settings_name',
					{ email: this.connected_email }
				);
				if (doc_name) {
					frappe.set_route('Form', 'Agent Settings', doc_name);
				} else {
					frappe.show_alert({ message: __('Agent Settings record not found.'), indicator: 'orange' });
				}
			} catch (err) {
				console.error("Error opening Agent Settings:", err);
			}
		});

		this.sidebar.find('.new-chat-btn').on('click', () => {
			this.session_manager.set_new_chat_draft();
		});

		this.layout.find('.agent-settings-btn').on('click', () => {
			let self = this;
			let d = new frappe.ui.Dialog({
				title: __('Agent Settings'),
				fields: [
					{
						fieldtype: 'HTML',
						options: `
							<div style="font-size: 13px; line-height: 1.6; margin-bottom: 20px; color: var(--text-color);">
								${__('Manage your Accountant Agent connection and account settings.')}
							</div>
							<div style="display: flex; flex-direction: column; gap: 10px;">
								<button class="btn btn-default btn-block logout-action-btn" style="text-align: left; display: flex; align-items: center; gap: 8px; padding: 10px 15px; margin: 0;">
									<i class="fa fa-sign-out text-muted" style="font-size: 16px; width: 20px;"></i>
									<div>
										<strong style="display: block; font-size: 13px;">${__('Log Out')}</strong>
										<span style="font-size: 11px; color: var(--text-muted); font-weight: normal;">${__('Disconnect the agent temporarily.')}</span>
									</div>
								</button>
								<button class="btn btn-danger btn-block delete-action-btn" style="text-align: left; display: flex; align-items: center; gap: 8px; padding: 10px 15px; background-color: var(--bg-red-light, #fff5f5); border-color: var(--border-red, #ffcccc); color: var(--text-red, #c53030); margin: 0;">
									<i class="fa fa-trash" style="font-size: 16px; width: 20px;"></i>
									<div>
										<strong style="display: block; font-size: 13px;">${__('Delete Account')}</strong>
										<span style="font-size: 11px; color: var(--text-red, #c53030); opacity: 0.8; font-weight: normal;">${__('Permanently delete your account.')}</span>
									</div>
								</button>
							</div>
						`
					}
				]
			});

			d.$wrapper.find('.logout-action-btn').on('click', async () => {
				d.hide();
				frappe.dom.freeze(__('Logging out...'));
				try {
					let agent_email = localStorage.getItem('connected_agent_email');
					await frappe.xcall(
						'accountant_agent.accountant_agent.page.agent_chat.agent_chat.disconnect_agent',
						{ agent_email: agent_email }
					);
					self.connected = false;
					self.connected_email = null;
					localStorage.removeItem('connected_agent_email');
					self.session_manager.session_id = null;
					await self.init();
					frappe.show_alert({ message: __('Logged out successfully!'), indicator: 'green' });
				} catch (e) {
					console.error("Logout error:", e);
				} finally {
					frappe.dom.unfreeze();
				}
			});

			d.$wrapper.find('.delete-action-btn').on('click', () => {
				frappe.confirm(
					__('Are you sure you want to permanently delete your agent account?'),
					async () => {
						d.hide();
						frappe.dom.freeze(__('Deleting account...'));
						try {
							let agent_email = localStorage.getItem('connected_agent_email');
							await frappe.xcall(
								'accountant_agent.accountant_agent.page.agent_chat.agent_chat.delete_agent_account',
								{ agent_email: agent_email }
							);
							self.connected = false;
							self.connected_email = null;
							localStorage.removeItem('connected_agent_email');
							self.session_manager.session_id = null;
							await self.init();
							frappe.show_alert({ message: __('Account deleted successfully!'), indicator: 'green' });
						} catch (e) {
							console.error("Delete account error:", e);
						} finally {
							frappe.dom.unfreeze();
						}
					}
				);
			});

			d.show();
		});

		this.textarea.on('input', () => {
			this.textarea.css('height', 'auto');
			this.textarea.css('height', (this.textarea[0].scrollHeight) + 'px');
			let length = this.textarea.val().length;
			this.layout.find('.agent-char-counter').text(`${length} / 10000`);
		});

		this.textarea.on('keydown', (e) => {
			if (e.which === 13 && !e.shiftKey) {
				e.preventDefault();
				let btn = this.layout.find('#agent-send-trigger');
				if (!btn.hasClass('agent-cancel-btn')) {
					this.message_handler.send_user_message();
				}
			}
		});

		this.layout.find('#agent-send-trigger').on('click', () => {
			let btn = this.layout.find('#agent-send-trigger');
			if (btn.hasClass('agent-cancel-btn')) {
				this.message_handler.cancel_agent_execution();
			} else {
				this.message_handler.send_user_message();
			}
		});
	}

	show_clarification_popup(questions) {
		this.message_handler.show_clarification_popup(questions);
	}

	generate_uuid() {
		return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
			let r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
			return v.toString(16);
		});
	}
}
