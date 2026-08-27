/**
 * Agent Selector Module
 * ---------------------
 * Manages the selection of agent type ('ask', 'analyse', 'audit', 'reconcile', 'create').
 * Renders an intuitive, elegant agent type switcher in the chat UI.
 *
 * AGENT_DEFINITIONS is the single source of truth for per-agent upload limits.
 * The numbers here MUST mirror the AgentSettings declared for each agent on the
 * server in each agent's service module (its AgentSettings). If the UI is more
 * permissive than the server, the user uploads a file and is only told it is
 * too large after the request has already failed. file_upload_handler.js reads
 * these rules rather than repeating them.
 */

class AgentSelector {
	constructor(options = {}) {
		// 'auto' means the router reads the question and picks the desk. The
		// other values name a desk explicitly and are honoured as given.
		this.selected_agent = options.default_agent || 'auto';
		this.on_change = options.on_change || null;
		this.$container = null;

		this.AGENT_DEFINITIONS = {
			// Auto is a routing instruction, not a desk. It is sent to the
			// server as 'auto', which is the ONLY value that makes the router
			// classify the question instead of honouring a chosen desk.
			//
			// This entry used to be the 'ask' desk wearing the label "Auto",
			// so choosing it asked the server for the general Q&A desk by
			// name and no routing ever happened. Its budgets are the most
			// generous of any desk, because the question may end up anywhere.
			auto: {
				id: 'auto',
				name: __('Auto'),
				icon: 'fa-magic',
				badge_class: 'agent-type-ask',
				description: __("I'll read your question and bring in the right specialist."),
				rules: {
					max_files: 8,
					max_per_file_mb: 40,
					max_non_excel_total_mb: 40,
					max_excel_total_mb: 80,
					is_aggregate: true
				}
			},
			ask: {
				id: 'ask',
				name: __('General Q&A'),
				icon: 'fa-comments',
				badge_class: 'agent-type-ask',
				description: __('Quick accounting questions and document look-ups.'),
				rules: {
					max_files: 5,
					max_per_file_mb: 20,
					max_non_excel_total_mb: 20,
					max_excel_total_mb: 20,
					is_aggregate: false
				}
			},
			analyse: {
				id: 'analyse',
				name: __('Analyse Agent'),
				icon: 'fa-bar-chart',
				badge_class: 'agent-type-analyse',
				description: __('In-depth analysis. Max 5 files (10 MB total, Excel up to 20 MB).'),
				rules: {
					max_files: 5,
					max_per_file_mb: 20,
					// Mirrors ANALYSE_SETTINGS.max_non_excel_total_bytes (10 MB).
					max_non_excel_total_mb: 10,
					max_excel_total_mb: 20,
					is_aggregate: true
				}
			},
			audit: {
				id: 'audit',
				name: __('Audit Agent'),
				icon: 'fa-search-plus',
				badge_class: 'agent-type-audit',
				description: __('Compliance & Audit. Max 5 files (15 MB total, Excel up to 20 MB).'),
				rules: {
					max_files: 5,
					max_per_file_mb: 20,
					max_non_excel_total_mb: 15,
					max_excel_total_mb: 20,
					is_aggregate: true
				}
			},
			reconcile: {
				id: 'reconcile',
				name: __('Reconciliation Agent'),
				icon: 'fa-balance-scale',
				badge_class: 'agent-type-reconcile',
				description: __('Compare two sets of records and explain every difference. Spreadsheets only \u2014 up to 8 files, 40 MB total.'),
				// A reconciliation compares N sources, so it needs more headroom
				// than the single-source agents: a 3-way match with supporting
				// schedules is already 4-5 files before any opening balance.
				// Mirrors RECONCILE_SETTINGS on the server.
				rules: {
					max_files: 8,
					max_per_file_mb: 40,
					max_non_excel_total_mb: 20,
					max_excel_total_mb: 40,
					is_aggregate: true,
					// Uploads are spreadsheets only: a reconciliation compares two
					// tables, and a PDF is not one. Refusing it here means the
					// customer is told before a 40 MB upload rather than after.
					//
					// This restricts what can be UPLOADED, not what can be
					// reconciled against. The desk still reads the accounting
					// system through its own tools, so a spreadsheet against the
					// ERP - or the ERP against itself - is unaffected.
					//
					// MUST stay in step with allowed_extensions in
					// RECONCILE_SETTINGS (agent/agent_services/reconcile_service/
					// reconcile.py), which is the check that actually protects the
					// server.
					allowed_extensions: ['.xlsx', '.xls', '.ods']
				}
			},
			create: {
				id: 'create',
				name: __('Creator Agent'),
				icon: 'fa-pencil-square-o',
				badge_class: 'agent-type-create',
				description: __('Prepare and record entries in your ERP. Every document is saved as a draft for your approval. Max 3 files (10 MB total, Excel up to 20 MB).'),
				// The ONLY agent that writes. Mirrors CREATE_SETTINGS on the
				// server: fewer files than the read agents, because a creation
				// request is one document or one import sheet, not an N-part
				// comparison.
				rules: {
					max_files: 3,
					max_per_file_mb: 20,
					max_non_excel_total_mb: 10,
					max_excel_total_mb: 20,
					is_aggregate: true
				}
			}
		};
	}

	render($parent) {
		let selected = this.AGENT_DEFINITIONS[this.selected_agent];
		this.$container = $(`
			<div class="agent-dropdown-container">
				<button type="button" class="agent-dropdown-toggle-btn ${selected.badge_class}" title="${selected.description}">
					<i class="fa ${selected.icon}"></i>
					<span>${selected.name}</span>
					<i class="fa fa-chevron-up caret-icon"></i>
				</button>
				<div class="agent-dropdown-menu" style="display: none;">
					${Object.keys(this.AGENT_DEFINITIONS).map(key => {
						let agent = this.AGENT_DEFINITIONS[key];
						let is_active = key === this.selected_agent;
						return `
							<div class="agent-dropdown-item ${is_active ? 'active' : ''} ${agent.badge_class}" data-agent="${agent.id}">
								<div class="agent-dropdown-item-header">
									<i class="fa ${agent.icon}"></i>
									<span class="agent-dropdown-item-name">${agent.name}</span>
									${is_active ? '<i class="fa fa-check check-icon"></i>' : ''}
								</div>
								<div class="agent-dropdown-item-desc">${agent.description}</div>
							</div>
						`;
					}).join('')}
				</div>
			</div>
		`);

		// Bind event to toggle dropdown
		this.$container.find('.agent-dropdown-toggle-btn').on('click', (e) => {
			e.stopPropagation();
			this.$container.find('.agent-dropdown-menu').toggle();
			this.$container.find('.agent-dropdown-toggle-btn').toggleClass('open');
		});

		// Bind event to item selection
		this.$container.find('.agent-dropdown-item').on('click', (e) => {
			let agent_id = $(e.currentTarget).data('agent');
			this.set_agent(agent_id);
			this.$container.find('.agent-dropdown-menu').hide();
			this.$container.find('.agent-dropdown-toggle-btn').removeClass('open');
		});

		// Close dropdown when clicking outside
		$(document).on('click', (e) => {
			if (!$(e.target).closest('.agent-dropdown-container').length) {
				this.$container.find('.agent-dropdown-menu').hide();
				this.$container.find('.agent-dropdown-toggle-btn').removeClass('open');
			}
		});

		if ($parent) {
			$parent.append(this.$container);
		}
		return this.$container;
	}

	set_agent(agent_id) {
		if (!this.AGENT_DEFINITIONS[agent_id]) return;
		if (this.selected_agent === agent_id) return;

		this.selected_agent = agent_id;
		let agent = this.AGENT_DEFINITIONS[agent_id];

		if (this.$container) {
			// Update the toggle button
			let $toggle = this.$container.find('.agent-dropdown-toggle-btn');
			$toggle.attr('class', `agent-dropdown-toggle-btn ${agent.badge_class}`);
			$toggle.attr('title', agent.description);
			$toggle.html(`
				<i class="fa ${agent.icon}"></i>
				<span>${agent.name}</span>
				<i class="fa fa-chevron-up caret-icon"></i>
			`);

			// Update active class in list items
			this.$container.find('.agent-dropdown-item').removeClass('active');
			let $selected_item = this.$container.find(`.agent-dropdown-item[data-agent="${agent_id}"]`);
			$selected_item.addClass('active');

			// Update check icons
			this.$container.find('.check-icon').remove();
			$selected_item.find('.agent-dropdown-item-header').append('<i class="fa fa-check check-icon"></i>');
		}

		if (typeof this.on_change === 'function') {
			this.on_change(agent_id, this.get_rules());
		}
	}

	get_selected_agent() {
		return this.selected_agent;
	}

	get_rules() {
		return this.AGENT_DEFINITIONS[this.selected_agent].rules;
	}
}
